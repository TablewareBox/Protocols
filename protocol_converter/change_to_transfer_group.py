import json
import os
from pathlib import Path
from pprint import pprint
import re
from typing import List, Dict, Any, Optional, Tuple

_DEF_WELL_COUNTS = (384, 96, 48, 24)

def _parse_well_count(name: str) -> Optional[int]:
    s = name.lower()
    for k in _DEF_WELL_COUNTS:
        # 匹配独立数字（避免把 96 匹配到 196 等）
        if re.search(rf'(^|[^0-9]){k}([^0-9]|$)', s):
            return k
    return None

def _parse_capacity_ul(name: str) -> Optional[float]:
    s = name.lower()
    m = re.search(r'(\d+(?:\.\d+)?)(\s*(?:u?l|[µμ]l|ml))', s, re.IGNORECASE)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).strip().lower()
    if unit == 'ml':
        return val * 1000.0
    # 兼容 'ul' / 'u l' / 'µl' / 'μl'
    return val

def _apply_pose_z_volumes(aspirate_list: List[Tuple[float, Optional[str]]]) -> Tuple[Optional[float], float, Optional[float]]:
    """
    根据相邻 aspirate 的 pose_z 分配体积参数。
    aspirate_list: [(vol, pose_z), ...]，pose_z 为 "top"、"bottom" 或 None。
    返回 (blow_out_air_volume_before, asp_vol, blow_out_air_volume)。
    - 2 个相邻 aspirate：(top, bottom) -> 第一个=blow_out_air_volume_before，第二个=asp_vols
    - 2 个相邻 aspirate：(bottom, top) -> 第一个=asp_vols，第二个=blow_out_air_volume
    - 3 个相邻 aspirate：第一个=blow_out_air_volume_before，第二个=asp_vols，第三个=blow_out_air_volume
    - 其他情况：仅 asp_vol = 各体积之和（或第一个体积），无 blow 参数
    """
    if not aspirate_list:
        return None, 0.0, None
    n = len(aspirate_list)
    if n == 1:
        return None, aspirate_list[0][0], None
    if n == 2:
        v1, p1 = aspirate_list[0]
        v2, p2 = aspirate_list[1]
        if p1 == "top" and p2 == "bottom":
            return v1, v2, None
        if p1 == "bottom" and p2 == "top":
            return None, v1, v2
        # 无法按 pose_z 区分，回退：第二个作为 asp_vol，第一个作为 blow_before（兼容旧逻辑）
        return v1, v2, None
    if n >= 3:
        v1, _ = aspirate_list[0]
        v2, _ = aspirate_list[1]
        v3, _ = aspirate_list[2]
        return v1, v2, v3
    return None, sum(a[0] for a in aspirate_list), None


def _parse_pose_z_height(pose_z: Optional[str], top_plus_ten: bool = False) -> Optional[float]:
    """从 pose_z 字符串中提取液面高度，如 bottom(1) -> 1.0。"""
    if not pose_z:
        return None
    pose_z_str = str(pose_z)
    m = re.search(r'\(([-+]?\d+(?:\.\d+)?)\)', pose_z_str)
    if not m:
        return None
    try:
        v = float(m.group(1))
        # 仅在 dispense 的 top(z) 场景启用：liquid_height = z + 10
        if top_plus_ten and pose_z_str.startswith("top"):
            return v + 10.0
        return v
    except Exception:
        return None


def _extract_mix_info(step: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """提取 mix 参数。"""
    return {
        "mix_times": step.get("mix_time"),
        "mix_vol": step.get("vol"),
        "mix_rate": step.get("flow_rate"),
        "mix_liquid_height": _parse_pose_z_height(step.get("pose_z")),
    }


def _delay_to_seconds(step: Dict[str, Any]) -> float:
    """将 delay step 统一换算成秒。"""
    minutes = step.get("minutes", 0) or 0
    seconds = step.get("seconds", 0) or 0
    try:
        return float(minutes) * 60.0 + float(seconds)
    except Exception:
        return 0.0


def _merge_mix_stage(stages: List[Optional[str]]) -> Optional[str]:
    has_before = any(s in ("before", "both") for s in stages if s)
    has_after = any(s in ("after", "both") for s in stages if s)
    if has_before and has_after:
        return "both"
    if has_before:
        return "before"
    if has_after:
        return "after"
    return None


def _min_or_none(values: List[Any]) -> Optional[float]:
    vals = []
    for v in values:
        if v is None:
            continue
        try:
            vals.append(float(v))
        except Exception:
            continue
    return min(vals) if vals else None


def get_action_list(steps_file, slot_to_labware_type: Dict[int, str] = None):
    """从steps JSON文件提取action list，包含体积和流速信息。
    按顺序读取相邻 aspirate，根据 pose_z 分配：
    - 2个相邻：(top,bottom)->blow_out_air_volume_before+asp_vols；(bottom,top)->asp_vols+blow_out_air_volume
    - 3个相邻：blow_out_air_volume_before + asp_vols + blow_out_air_volume
    按phase分组，每个phase单独生成action，不跨phase合并。
    slot_to_labware_type: 可选，slot->labware_type映射；用于按tip rack类型隔离合并。"""
    with open(steps_file, "r") as f:
        data = json.load(f)

    # 按顺序解析：收集 (source, target, aspirate_block, dispense_info, is_split)
    # is_split: 1asp+多disp 拆分出的单独 transfer，需单独成 action
    transfers: List[Tuple] = []
    current_tip_rack_slot = None

    for phase in data:
        i = 0
        pending_air_gap_before = 0.0
        while i < len(phase):
            step = phase[i]
            if step['action'] == "pick_tip":
                current_tip_rack_slot = step['tip_rack']['slot']
                pending_air_gap_before = 0.0
                i += 1
                continue
            if step['action'] == "air_gap":
                pending_air_gap_before = float(step.get('vol', 0))
                i += 1
                continue
            if step['action'] == "aspirate":
                air_gap_before_vol = pending_air_gap_before
                pending_air_gap_before = 0.0

                # ---- back-aspirate-from-target 预扫描 ----
                # 找紧随的 dispense target，把 source==target 的 aspirate 视为回吸（如
                # p20.aspirate(vol, src); p20.aspirate(1, dest); p20.dispense(at dest)），
                # 剥离到 pre_asp_from_target_vol，避免被误判为 consolidate 多源。
                pre_asp_from_target_vol = 0.0
                look = i
                while look < len(phase) and phase[look].get('action') == 'aspirate':
                    look += 1
                while look < len(phase) and phase[look].get('action') in ('air_gap', 'delay'):
                    look += 1
                backasp_tgt_key = None
                if look < len(phase) and phase[look].get('action') == 'dispense':
                    dis_tgt_peek = phase[look].get('target', {})
                    backasp_tgt_key = (dis_tgt_peek.get('slot'), dis_tgt_peek.get('well'))

                # 收集连续同源的 aspirate（跳过 back-aspirate）
                asp_block: List[Tuple[float, Optional[str]]] = []
                src_key = None
                j = i
                while j < len(phase) and phase[j]['action'] == "aspirate":
                    s = phase[j]
                    sk = (s['source']['slot'], s['source']['well'])
                    if backasp_tgt_key is not None and sk == backasp_tgt_key:
                        # source 即紧随 dispense 的 target → 回吸，剥离后续不计
                        pre_asp_from_target_vol += float(s.get('vol', 0))
                        j += 1
                        continue
                    if src_key is not None and sk != src_key:
                        break
                    src_key = sk
                    vol = s.get('vol', 0)
                    pose_z = s.get('pose_z')
                    asp_block.append((vol, pose_z))
                    j += 1

                # ---- consolidate 模式检测 ----
                # j 仍指向不同 source 的 aspirate → 可能是多源合并到单目标
                if j < len(phase) and phase[j].get('action') == 'aspirate' and src_key is not None:
                    # 收集从 i 开始的全部连续 aspirate（不限 source），但跳过 back-aspirate
                    all_cons = []
                    k_c = i
                    while k_c < len(phase) and phase[k_c]['action'] == 'aspirate':
                        s_c = phase[k_c]
                        sk_c = (s_c['source']['slot'], s_c['source']['well'])
                        if backasp_tgt_key is not None and sk_c == backasp_tgt_key:
                            k_c += 1
                            continue
                        all_cons.append((sk_c, s_c.get('vol', 0), s_c.get('pose_z')))
                        k_c += 1
                    # 跳过 air_gap / delay，找到紧随的 dispense
                    k_d = k_c
                    cons_ag_after = 0.0
                    if k_d < len(phase) and phase[k_d].get('action') == 'air_gap':
                        cons_ag_after = float(phase[k_d].get('vol', 0))
                        k_d += 1
                    while k_d < len(phase) and phase[k_d].get('action') == 'delay':
                        k_d += 1
                    if k_d < len(phase) and phase[k_d].get('action') == 'dispense':
                        dis_s = phase[k_d]
                        dis_vol_total = dis_s.get('vol', 0)
                        # 回吸量已被吸到 tip 内并随 dispense 一起吐出，故从 dis_vol 中扣除
                        adj_dis_vol_total = dis_vol_total - pre_asp_from_target_vol
                        asp_total_c = sum(a[1] for a in all_cons)
                        # 允许微小误差；total_asp ≈ dis_vol 即为 consolidate
                        if abs(asp_total_c - adj_dis_vol_total) < 0.5 or \
                                abs(asp_total_c - adj_dis_vol_total - cons_ag_after) < 0.5:
                            # 构造 3-tuple asp_block：(vol, pose_z, actual_src_key)
                            multi_asp = [(a[1], a[2], a[0]) for a in all_cons]
                            cons_src_key = ("__consolidate__",
                                            dis_s['target']['slot'],
                                            dis_s['target']['well'])
                            cons_tgt_key = (dis_s['target']['slot'], dis_s['target']['well'])
                            cons_liq_h = _parse_pose_z_height(
                                dis_s.get('pose_z'), top_plus_ten=True)
                            # 向前查找 mix_before
                            b2, cons_before_mix = i - 1, None
                            while b2 >= 0:
                                p2 = phase[b2]
                                a2 = p2.get('action')
                                if a2 == 'mix':
                                    cons_before_mix = _extract_mix_info(p2)
                                    break
                                if a2 in ('aspirate', 'dispense', 'pick_tip', 'drop_tip'):
                                    break
                                b2 -= 1
                            transfers.append((
                                cons_src_key, cons_tgt_key, multi_asp,
                                all_cons[0][1],              # representative dis_vol
                                dis_s.get('flow_rate', 7.6),
                                current_tip_rack_slot, False,
                                cons_before_mix, None,
                                cons_liq_h, False, 0.0,
                                air_gap_before_vol, cons_ag_after,
                                pre_asp_from_target_vol))
                            i = k_d + 1
                            continue

                # aspirate 块之后紧跟 air_gap → blow_out_air_volume
                air_gap_after_vol = 0.0
                if j < len(phase) and phase[j].get('action') == "air_gap":
                    air_gap_after_vol = float(phase[j].get('vol', 0))
                    j += 1

                # 收集该 aspirate 之后的所有连续 dispense（直到遇到下一个 aspirate/pick_tip）
                dispenses: List[Tuple[Tuple, float, float, Optional[float], float]] = []
                before_mix = None
                after_mixes: List[Dict[str, Optional[float]]] = []
                has_touch_tip = False
                aspirate_delay_seconds = 0.0

                # 查找 aspirate 之前最近的 mix（遇到关键液体动作则停止）
                b = i - 1
                while b >= 0:
                    prev = phase[b]
                    a = prev.get('action')
                    if a == "mix":
                        before_mix = _extract_mix_info(prev)
                        break
                    if a in ("aspirate", "dispense", "pick_tip", "drop_tip"):
                        break
                    b -= 1

                k = j
                # aspirate 后紧邻 delay（分钟已换算秒）
                while k < len(phase) and phase[k].get('action') == "delay":
                    if k == 0 or phase[k - 1].get('action') != "drop_tip":
                        aspirate_delay_seconds += _delay_to_seconds(phase[k])
                    k += 1
                seen_valid_dispense = False
                while k < len(phase):
                    st = phase[k]
                    if st['action'] == "aspirate" or st['action'] == "pick_tip":
                        break
                    if st['action'] == "dispense":
                        vol_d = st.get('vol', 0)
                        tgt = st.get('target', {})
                        lab = (tgt.get('labware') or "").lower()
                        if vol_d != -1 and "trash" not in lab and tgt.get('slot') != 12:
                            tgt_key = (tgt['slot'], tgt['well'])
                            # 第一笔 dispense 扣除回吸量（回吸液体随 dispense 一并吐出）
                            if not seen_valid_dispense and pre_asp_from_target_vol > 0:
                                vol_d = max(0, vol_d - pre_asp_from_target_vol)
                            liquid_height = _parse_pose_z_height(st.get('pose_z'), top_plus_ten=True)
                            # dispense 后紧邻 delay，drop_tip 后的 delay 不计入
                            delay_seconds = 0.0
                            d = k + 1
                            while d < len(phase) and phase[d].get('action') == "delay":
                                if phase[d - 1].get('action') != "drop_tip":
                                    delay_seconds += _delay_to_seconds(phase[d])
                                d += 1
                            if delay_seconds <= 0 and aspirate_delay_seconds > 0:
                                delay_seconds = aspirate_delay_seconds
                            dispenses.append((tgt_key, vol_d, st.get('flow_rate', 7.6), liquid_height, delay_seconds))
                            seen_valid_dispense = True
                            k = d
                            continue
                    elif st['action'] == "mix" and seen_valid_dispense:
                        after_mixes.append(_extract_mix_info(st))
                    elif st['action'] == "touch_tip":
                        has_touch_tip = True
                    k += 1

                after_mix = None
                if after_mixes:
                    # 多个 after mix，逐参数取最小
                    after_mix = {
                        "mix_times": _min_or_none([m.get("mix_times") for m in after_mixes]),
                        "mix_vol": _min_or_none([m.get("mix_vol") for m in after_mixes]),
                        "mix_rate": _min_or_none([m.get("mix_rate") for m in after_mixes]),
                        "mix_liquid_height": _min_or_none([m.get("mix_liquid_height") for m in after_mixes]),
                    }

                # 若 1 个 aspirate + 多个 dispense，且 asp_vol >= sum(disp_vols)，拆成多个 1:1 transfer
                asp_total = sum(a[0] for a in asp_block)
                if len(asp_block) == 1 and len(dispenses) >= 2 and asp_total >= sum(d[1] for d in dispenses):
                    first_split = True
                    for tgt_key, vol_d, dis_fr, liq_h, delay_seconds in dispenses:
                        single_asp = [(vol_d, asp_block[0][1])]
                        # 回吸只在第一笔 split 上记账，避免在拆分中被重复计算
                        split_pre_asp = pre_asp_from_target_vol if first_split else 0.0
                        transfers.append((src_key, tgt_key, single_asp, vol_d, dis_fr, current_tip_rack_slot, True, before_mix, after_mix, liq_h, has_touch_tip, delay_seconds, air_gap_before_vol, air_gap_after_vol, split_pre_asp))
                        first_split = False
                elif dispenses:
                    tgt_key, vol_d, dis_fr, liq_h, delay_seconds = dispenses[0]
                    transfers.append((src_key, tgt_key, asp_block, vol_d, dis_fr, current_tip_rack_slot, False, before_mix, after_mix, liq_h, has_touch_tip, delay_seconds, air_gap_before_vol, air_gap_after_vol, pre_asp_from_target_vol))

                i = j
                continue
            i += 1

    # 按 (source, target_slot, tip_labware_type) 分组，构建 action_list。
    # tip rack 种类不同的 transfer 不能合并，用 labware_type（而非 slot）作为分组维度，
    # 以便同类型不同 slot 的 tip rack 仍可合并。
    # is_split 时用 (src, tgt) 作 key；否则用 (src, tgt_slot) 避免不同目标 slot 混在一起
    def _tip_type_key(tip_slot: int) -> str:
        """将 tip_slot 转换为 labware_type 字符串，用于分组。"""
        if slot_to_labware_type and tip_slot is not None:
            return slot_to_labware_type.get(tip_slot, f"__slot_{tip_slot}")
        # 未提供映射时退回旧行为（不按 tip rack 区分），返回固定常量
        return ""

    source_to_transfers: Dict[Tuple, List] = {}
    for t in transfers:
        src, tgt, asp_block, dis_vol, dis_fr, tip_slot, is_split, before_mix, after_mix, liquid_height, touch_tip, delay_seconds = t[0], t[1], t[2], t[3], t[4], t[5], t[6], t[7], t[8], t[9], t[10], t[11]
        air_gap_bef = t[12] if len(t) > 12 else 0.0
        air_gap_aft = t[13] if len(t) > 13 else 0.0
        pre_asp_tgt = t[14] if len(t) > 14 else 0.0
        tip_type = _tip_type_key(tip_slot)
        key = (src, tgt, tip_type) if is_split else (src, tgt[0], tip_type)
        if key not in source_to_transfers:
            source_to_transfers[key] = []
        source_to_transfers[key].append((tgt, asp_block, dis_vol, dis_fr, tip_slot, before_mix, after_mix, liquid_height, touch_tip, delay_seconds, air_gap_bef, air_gap_aft, pre_asp_tgt))

    action_list = []
    for key, tlist in source_to_transfers.items():
        source_well = key[0]
        tip_slots = {t[4] for t in tlist if t[4] is not None}
        tip_racks = f"tiprack_{sorted(tip_slots)[0]}" if tip_slots else ""

        asp_vols_array = []
        dis_vols_array = []
        asp_flow_rates_array = []
        dis_flow_rates_array = []
        blow_before_array: List[Optional[float]] = []
        blow_after_array: List[Optional[float]] = []
        liquid_height_array: List[Optional[float]] = []
        delays_array: List[float] = []
        mix_stages: List[Optional[str]] = []
        mix_times_vals: List[Any] = []
        mix_vol_vals: List[Any] = []
        mix_rate_vals: List[Any] = []
        mix_height_vals: List[Any] = []
        touch_tip_flags: List[bool] = []
        pre_asp_target_array: List[float] = []
        # consolidate 模式：asp_block 为 3-tuple，收集全部实际 source well
        consolidate_sources: List[Tuple] = []

        for tgt, asp_block, dis_vol, dis_fr, _, before_mix, after_mix, liquid_height, touch_tip, delay_seconds, air_gap_bef, air_gap_aft, pre_asp_tgt in tlist:
            # --- consolidate 模式 (asp_block 含 3-tuple: (vol, pose_z, actual_src_key)) ---
            if asp_block and len(asp_block[0]) == 3:
                for idx_c, (vol, pose_z, actual_src) in enumerate(asp_block):
                    asp_vols_array.append(float(vol))
                    dis_vols_array.append(float(vol))
                    asp_flow_rates_array.append(dis_fr)
                    dis_flow_rates_array.append(dis_fr)
                    blow_before_array.append(None)
                    blow_after_array.append(None)
                    liquid_height_array.append(liquid_height)
                    touch_tip_flags.append(bool(touch_tip))
                    delays_array.append(float(delay_seconds or 0.0))
                    mix_stages.append(None)
                    consolidate_sources.append(actual_src)
                    # 回吸量挂在第一条 cons 上
                    pre_asp_target_array.append(float(pre_asp_tgt) if idx_c == 0 else 0.0)
                continue

            # --- 普通模式 ---
            blow_before, asp_vol, blow_after = _apply_pose_z_volumes(asp_block)
            # 合并 air_gap 与 pose_z 推断的 blow 值
            if air_gap_bef:
                blow_before = (blow_before or 0) + air_gap_bef
            if air_gap_aft:
                blow_after = (blow_after or 0) + air_gap_aft
            # dis_vols = 原 dispense 体积 - 所有 air gap 体积
            adj_dis = dis_vol - (blow_before or 0) - (blow_after or 0)
            adj_dis = max(0, adj_dis)
            # 有 blow 时 asp_vol 与 dis_vol 保持一致（均为实际样品量）
            if blow_before or blow_after:
                asp_vol = adj_dis
            asp_vols_array.append(asp_vol)
            dis_vols_array.append(adj_dis)
            asp_flow_rates_array.append(dis_fr)
            dis_flow_rates_array.append(dis_fr)
            blow_before_array.append(blow_before)
            blow_after_array.append(blow_after)
            liquid_height_array.append(liquid_height)
            touch_tip_flags.append(bool(touch_tip))
            delays_array.append(float(delay_seconds or 0.0))
            pre_asp_target_array.append(float(pre_asp_tgt or 0.0))

            has_before = before_mix is not None
            has_after = after_mix is not None
            stage = "both" if (has_before and has_after) else ("before" if has_before else ("after" if has_after else None))
            mix_stages.append(stage)
            for mix_info in (before_mix, after_mix):
                if not mix_info:
                    continue
                mix_times_vals.append(mix_info.get("mix_times"))
                mix_vol_vals.append(mix_info.get("mix_vol"))
                mix_rate_vals.append(mix_info.get("mix_rate"))
                mix_height_vals.append(mix_info.get("mix_liquid_height"))

        avg_asp = sum(asp_vols_array) / len(asp_vols_array) if asp_vols_array else 0
        avg_dis = sum(dis_vols_array) / len(dis_vols_array) if dis_vols_array else 0
        avg_asp_fr = sum(asp_flow_rates_array) / len(asp_flow_rates_array) if asp_flow_rates_array else 7.6
        avg_dis_fr = sum(dis_flow_rates_array) / len(dis_flow_rates_array) if dis_flow_rates_array else 7.6

        # consolidate 时用收集到的各实际 source well；否则用 group key 中的 source well
        aspirate_list = consolidate_sources if consolidate_sources else [source_well]

        # 确定 tip rack 的 labware type，供后续 simplify/merge 分组用
        if tip_slots and slot_to_labware_type:
            tip_labware_type = slot_to_labware_type.get(
                sorted(tip_slots)[0], f"__slot_{sorted(tip_slots)[0]}")
        else:
            tip_labware_type = tip_racks  # 无映射时退回 tiprack key 本身

        act: Dict[str, Any] = {
            "phase": len(action_list),
            "aspirate": aspirate_list,
            "dispense": [t[0] for t in tlist],
            "asp_vol": avg_asp,
            "dis_vol": avg_dis,
            "asp_flow_rate": avg_asp_fr,
            "dis_flow_rate": avg_dis_fr,
            "asp_vols": asp_vols_array,
            "dis_vols": dis_vols_array,
            "asp_flow_rates": asp_flow_rates_array,
            "dis_flow_rates": dis_flow_rates_array,
            "tip_racks": tip_racks,
            "_tip_labware_type": tip_labware_type,
        }
        if any(blow_before_array):
            act["blow_out_air_volume_before"] = [v or 0 for v in blow_before_array]
        if any(blow_after_array):
            act["blow_out_air_volume"] = [v or 0 for v in blow_after_array]
        if any(float(v or 0) > 0 for v in pre_asp_target_array):
            act["pre_aspirate_from_target"] = [float(v or 0) for v in pre_asp_target_array]
        act["liquid_height"] = [0 if v is None else v for v in liquid_height_array]
        if any(float(v or 0) > 0 for v in delays_array):
            act["delays"] = delays_array
        if any(touch_tip_flags):
            act["touch_tip"] = True
        mix_stage = _merge_mix_stage(mix_stages)
        if mix_stage:
            act["mix_stage"] = mix_stage
            mix_times = _min_or_none(mix_times_vals)
            mix_vol = _min_or_none(mix_vol_vals)
            mix_rate = _min_or_none(mix_rate_vals)
            mix_height = _min_or_none(mix_height_vals)
            if mix_times is not None:
                act["mix_times"] = int(mix_times)
            if mix_vol is not None:
                act["mix_vol"] = mix_vol
            if mix_rate is not None:
                act["mix_rate"] = mix_rate
            if mix_height is not None:
                act["mix_liquid_height"] = mix_height
        action_list.append(act)

    return action_list


def extract_labware_info_from_json(json_data: dict, total_slots: int) -> Tuple[list, dict]:
    """
    从 Opentrons JSON 配置中提取板位信息，并根据 `total_slots` 进行槽位映射：
      - 若 total_slots >= 12：不映射，保留原始 slot。
      - 若 total_slots < 12：将出现过的原始 slot（去重、按出现顺序）紧凑映射到 1..total_slots。
        若去重后的原始 slot 数量 > total_slots，则报错。
    返回:
      output: 规范化后的 labware 列表
      replace_map: {原始slot: 新slot}
    """
    labware_list = json_data.get("labware", [])
    if not isinstance(labware_list, list):
        raise ValueError("json_data['labware'] must be a list.")

    if len(labware_list) > 12:
        # 你原来文本里已经放宽到 12，这里沿用
        raise ValueError("Labware list exceeds 12 items, which is not supported by the PRCXI 9320.")

    # 1) 收集"原始 slot"出现顺序（去重），转换为整数
    orig_slots_in_order = []
    for lw in labware_list:
        s = lw.get("slot")
        if s is None:
            raise ValueError(f"Labware item missing 'slot': {lw}")
        # 转换为整数
        try:
            s_int = int(s)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid slot value (must be convertible to int): {s}")
        if s_int not in orig_slots_in_order:
            orig_slots_in_order.append(s_int)

    # 2) 计算映射表 replace_map
    replace_map: Dict[int, int] = {}

    if total_slots >= 12:
        # 不映射：保留原始 slot
        replace_map = {s: s for s in orig_slots_in_order}
    else:
        # 紧凑映射到 1..total_slots
        if len(orig_slots_in_order) > total_slots:
            raise ValueError(
                f"Cannot compact-map {len(orig_slots_in_order)} distinct slots into total_slots={total_slots}."
            )
        # 依出现顺序映射：第1个 → 1，第2个 → 2，…
        replace_map = {s: i + 1 for i, s in enumerate(orig_slots_in_order)}

    # 3) 组装输出
    output = []
    container_char = ['wellplate', 'well', 'pcr']

    for lw in labware_list:
        class_name = (lw.get("type") or "").strip()
        if not class_name:
            raise ValueError(f"Labware item missing 'type': {lw}")
        # 清洗 class_name 中的点和特殊字符
        class_name = re.sub(r'\.', 'point', class_name)
        class_name = re.sub(r'[µμ]', 'u', class_name)

        # 默认体积
        liquid_vol = 200.0
        # 若名字看起来像盛液板，尝试解析体积
        if any(c in class_name.lower() for c in container_char):
            # 先小数：12.5ul / 0.5ml
            m = re.search(r'(\d+)\.(\d+)([mu]l)', class_name, re.IGNORECASE)
            if m:
                num1, num2, unit = m.groups()
                value = float(f"{num1}.{num2}")
                if unit.lower() == "ml":
                    liquid_vol = value * 1000.0
                else:  # 'ul'
                    liquid_vol = value
            else:
                # 再整数：200ul / 1ml
                m2 = re.search(r'(\d+)([mu]l)', class_name, re.IGNORECASE)
                if m2:
                    num, unit = m2.groups()
                    value = float(num)
                    if unit.lower() == "ml":
                        liquid_vol = value * 1000.0
                    else:  # 'ul'
                        liquid_vol = value

        # 计算新 slot
        orig_slot_raw = lw.get("slot")
        try:
            orig_slot = int(orig_slot_raw)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid slot value (must be convertible to int): {orig_slot_raw}")
        new_slot = replace_map.get(orig_slot)
        if new_slot is None:
            raise RuntimeError(f"Internal mapping error: slot {orig_slot} not in replace_map.")

        # 生成新 id：把 "on X" 改成 "on {new_slot}"，再把空格换成下划线
        prcxi_id = (lw.get("name") or "").strip()
        # 替换特殊字符
        prcxi_id = re.sub(r'[µμ]', 'u', prcxi_id)
        if not prcxi_id:
            # 没有名字就用类型占位，防止空
            prcxi_id = f"{class_name} on {orig_slot_raw}"
        new_id = re.sub(r'on \d+', f'on {new_slot}', prcxi_id)
        new_id = re.sub(r'\s+', '_', new_id)

        output.append({
            "id": new_id,
            "parent": "deck",
            "slot_on_deck": new_slot,
            "class_name": class_name,
            "liquid_type": [],
            "liquid_volume": [liquid_vol],
            "liquid_input_wells": []
        })
    # print('=== Lawbare Info ===')
    # pp.pprint(output)
    # pp.pprint(replace_map)

    return output, replace_map


def get_labware_data(protocol_name):
    """获取protocol的labware数据"""
    # 使用相对路径，从当前脚本所在目录找protoBuilds
    current_dir = os.path.dirname(os.path.abspath(__file__))
    base_dir = os.path.join(os.path.dirname(current_dir), "protoBuilds")
    
    # 先试标准命名
    standard_file = f"{base_dir}/{protocol_name}/{protocol_name}.ot2.apiv2.py.json"
    if os.path.exists(standard_file):
        with open(standard_file, "r") as f:
            return json.load(f)
    
    # 如果标准文件不存在，找其他json文件
    proto_dir = f"{base_dir}/{protocol_name}/"
    if not os.path.exists(proto_dir):
        raise FileNotFoundError(f"Protocol directory not found: {proto_dir}")
    
    for filename in os.listdir(proto_dir):
        if filename.endswith(".json") and filename not in ("metadata.json", "README.json"):
            with open(os.path.join(proto_dir, filename), "r") as f:
                return json.load(f)
    
    raise FileNotFoundError(f"No protocol json found in {proto_dir}")


def load_liquid_locations(protocol_name):
    """加载原始的liquid_locations映射"""
    detailed_action_file = f"./detailed_action_json/{protocol_name}.json"
    
    if not os.path.exists(detailed_action_file):
        print(f"  未找到 {detailed_action_file}，将使用自动生成的液体名称")
        return {}
    
    try:
        with open(detailed_action_file, "r") as f:
            data = json.load(f)
            liquid_locs = data.get("liquid_locations", {})
            
            # 构建从 (slot, well) 到变量名的映射
            well_to_varname = {}
            for var_name, loc_info in liquid_locs.items():
                slot = int(loc_info.get("slot", 0))
                well = loc_info.get("well", "")
                if slot and well:
                    # 清理变量名：去掉数组索引 [0], [1] 等
                    clean_name = re.sub(r'\[\d+\]$', '', var_name)
                    well_to_varname[(slot, well)] = clean_name
            
            print(f"  加载了 {len(well_to_varname)} 个原始试剂位置映射")
            return well_to_varname
            
    except Exception as e:
        print(f"  加载 liquid_locations 失败: {e}，将使用自动生成的液体名称")
        return {}


def process_protocol(protocol_name):
    """处理单个protocol，返回action_list和labware_data"""
    print(f"Processing {protocol_name}...")
    
    # 获取action_list - 寻找对应的steps文件
    steps_dir = "./steps/"
    
    # 先尝试找同名的steps文件
    steps_file = None
    possible_files = [
        f"{protocol_name}.json",
        f"{protocol_name}-steps.json", 
        f"steps-{protocol_name}.json"
    ]
    
    for filename in possible_files:
        if os.path.exists(os.path.join(steps_dir, filename)):
            steps_file = os.path.join(steps_dir, filename)
            break
    
    # 如果找不到同名文件，使用第一个json文件
    if not steps_file:
        steps_files = [f for f in os.listdir(steps_dir) if f.endswith(".json")]
        if not steps_files:
            raise FileNotFoundError("No steps json files found")
        steps_file = os.path.join(steps_dir, steps_files[0])
        print(f"  Warning: 使用默认steps文件: {steps_files[0]}")
    else:
        print(f"  找到对应steps文件: {os.path.basename(steps_file)}")
    
    # 先加载 labware 数据，以便 get_action_list 能按 tip rack 种类区分合并
    labware_json = get_labware_data(protocol_name)
    labware_info, replace_map = extract_labware_info_from_json(labware_json, 12)

    # 构建 slot -> labware_type 映射（供 get_action_list 使用）
    slot_to_labware_type: Dict[int, str] = {}
    for lab in (labware_json or {}).get("labware", []):
        slot = lab.get("slot")
        ltype = lab.get("type", "")
        if slot is not None and ltype:
            try:
                slot_to_labware_type[int(slot)] = ltype
            except (ValueError, TypeError):
                pass

    action_list = get_action_list(steps_file, slot_to_labware_type=slot_to_labware_type)

    return action_list, labware_info


def set_liquid_info(results, well_to_varname=None):
    """
    设置液体信息：为每个protocol的每个phase分配液体名称
    
    新逻辑：
    1. 同一个phase中所有被aspirate的孔位 = 同一种液体
    2. 同一个phase中所有被dispense的孔位 = 同一种液体
    3. 如果孔位已经在之前的phase中分配过液体，使用已有的液体名称
    4. 更新labware_info中的liquid_type和liquid_input_wells
    5. 优先使用原始的变量名（从well_to_varname映射），如果没有则使用Liquid_N格式
    """
    
    if well_to_varname is None:
        well_to_varname = {}
    
    for protocol_name, (action_list, labware_info) in results.items():
        print(f"处理 {protocol_name} 的液体信息...")
        
        # 跟踪每个孔位对应的液体名称: {(slot, well): liquid_name}
        well_to_liquid = {}
        
        # 液体计数器，用于生成唯一的液体名称
        liquid_counter = 1
        # 已使用的液体名称（用于避免重复）
        used_liquid_names = set()
        
        def get_liquid_name_for_well(slot, well):
            """为孔位获取液体名称，优先使用原始变量名"""
            well_key = (slot, well)
            
            # 如果已经分配过，直接返回
            if well_key in well_to_liquid:
                return well_to_liquid[well_key]
            
            # 尝试使用原始变量名
            if well_key in well_to_varname:
                original_name = well_to_varname[well_key]
                # 如果原始名称未被使用，直接用
                if original_name not in used_liquid_names:
                    used_liquid_names.add(original_name)
                    return original_name
                # 如果已被使用，添加后缀
                counter = 2
                while f"{original_name}_{counter}" in used_liquid_names:
                    counter += 1
                new_name = f"{original_name}_{counter}"
                used_liquid_names.add(new_name)
                return new_name
            
            # 没有原始名称，生成 Liquid_N 格式
            nonlocal liquid_counter
            while f"Liquid_{liquid_counter}" in used_liquid_names:
                liquid_counter += 1
            liquid_name = f"Liquid_{liquid_counter}"
            liquid_counter += 1
            used_liquid_names.add(liquid_name)
            return liquid_name
        
        # 遍历每个phase
        for phase_idx, action in enumerate(action_list):
            # 处理aspirate操作 - 每个action只有一个source well
            if action["aspirate"]:
                # 每个action只有一个source well，直接为其分配液体名称
                slot, well = action["aspirate"][0]  # 只有一个well
                well_key = (slot, well)
                aspirate_liquid_name = get_liquid_name_for_well(slot, well)
                well_to_liquid[well_key] = aspirate_liquid_name
                action["source_liquids"] = [aspirate_liquid_name]
                print(f"  源液体: {aspirate_liquid_name} ({slot}:{well})")
            else:
                action["source_liquids"] = []

            # 处理dispense操作 - 同一action中的所有dispense孔位共享同一液体
            if action["dispense"]:
                # 为这个action的所有dispense孔位分配相同的target液体
                # 使用一个固定的target名称，因为所有dispense都是到同一个地方
                target_liquid_name = "samples"
                if target_liquid_name not in used_liquid_names:
                    used_liquid_names.add(target_liquid_name)

                # 为这个action的所有dispense孔位分配相同的液体
                for slot, well in action["dispense"]:
                    well_key = (slot, well)
                    well_to_liquid[well_key] = target_liquid_name

                action["target_liquids"] = [target_liquid_name]
                print(f"  目标液体: {target_liquid_name} ({len(action['dispense'])} 个wells)")
            else:
                action["target_liquids"] = []
        
        # 更新labware_info中的液体信息
        for labware in labware_info:
            slot = labware["slot_on_deck"]

            # 为每个slot创建液体到wells的映射
            liquid_to_wells = {}

            for (well_slot, well), liquid_name in well_to_liquid.items():
                if well_slot == slot:
                    if liquid_name not in liquid_to_wells:
                        liquid_to_wells[liquid_name] = []
                    liquid_to_wells[liquid_name].append(well)

            # 更新labware的液体信息
            labware["liquid_type"] = list(liquid_to_wells.keys())
            # 对于多个液体的情况，我们需要展开所有wells
            all_wells = []
            for wells in liquid_to_wells.values():
                all_wells.extend(wells)
            labware["liquid_input_wells"] = all_wells

            if labware["liquid_type"]:
                print(f"  Labware {labware['id']}: {len(labware['liquid_type'])} 种液体在 {all_wells}")
        
        print(f"  {protocol_name}: 总共识别了 {len(used_liquid_names)} 种液体")
    
    return results




def process_all_protocols():
    """处理所有protocols"""
    original_dir = "/Users/guangxinzhang/Documents/Deep_Potential/opentrons/convert/protocols/original"
    
    # 获取所有protocol名称
    protocol_names = [d for d in os.listdir(original_dir) 
                     if os.path.isdir(os.path.join(original_dir, d))]
    
    results = {}
    errors = []
    
    for name in protocol_names:
        try:
            action_list, labware_data = process_protocol(name)
            results[name] = (action_list, labware_data)
            print(f"✓ {name} - success")
        except Exception as e:
            error_msg = f"✗ {name} - error: {str(e)}"
            print(error_msg)
            errors.append(error_msg)
    
    # 写错误日志
    if errors:
        os.makedirs("protocols/log", exist_ok=True)
        with open("protocols/log/error_converting.txt", "w") as f:
            for error in errors:
                f.write(f"{error}\n")
    
    print(f"\n完成处理: {len(results)} 成功, {len(errors)} 失败")
    return results




def generate_transfer_actions(protocol_name):
    """
    生成transfer_liquid格式的actions
    只包含同时有aspirate和dispense的有效phases
    """
    try:
        action_list, labware_info = process_protocol(protocol_name)
        results = {protocol_name: (action_list, labware_info)}
        
        # 加载原始试剂名称映射
        well_to_varname = load_liquid_locations(protocol_name)
        
        # 设置液体信息（静默处理）
        import sys
        from io import StringIO
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        results = set_liquid_info(results, well_to_varname)
        sys.stdout = old_stdout
        
        updated_action_list, updated_labware_info = results[protocol_name]
        
        # 生成有效的transfer actions
        transfer_actions = []
        
        for i, phase in enumerate(updated_action_list):
            # 跳过空的phase（没有aspirate或dispense）
            if not phase['aspirate'] or not phase['dispense']:
                continue
            
            # 确保有液体信息
            if not phase['source_liquids'] or not phase['target_liquids']:
                continue
            
            # 使用数组格式的体积和流速（标准格式要求）
            asp_vols = phase.get('asp_vols', [phase.get('asp_vol', 0)])
            dis_vols = phase.get('dis_vols', [phase.get('dis_vol', 0)])
            asp_flow_rates = phase.get('asp_flow_rates', [phase.get('asp_flow_rate', 0)])
            dis_flow_rates = phase.get('dis_flow_rates', [phase.get('dis_flow_rate', 0)])

            action_args = {
                "sources": phase['source_liquids'][0] if len(phase['source_liquids']) == 1 else phase['source_liquids'],
                "targets": phase['target_liquids'][0] if len(phase['target_liquids']) == 1 else phase['target_liquids'],
                "asp_vols": asp_vols,
                "dis_vols": dis_vols,
                "asp_flow_rates": asp_flow_rates,
                "dis_flow_rates": dis_flow_rates,
                "tip_racks": phase.get('tip_racks', [])
            }
            if phase.get('blow_out_air_volume_before'):
                action_args['blow_out_air_volume_before'] = phase['blow_out_air_volume_before']
            if phase.get('blow_out_air_volume'):
                action_args['blow_out_air_volume'] = phase['blow_out_air_volume']
            if phase.get('pre_aspirate_from_target'):
                action_args['pre_aspirate_from_target'] = phase['pre_aspirate_from_target']
            if phase.get('liquid_height'):
                action_args['liquid_height'] = phase['liquid_height']
            if phase.get('delays') and any(float(v or 0) > 0 for v in phase['delays']):
                action_args['delays'] = phase['delays']
            if phase.get('touch_tip'):
                action_args['touch_tip'] = True
            if phase.get('mix_stage'):
                action_args['mix_stage'] = phase['mix_stage']
                if phase.get('mix_times') is not None:
                    action_args['mix_times'] = phase['mix_times']
                if phase.get('mix_vol') is not None:
                    action_args['mix_vol'] = phase['mix_vol']
                if phase.get('mix_rate') is not None:
                    action_args['mix_rate'] = phase['mix_rate']
                if phase.get('mix_liquid_height') is not None:
                    action_args['mix_liquid_height'] = phase['mix_liquid_height']

            # 用于 simplify/merge 的 well 信息
            # phase['aspirate'] 可能是普通的单个 (slot, well) 元组，
            # 也可能是 consolidate 展开后的多个 (slot, well) 元组
            asp_list = phase.get('aspirate', [])
            dispense_list = phase.get('dispense', [])
            if asp_list:
                src_slot = asp_list[0][0]
                # 单孔吸、多孔打：重复 source well 与 dispense 等长，便于 _pair_mergeable / 贪心合并
                if len(asp_list) == 1 and len(dispense_list) > 1:
                    w = asp_list[0][1]
                    src_wells = [w] * len(dispense_list)
                else:
                    src_wells = [entry[1] for entry in asp_list]
            else:
                src_slot, src_wells = None, []
            tgt_slot = dispense_list[0][0] if dispense_list else None
            tgt_wells = [d[1] for d in dispense_list]

            action = {
                "action": "transfer_liquid",
                "action_args": action_args,
                "_source_slot": src_slot,
                "_source_wells": src_wells,
                "_target_slot": tgt_slot,
                "_target_wells": tgt_wells,
                "_tip_labware_type": phase.get('_tip_labware_type', ''),
            }

            transfer_actions.append(action)

        # 1. 简化：若多个1:1 transfer的source都是同一孔位，合并为1:N（如 l1[C1]->96个target）
        transfer_actions = _simplify_transfer_actions(transfer_actions)
        # 1b. 1:N -> N:N（重复 source well），否则 simplify 产出的 1:4 无法进入 merge
        transfer_actions = _normalize_pairing_wells_for_merge(transfer_actions)
        # 2. 合并：仅当slot相同、source相同、target相同时才合并
        transfer_actions = _merge_transfer_actions(transfer_actions)
        
        return transfer_actions, updated_labware_info
        
    except Exception as e:
        print(f"生成 transfer actions 失败: {e}")
        return [], []


def _simplify_transfer_actions(transfer_actions):
    """
    简化：若多个1:1 transfer的source都是同一孔位，合并为1:N（如 l1[C1]->96个target）。
    这样 l1[C1]->A1, l1[C1]->B1, ... 会合并成 l1[C1]->[A1,B1,...,96孔]
    """
    if len(transfer_actions) <= 1:
        return transfer_actions

    # 按 (source_slot, source_well, target_slot, tip_labware_type) 分组，只处理1:1的actions
    # tip rack 类型不同的 transfer 不能合并
    groups = {}
    non_simplifiable = []
    for action in transfer_actions:
        src_wells = action.get('_source_wells', [])
        tgt_wells = action.get('_target_wells', [])
        if len(src_wells) != 1 or len(tgt_wells) != 1:
            non_simplifiable.append(action)
            continue
        src_slot = action.get('_source_slot')
        tgt_slot = action.get('_target_slot')
        if src_slot is None or tgt_slot is None:
            non_simplifiable.append(action)
            continue
        tip_ltype = action.get('_tip_labware_type', '')
        key = (src_slot, src_wells[0], tgt_slot, tip_ltype)
        if key not in groups:
            groups[key] = []
        groups[key].append(action)

    simplified = list(non_simplifiable)
    for key, actions in groups.items():
        if len(actions) <= 1:
            simplified.extend(actions)
            continue
        # 合并为 1:N
        first = actions[0]
        args = first['action_args'].copy()
        args['asp_vols'] = []
        args['dis_vols'] = []
        args['asp_flow_rates'] = []
        args['dis_flow_rates'] = []
        if 'blow_out_air_volume' in first['action_args']:
            args['blow_out_air_volume'] = []
        if 'blow_out_air_volume_before' in first['action_args']:
            args['blow_out_air_volume_before'] = []
        if 'pre_aspirate_from_target' in first['action_args']:
            args['pre_aspirate_from_target'] = []
        if 'liquid_height' in first['action_args']:
            args['liquid_height'] = []
        args['delays'] = []
        has_nonzero_delay = False
        touch_tip_flags = []
        if first['action_args'].get('touch_tip'):
            touch_tip_flags.append(True)
        mix_stages = []
        mix_times_vals = []
        mix_vol_vals = []
        mix_rate_vals = []
        mix_height_vals = []

        source_wells = [first['_source_wells'][0]]
        target_wells = []
        for a in actions:
            target_wells.append(a['_target_wells'][0])
            args['asp_vols'].append(a['action_args']['asp_vols'][0])
            args['dis_vols'].append(a['action_args']['dis_vols'][0])
            args['asp_flow_rates'].append(a['action_args'].get('asp_flow_rates', [7.6])[0])
            args['dis_flow_rates'].append(a['action_args'].get('dis_flow_rates', [7.6])[0])
            if 'blow_out_air_volume' in a['action_args']:
                args['blow_out_air_volume'].append(a['action_args']['blow_out_air_volume'][0])
            if 'blow_out_air_volume_before' in a['action_args']:
                args['blow_out_air_volume_before'].append(a['action_args']['blow_out_air_volume_before'][0])
            if 'pre_aspirate_from_target' in a['action_args']:
                args['pre_aspirate_from_target'].append(a['action_args']['pre_aspirate_from_target'][0])
            if 'liquid_height' in a['action_args']:
                args['liquid_height'].append(a['action_args']['liquid_height'][0])
            delay_val = 0.0
            if 'delays' in a['action_args'] and a['action_args']['delays']:
                delay_val = float(a['action_args']['delays'][0] or 0.0)
            args['delays'].append(delay_val)
            if delay_val > 0:
                has_nonzero_delay = True
            if a['action_args'].get('touch_tip'):
                touch_tip_flags.append(True)
            if a['action_args'].get('mix_stage'):
                mix_stages.append(a['action_args'].get('mix_stage'))
                mix_times_vals.append(a['action_args'].get('mix_times'))
                mix_vol_vals.append(a['action_args'].get('mix_vol'))
                mix_rate_vals.append(a['action_args'].get('mix_rate'))
                mix_height_vals.append(a['action_args'].get('mix_liquid_height'))

        first_src = first['action_args']['sources']
        base_name = re.sub(r'_\d+$', '', first_src) if isinstance(first_src, str) else re.sub(r'_\d+$', '', first_src[0])
        first_tgt = first['action_args']['targets']
        tgt_base = re.sub(r'_\d+$', '', first_tgt) if isinstance(first_tgt, str) else re.sub(r'_\d+$', '', first_tgt[0])
        args['sources'] = base_name
        args['targets'] = tgt_base
        if not has_nonzero_delay:
            args.pop('delays', None)
        if any(touch_tip_flags):
            args['touch_tip'] = True
        merged_mix_stage = _merge_mix_stage(mix_stages)
        if merged_mix_stage:
            args['mix_stage'] = merged_mix_stage
            m_times = _min_or_none(mix_times_vals)
            m_vol = _min_or_none(mix_vol_vals)
            m_rate = _min_or_none(mix_rate_vals)
            m_height = _min_or_none(mix_height_vals)
            if m_times is not None:
                args['mix_times'] = int(m_times)
            if m_vol is not None:
                args['mix_vol'] = m_vol
            if m_rate is not None:
                args['mix_rate'] = m_rate
            if m_height is not None:
                args['mix_liquid_height'] = m_height

        simplified.append({
            "action": "transfer_liquid",
            "action_args": args,
            "_source_slot": first['_source_slot'],
            "_source_wells": source_wells,
            "_target_slot": first['_target_slot'],
            "_target_wells": target_wells
        })

    return simplified


def _normalize_pairing_wells_for_merge(transfer_actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将 1:N（单 source 孔、多 target 孔）展开为逐孔配对的 N:N，便于 _pair_mergeable。

    来源：原始 phase 的 1 吸多打，或 _simplify_transfer_actions 把多段 1:1 收成 1:N 后仍只保留一个 source well。
    """
    out: List[Dict[str, Any]] = []
    for action in transfer_actions:
        aw = action.get('_source_wells', [])
        tw = action.get('_target_wells', [])
        if len(aw) == 1 and len(tw) > 1:
            out.append({**action, '_source_wells': [aw[0]] * len(tw)})
        else:
            out.append(action)
    return out


def _pair_mergeable(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """相邻两条是否满足合并条件（与旧版全局合并相同的 slot / tip / 1:1 约束）。"""
    sa, ta = a.get('_source_slot'), a.get('_target_slot')
    sb, tb = b.get('_source_slot'), b.get('_target_slot')
    if sa is None or ta is None or sb is None or tb is None:
        return False
    if (sa, ta) != (sb, tb):
        return False
    if a.get('_tip_labware_type', '') != b.get('_tip_labware_type', ''):
        return False
    aw, tw = a.get('_source_wells', []), a.get('_target_wells', [])
    bw, uw = b.get('_source_wells', []), b.get('_target_wells', [])
    if len(aw) != len(tw) or len(bw) != len(uw):
        return False
    return True


def _merge_two_transfer_actions(first: Dict[str, Any], second: Dict[str, Any]) -> Dict[str, Any]:
    """将相邻两条 mergeable 的 transfer 合并为一条（字段处理与旧版多段合并一致）。"""
    args = first['action_args'].copy()
    target_wells = list(first.get('_target_wells', []))
    source_wells = list(first.get('_source_wells', []))
    first_src = first['action_args']['sources']
    base_name = re.sub(r'_\d+$', '', first_src) if isinstance(first_src, str) else re.sub(r'_\d+$', '', first_src[0])

    args['asp_vols'] = list(first['action_args']['asp_vols'])
    args['dis_vols'] = list(first['action_args']['dis_vols'])
    args['asp_flow_rates'] = list(first['action_args'].get('asp_flow_rates', []))
    args['dis_flow_rates'] = list(first['action_args'].get('dis_flow_rates', []))
    if 'blow_out_air_volume' in first['action_args']:
        args['blow_out_air_volume'] = list(first['action_args']['blow_out_air_volume'])
    if 'blow_out_air_volume_before' in first['action_args']:
        args['blow_out_air_volume_before'] = list(first['action_args']['blow_out_air_volume_before'])
    if 'pre_aspirate_from_target' in first['action_args']:
        args['pre_aspirate_from_target'] = list(first['action_args']['pre_aspirate_from_target'])
    if 'liquid_height' in first['action_args']:
        args['liquid_height'] = list(first['action_args']['liquid_height'])
    args['delays'] = []
    has_nonzero_delay = False
    first_delays = first['action_args'].get('delays')
    if first_delays:
        norm_first_delays = [float(v or 0.0) for v in first_delays]
    else:
        norm_first_delays = [0.0] * len(first['action_args'].get('dis_vols', []))
    args['delays'].extend(norm_first_delays)
    if any(v > 0 for v in norm_first_delays):
        has_nonzero_delay = True
    touch_tip_flags = []
    if first['action_args'].get('touch_tip'):
        touch_tip_flags.append(True)
    mix_stages = []
    mix_times_vals = []
    mix_vol_vals = []
    mix_rate_vals = []
    mix_height_vals = []
    if first['action_args'].get('mix_stage'):
        mix_stages.append(first['action_args'].get('mix_stage'))
        mix_times_vals.append(first['action_args'].get('mix_times'))
        mix_vol_vals.append(first['action_args'].get('mix_vol'))
        mix_rate_vals.append(first['action_args'].get('mix_rate'))
        mix_height_vals.append(first['action_args'].get('mix_liquid_height'))

    for a in (second,):
        target_wells.extend(a.get('_target_wells', []))
        source_wells.extend(a.get('_source_wells', []))
        args['asp_vols'].extend(a['action_args']['asp_vols'])
        args['dis_vols'].extend(a['action_args']['dis_vols'])
        args['asp_flow_rates'].extend(a['action_args'].get('asp_flow_rates', []))
        args['dis_flow_rates'].extend(a['action_args'].get('dis_flow_rates', []))
        if 'blow_out_air_volume' in a['action_args']:
            args['blow_out_air_volume'].extend(a['action_args']['blow_out_air_volume'])
        if 'blow_out_air_volume_before' in a['action_args']:
            args['blow_out_air_volume_before'].extend(a['action_args']['blow_out_air_volume_before'])
        if 'pre_aspirate_from_target' in a['action_args']:
            args['pre_aspirate_from_target'].extend(a['action_args']['pre_aspirate_from_target'])
        if 'liquid_height' in a['action_args']:
            args['liquid_height'].extend(a['action_args']['liquid_height'])
        cur_delays = a['action_args'].get('delays')
        if cur_delays:
            norm_cur_delays = [float(v or 0.0) for v in cur_delays]
        else:
            norm_cur_delays = [0.0] * len(a['action_args'].get('dis_vols', []))
        args['delays'].extend(norm_cur_delays)
        if any(v > 0 for v in norm_cur_delays):
            has_nonzero_delay = True
        if a['action_args'].get('touch_tip'):
            touch_tip_flags.append(True)
        if a['action_args'].get('mix_stage'):
            mix_stages.append(a['action_args'].get('mix_stage'))
            mix_times_vals.append(a['action_args'].get('mix_times'))
            mix_vol_vals.append(a['action_args'].get('mix_vol'))
            mix_rate_vals.append(a['action_args'].get('mix_rate'))
            mix_height_vals.append(a['action_args'].get('mix_liquid_height'))

    first_tgt = first['action_args']['targets']
    tgt_base = re.sub(r'_\d+$', '', first_tgt) if isinstance(first_tgt, str) else re.sub(r'_\d+$', '', first_tgt[0])
    args['sources'] = base_name
    args['targets'] = tgt_base
    if not has_nonzero_delay:
        args.pop('delays', None)
    if any(touch_tip_flags):
        args['touch_tip'] = True
    merged_mix_stage = _merge_mix_stage(mix_stages)
    if merged_mix_stage:
        args['mix_stage'] = merged_mix_stage
        m_times = _min_or_none(mix_times_vals)
        m_vol = _min_or_none(mix_vol_vals)
        m_rate = _min_or_none(mix_rate_vals)
        m_height = _min_or_none(mix_height_vals)
        if m_times is not None:
            args['mix_times'] = int(m_times)
        if m_vol is not None:
            args['mix_vol'] = m_vol
        if m_rate is not None:
            args['mix_rate'] = m_rate
        if m_height is not None:
            args['mix_liquid_height'] = m_height

    return {
        "action": "transfer_liquid",
        "action_args": args,
        "_source_slot": first.get('_source_slot'),
        "_source_wells": source_wells,
        "_target_slot": first.get('_target_slot'),
        "_target_wells": target_wells,
        "_tip_labware_type": first.get('_tip_labware_type', ''),
    }


def _merge_transfer_actions(transfer_actions):
    """合并相邻 mergeable 的连续段：单遍贪心，把每段塌成 1 条。

    扫到一对 mergeable 后，继续往后吃，直到遇到不可合并者；语义上等价于"迭代
    pairwise 到稳定"，但 O(N) 一遍即可，避免了 96→48→…→1 的多轮调用。
    """
    if len(transfer_actions) <= 1:
        return transfer_actions

    merged: List[Dict[str, Any]] = []
    i = 0
    n = len(transfer_actions)
    while i < n:
        cur = transfer_actions[i]
        j = i + 1
        while j < n and _pair_mergeable(cur, transfer_actions[j]):
            cur = _merge_two_transfer_actions(cur, transfer_actions[j])
            j += 1
        merged.append(cur)
        i = j
    return merged


def print_transfer_actions(protocol_name):
    """打印指定协议的transfer actions"""
    print(f"\n{'='*50}")
    print(f"协议: {protocol_name}")
    print(f"{'='*50}")
    
    transfer_actions, labware_info = generate_transfer_actions(protocol_name)
    
    if not transfer_actions:
        print("没有有效的transfer actions")
        return
    
    print(f"生成了 {len(transfer_actions)} 个有效actions:")
    
    for i, action in enumerate(transfer_actions, 1):
        print(f"\nAction {i}:")
        print(f"  {{")
        print(f"    \"action\": \"{action['action']}\",")
        print(f"    \"action_args\": {{")
        print(f"      \"sources\": \"{action['action_args']['sources']}\",")
        print(f"      \"targets\": \"{action['action_args']['targets']}\",")
        print(f"      \"asp_vols\": {action['action_args']['asp_vols']},")
        print(f"      \"dis_vols\": {action['action_args']['dis_vols']},")
        print(f"      \"asp_flow_rates\": {action['action_args']['asp_flow_rates']},")
        print(f"      \"dis_flow_rates\": {action['action_args']['dis_flow_rates']}")
        print(f"    }}")
        print(f"  }}")
    
    # 显示液体分布信息
    liquid_summary = {}
    for labware in labware_info:
        if labware['liquid_type']:
            for liquid in labware['liquid_type']:
                if liquid not in liquid_summary:
                    liquid_summary[liquid] = []
                liquid_summary[liquid].append(f"slot{labware['slot_on_deck']}")
    
    # 显示reagent信息
    print(f"\nReagent信息:")

    # 从原始liquid_locations映射中获取准确的reagent信息
    well_to_varname = load_liquid_locations(protocol_name)

    # 构建liquid到well的映射
    liquid_to_info = {}
    for (slot, well), liquid_name in well_to_varname.items():
        if liquid_name not in liquid_to_info:
            liquid_to_info[liquid_name] = {
                "slot": slot,
                "wells": [well],  # 直接设置单个well
                "labware": ""
            }
        else:
            # 如果已经存在，添加到列表中
            liquid_to_info[liquid_name]["wells"].append(well)

    # 为每个labware设置labware类型（使用type而不是name）
    # 获取protoBuilds中的原始JSON数据来获取type信息
    slot_to_type = {}
    try:
        proto_json = get_labware_data(protocol_name)
        if 'labware' in proto_json:
            for labware in proto_json['labware']:
                slot = int(labware.get('slot', 0))
                labware_type = labware.get('type', '')
                slot_to_type[slot] = labware_type
    except Exception as e:
        pass

    # 为每个labware设置labware类型
    for labware in labware_info:
        slot = labware['slot_on_deck']
        for liquid_info in liquid_to_info.values():
            if liquid_info["slot"] == slot:
                # 使用type而不是name
                liquid_info["labware"] = slot_to_type.get(slot, "")

    # 处理transfer_actions中的liquids
    for action in transfer_actions:
        sources = action['action_args']['sources']
        targets = action['action_args']['targets']

        if isinstance(sources, str):
            sources = [sources]
        if isinstance(targets, str):
            targets = [targets]

        all_liquids = sources + targets

        for liquid in all_liquids:
            if liquid not in liquid_to_info and liquid == "samples":
                # 对于samples，找到对应的wells
                for labware in labware_info:
                    if labware['liquid_type'] and "samples" in labware['liquid_type']:
                        liquid_to_info[liquid] = {
                            "slot": labware['slot_on_deck'],
                            "wells": labware['liquid_input_wells'] if labware['liquid_input_wells'] else [],
                            "labware": slot_to_type.get(labware['slot_on_deck'], "")
                        }
                        break

    # 添加tiprack信息
    try:
        proto_json = get_labware_data(protocol_name)
        if 'labware' in proto_json:
            for labware in proto_json['labware']:
                labware_type = labware.get('type', '').lower()
                # 检查是否是tiprack
                if 'tip' in labware_type and 'rack' in labware_type:
                    slot = int(labware.get('slot', 0))
                    labware_type_name = labware.get('type', '')

                    # 生成tiprack key，根据slot命名，如 tiprack_1, tiprack_2 等
                    tiprack_key = f"tiprack_{slot}"
                    liquid_to_info[tiprack_key] = {
                        "slot": slot,
                        "labware": labware_type_name
                    }
    except Exception as e:
        pass

    # 显示所有reagent信息
    for liquid, info in liquid_to_info.items():
        if 'wells' in info:
            wells_str = ', '.join(info['wells'][:3])  # 显示前3个wells
            if len(info['wells']) > 3:
                wells_str += f" (+{len(info['wells'])-3}个)"
            print(f"  {liquid}: slot{info['slot']} | {info['labware']} | wells: [{wells_str}]")
        else:
            # 对于tiprack等没有wells信息的项目
            print(f"  {liquid}: slot{info['slot']} | {info['labware']}")


def export_transfer_actions(protocol_name, output_file=None):
    """导出transfer actions到JSON文件"""
    transfer_actions, labware_info = generate_transfer_actions(protocol_name)
    
    if not transfer_actions:
        print(f"协议 {protocol_name} 没有有效的transfer actions")
        return
    
    # 生成reagent信息
    reagents = {}
    
    # 创建slot到labware的映射
    slot_to_labware = {}
    for labware in labware_info:
        slot_to_labware[labware['slot_on_deck']] = labware
    
    # 从原始liquid_locations映射中获取准确的reagent信息
    well_to_varname = load_liquid_locations(protocol_name)

    # 构建liquid到well的映射
    liquid_to_info = {}
    for (slot, well), liquid_name in well_to_varname.items():
        if liquid_name not in liquid_to_info:
            liquid_to_info[liquid_name] = {
                "slot": slot,
                "wells": [well],  # 直接设置单个well
                "labware": ""
            }
        else:
            # 如果已经存在，添加到列表中
            liquid_to_info[liquid_name]["wells"].append(well)

    # 为每个labware设置labware类型（使用type而不是name）
    # 获取protoBuilds中的原始JSON数据来获取type信息
    slot_to_type = {}
    try:
        proto_json = get_labware_data(protocol_name)
        if 'labware' in proto_json:
            for labware in proto_json['labware']:
                slot = int(labware.get('slot', 0))
                labware_type = labware.get('type', '')
                slot_to_type[slot] = labware_type
    except Exception as e:
        pass

    # 为每个labware设置labware类型
    for labware in labware_info:
        slot = labware['slot_on_deck']
        for liquid_info in liquid_to_info.values():
            if liquid_info["slot"] == slot:
                # 使用type而不是name
                liquid_info["labware"] = slot_to_type.get(slot, "")

    def _next_unique_key(base_name, counter_map):
        """为同名液体生成不与现有reagent冲突的新key。"""
        counter_map[base_name] = counter_map.get(base_name, 1) + 1
        new_key = f"{base_name}_{counter_map[base_name]}"
        while new_key in reagents:
            counter_map[base_name] += 1
            new_key = f"{base_name}_{counter_map[base_name]}"
        return new_key

    def _normalize_reagent_wells(wells):
        """reagent显示层：若全部是同一well，则合并为单个well。"""
        if not wells:
            return wells
        return [wells[0]] if len(set(wells)) == 1 else wells

    # 处理transfer_actions中的liquids：source液体使用action的source_wells，同液体不同wells分开写
    liquid_well_to_key = {}  # (liquid_base, slot, tuple(wells)) -> reagent_key
    liquid_key_counter = {}

    for action in transfer_actions:
        sources = action['action_args']['sources']
        targets = action['action_args']['targets']
        source_slot = action.get('_source_slot')
        source_wells = action.get('_source_wells', [])

        if isinstance(sources, str):
            sources = [sources]
        if isinstance(targets, str):
            targets = [targets]

        for liquid in sources:
            if source_slot is not None and source_wells:
                well_key = (liquid, source_slot, tuple(sorted(source_wells)))
                if well_key not in liquid_well_to_key:
                    if liquid not in reagents:
                        liquid_well_to_key[well_key] = liquid
                        reagents[liquid] = {
                            "slot": source_slot,
                            "well": _normalize_reagent_wells(source_wells),
                            "labware": slot_to_type.get(source_slot, ""),
                            "object": "source"
                        }
                    else:
                        new_key = _next_unique_key(liquid, liquid_key_counter)
                        liquid_well_to_key[well_key] = new_key
                        action['action_args']['sources'] = new_key
                        reagents[new_key] = {
                            "slot": source_slot,
                            "well": _normalize_reagent_wells(source_wells),
                            "labware": slot_to_type.get(source_slot, ""),
                            "object": "source"
                        }
                else:
                    action['action_args']['sources'] = liquid_well_to_key[well_key]
            elif liquid not in reagents and liquid in liquid_to_info:
                info = liquid_to_info[liquid]
                reagents[liquid] = {
                    "slot": info["slot"],
                    "well": info["wells"],
                    "labware": info["labware"],
                    "object": "source"
                }

    # 对于target液体（如"samples"），使用action的target_wells，同液体不同wells分开写
    target_well_to_key = {}
    target_key_counter = {}
    for action in transfer_actions:
        targets = action['action_args']['targets']
        target_slot = action.get('_target_slot')
        target_wells = action.get('_target_wells', [])

        if isinstance(targets, str):
            targets = [targets]

        for liquid in targets:
            if target_slot is not None and target_wells:
                well_key = (liquid, target_slot, tuple(sorted(target_wells)))
                if well_key not in target_well_to_key:
                    if liquid not in reagents:
                        target_well_to_key[well_key] = liquid
                        reagents[liquid] = {
                            "slot": target_slot,
                            "well": _normalize_reagent_wells(target_wells),
                            "labware": slot_to_type.get(target_slot, ""),
                            "object": "target"
                        }
                    else:
                        new_key = _next_unique_key(liquid, target_key_counter)
                        target_well_to_key[well_key] = new_key
                        action['action_args']['targets'] = new_key
                        reagents[new_key] = {
                            "slot": target_slot,
                            "well": _normalize_reagent_wells(target_wells),
                            "labware": slot_to_type.get(target_slot, ""),
                            "object": "target"
                        }
                else:
                    action['action_args']['targets'] = target_well_to_key[well_key]
            elif liquid not in reagents:
                for labware in labware_info:
                    if labware['liquid_type'] and liquid in labware['liquid_type']:
                        slot = labware['slot_on_deck']
                        wells = labware['liquid_input_wells']
                        reagents[liquid] = {
                            "slot": slot,
                            "well": wells,
                            "labware": slot_to_type.get(slot, "")
                        }
                        break

    # 添加 tiprack 和 trash 信息（从 protoBuilds 获取）
    try:
        proto_json = get_labware_data(protocol_name)
        slot_to_labware_type: Dict[int, str] = {}
        if 'labware' in proto_json:
            for labware in proto_json['labware']:
                labware_type = labware.get('type', '').lower()
                labware_slot = int(labware.get('slot', 0))
                slot_to_labware_type[labware_slot] = labware.get('type', '')
                # 检查是否是 tiprack
                if 'tip' in labware_type and 'rack' in labware_type:
                    tiprack_key = f"tiprack_{labware_slot}"
                    reagents[tiprack_key] = {
                        "slot": labware_slot,
                        "labware": labware.get('type', ''),
                        "object": "tiprack"
                    }
                # 检查是否是 trash（Opentrons Fixed Trash）
                elif 'trash' in labware_type or ('trash' in (labware.get('name') or '').lower()):
                    reagents["trash"] = {
                        "slot": labware_slot,
                        "labware": labware.get('type', 'opentrons_1_trash_1100ml_fixed'),
                        "object": "trash"
                    }
        # 补全 workflow 中引用但 reagent 中缺失的 tiprack（如非标准命名的 SPE 板被当做 tip 使用）
        for action in transfer_actions:
            tr = action.get('action_args', {}).get('tip_racks', '')
            if isinstance(tr, str) and tr.startswith('tiprack_'):
                try:
                    tip_slot = int(tr.split('_', 1)[1])
                    tip_key = f"tiprack_{tip_slot}"
                    if tip_key not in reagents and tip_slot in slot_to_labware_type:
                        reagents[tip_key] = {
                            "slot": tip_slot,
                            "labware": slot_to_labware_type[tip_slot],
                            "object": "tiprack"
                        }
                except (ValueError, IndexError):
                    pass
        # 若 protoBuilds 中未找到 trash，添加默认 trash（slot 12）
        if "trash" not in reagents:
            reagents["trash"] = {
                "slot": 12,
                "labware": "opentrons_1_trash_1100ml_fixed",
                "object": "trash"
            }
    except Exception as e:
        print(f"  加载protoBuilds数据失败: {e}")
        # 即使失败也添加默认 trash
        reagents["trash"] = {
            "slot": 12,
            "labware": "opentrons_1_trash_1100ml_fixed",
            "object": "trash"
        }

    # 移除内部字段，不输出到JSON
    for action in transfer_actions:
        action.pop('_source_slot', None)
        action.pop('_source_wells', None)
        action.pop('_target_slot', None)
        action.pop('_target_wells', None)
        action.pop('_tip_labware_type', None)

    output_data = {
        "workflow": transfer_actions,
        "reagent": reagents
    }

    if output_file is None:
        output_file = f"{protocol_name}_transfer_actions.json"
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)
    
    print(f"Transfer actions已导出到: {output_file}")
    return output_data


def batch_generate_transfer_actions(output_dir="transfer_actions"):
    """批量生成所有协议的transfer actions"""
    import os
    
    steps_dir = "./steps/"
    if not os.path.exists(steps_dir):
        print("steps目录不存在")
        return
    
    # 创建输出目录
    os.makedirs(output_dir, exist_ok=True)
    
    # 获取所有协议
    protocols = [f.replace('.json', '') for f in os.listdir(steps_dir) if f.endswith('.json')]
    
    print(f"发现 {len(protocols)} 个协议")
    
    success_count = 0
    results_summary = []
    
    for i, protocol in enumerate(protocols, 1):
        try:
            print(f"[{i}/{len(protocols)}] 处理 {protocol}...")
            
            transfer_actions, labware_info = generate_transfer_actions(protocol)
            
            if not transfer_actions:
                print(f"  跳过 - 没有有效actions")
                continue
            
            # 导出单个协议的actions，文件名就是方案名.json
            output_file = os.path.join(output_dir, f"{protocol}.json")
            export_data = export_transfer_actions(protocol, output_file)
            
            # 计算液体种类数量
            all_liquids = set([action['action_args']['sources'] for action in transfer_actions] + 
                             [action['action_args']['targets'] for action in transfer_actions])
            
            results_summary.append({
                "protocol": protocol,
                "actions_count": len(transfer_actions),
                "liquids_count": len(all_liquids)
            })
            
            success_count += 1
            print(f"  成功 - {len(transfer_actions)} actions")
            
        except Exception as e:
            print(f"  失败: {e}")
            continue
    
    # 生成总览文件
    summary_file = os.path.join(output_dir, "batch_summary.json")
    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump({
            "total_protocols": len(protocols),
            "successful_protocols": success_count,
            "results": results_summary
        }, f, indent=2, ensure_ascii=False)
    
    print(f"\n批量处理完成!")
    print(f"  成功: {success_count}/{len(protocols)}")
    print(f"  输出目录: {output_dir}")
    print(f"  总览文件: {summary_file}")


if __name__ == "__main__":
    # 选择运行模式
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "batch":
        # 批量模式 - 支持自定义输出目录
        output_dir = sys.argv[2] if len(sys.argv) > 2 else "transfer_actions"
        batch_generate_transfer_actions(output_dir)
    else:
        # 示例模式
        output_dir = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "batch" else "transfer_actions_copy4"
        if len(sys.argv) > 1 and sys.argv[1] == "batch":
            output_dir = sys.argv[2] if len(sys.argv) > 2 else "transfer_actions_copy4"
        batch_generate_transfer_actions(output_dir)
