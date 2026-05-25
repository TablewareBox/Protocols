"""P2 跨 slot transfer_liquid 合并 —— Stage 2 单元测试。

对应 ``product_designs/protocol_convert/02-cross-slot-merge.md`` §9（v2 推荐方案）+ §9.5 step 6.1。

覆盖范围
--------
- ``_pair_mergeable`` 放宽 target_slot 约束（仅要求 source slot + tip 维度一致）。
- ``_simplify_transfer_actions`` 分组键去 ``tgt_slot``，单源 1:1 列表跨多 target slot 也能塌成 1:N。
- ``_merge_two_transfer_actions`` 跨 slot 合并时携带 ``_target_slots: list[int]``
  与 ``_target_wells`` 平行（每次 dispense 一条），用于 export 阶段反查 reagent_key。
- ``export_transfer_actions``：
  - 跨 slot ⇒ ``action_args.targets`` 为 ``list[str]``，每元素是 reagent_key（不同 slot 不同 key）。
  - 单 slot 退化 ⇒ ``targets`` 仍为 ``str``（兼容旧协议）。
  - ``reagent`` 块独立持有每个 reagent_key 的 ``slot/well/labware``，**任何 ``_`` 前缀辅助字段
    都 ``pop`` 掉，不进入 JSON 输出**。

本文件**只导入**纯函数 ``_pair_mergeable / _simplify_transfer_actions /
_merge_two_transfer_actions``，不触碰 Opentrons 模拟运行链（避免重型依赖）。
``export_transfer_actions`` 的端到端验证放在 batch 回归脚本里。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List


# 让 import 找到 Protocols/protocol_converter/change_to_transfer_group.py
ROOT_DIR = Path(__file__).resolve().parents[3]
PROTOCOL_DIR = ROOT_DIR / "Protocols" / "protocol_converter"
if str(PROTOCOL_DIR) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_DIR))

from change_to_transfer_group import (  # noqa: E402
    _merge_transfer_actions,
    _merge_two_transfer_actions,
    _pair_mergeable,
    _simplify_transfer_actions,
)


# ==================== 工具函数：构造最小 transfer action ====================


def _make_one_to_one_action(
    *,
    source_slot: int,
    source_well: str,
    target_slot: int,
    target_well: str,
    sources_name: str = "l1",
    targets_name: str = "plate",
    tip_ltype: str = "opentrons_96_tiprack_300ul",
    asp_vol: float = 8.3,
    dis_vol: float = 8.3,
    use_channels: Any = None,
) -> Dict[str, Any]:
    """构造一条已经经过 simplify / pre-merge 之前形态的 1:1 transfer action。

    参数完全显式，方便单测构造跨 slot 序列。
    """
    args: Dict[str, Any] = {
        "sources": sources_name,
        "targets": targets_name,
        "asp_vols": [asp_vol],
        "dis_vols": [dis_vol],
        "asp_flow_rates": [7.6],
        "dis_flow_rates": [7.6],
        "tip_racks": f"tiprack_{target_slot}",
    }
    if use_channels is not None:
        args["use_channels"] = list(use_channels)
    return {
        "action": "transfer_liquid",
        "action_args": args,
        "_source_slot": source_slot,
        "_source_wells": [source_well],
        "_target_slot": target_slot,
        "_target_wells": [target_well],
        "_tip_labware_type": tip_ltype,
    }


# ==================== §9.5 step 6.1（P2 v2 测试）====================


class TestPairMergeable:
    """``_pair_mergeable`` 放宽 ``_target_slot`` 约束（v2 设计 §3.1.1 / §9.5）。"""

    def test_same_source_same_target_slot_mergeable(self):
        """v1 已有行为：同 source slot + 同 target slot 可合并。"""
        a = _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=2, target_well="A1")
        b = _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=2, target_well="B1")
        assert _pair_mergeable(a, b) is True

    def test_same_source_different_target_slot_mergeable(self):
        """P2 v2 关键：源 slot 相同、target slot 不同也可合并（跨 slot）。"""
        a = _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=2, target_well="A1")
        b = _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=3, target_well="A1")
        assert _pair_mergeable(a, b) is True

    def test_different_source_slot_not_mergeable(self):
        """源 slot 不同绝不合并（v1 / v2 一致约束）。"""
        a = _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=2, target_well="A1")
        b = _make_one_to_one_action(source_slot=4, source_well="A1", target_slot=2, target_well="A1")
        assert _pair_mergeable(a, b) is False

    def test_different_tip_labware_type_not_mergeable(self):
        """tip 类型不同（量程档不一致）不合并 —— P6 兜底约束。"""
        a = _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=2, target_well="A1",
                                    tip_ltype="opentrons_96_tiprack_300ul")
        b = _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=3, target_well="A1",
                                    tip_ltype="opentrons_96_tiprack_1000ul")
        assert _pair_mergeable(a, b) is False

    def test_use_channels_mismatch_not_mergeable(self):
        """multi / single 通道不同不可合并（P1 v4 约束）。"""
        a = _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=2, target_well="A1",
                                    use_channels=[0])
        b = _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=3, target_well="A1",
                                    use_channels=list(range(8)))
        assert _pair_mergeable(a, b) is False

    def test_same_source_slot_different_source_wells_not_mergeable(self):
        """P2 v2 §12.6.1 修复：source slot 相同但 source well 不同 → 禁止合并。

        51b9a5 核心场景：src A1 序列与 src A2 序列即便落到相邻 phase，也必须保持独立
        transfer，否则 runtime 无法重构「source.A_i ↔ target.A_i 列锚定」对应关系。
        """
        a = _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=2, target_well="A1")
        b = _make_one_to_one_action(source_slot=1, source_well="A2", target_slot=2, target_well="A2")
        assert _pair_mergeable(a, b) is False, "src A1 与 src A2 是不同 source well，必须独立"

    def test_same_source_wells_sequence_mergeable_across_target_slots(self):
        """P2 v2 §12.6.1 修复后正样本：两条 transfer 的 source_wells 完全等价（哪怕
        是 ``[A1] * N`` 的扩展形态），跨多个 target slot 仍允许合并 —— 这是 51b9a5
        修复后真正期望的合并形态：一组「单源 → 跨 N 板同位置」的 1:N 与下一组同样
        「单源 → 跨 N 板同位置」 **不能跨源合并**，但**同源跨板可合并**。
        """
        a = _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=2, target_well="A1")
        b = _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=3, target_well="A1")
        assert _pair_mergeable(a, b) is True, "同 source_wells（[A1]）跨 target slot 应允许合并"


class TestSimplifyAcrossTargetSlot:
    """``_simplify_transfer_actions`` 分组键去 ``tgt_slot``（v2 设计 §3.1.2）。

    源 slot + 源 well + tip + use_channels 相同的多条 1:1 应被收成单条 1:N，
    无论 target 落到哪个 slot。
    """

    def test_simplify_collapses_cross_slot_one_to_one(self):
        """51b9a5 主场景：l1[A1] -> slot2/3/5/6/A1 四次 1:1 → 单条 1:4。"""
        actions = [
            _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=slot, target_well="A1")
            for slot in (2, 3, 5, 6)
        ]
        simplified = _simplify_transfer_actions(actions)
        assert len(simplified) == 1, "跨 4 个 target slot 的 1:1 应被合并为单条 1:4"
        merged = simplified[0]
        # asp_vols / dis_vols 各 4 项
        assert len(merged["action_args"]["asp_vols"]) == 4
        assert len(merged["action_args"]["dis_vols"]) == 4
        # 内部辅助字段：_target_wells 长度 4
        assert len(merged["_target_wells"]) == 4
        # _target_slots（v2 新增）：与 _target_wells 平行，记录每次 dispense 的目标 slot
        assert merged.get("_target_slots") == [2, 3, 5, 6], (
            "v2：_target_slots 必须按 dispense 顺序记录每条 1:1 的目标 slot"
        )

    def test_simplify_keeps_separate_groups_by_use_channels(self):
        """同源同 tip 但 use_channels 不同：分两组，不合并。"""
        actions = [
            _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=2, target_well="A1",
                                    use_channels=[0]),
            _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=3, target_well="A1",
                                    use_channels=[0]),
            _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=2, target_well="B1",
                                    use_channels=list(range(8))),
            _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=3, target_well="B1",
                                    use_channels=list(range(8))),
        ]
        simplified = _simplify_transfer_actions(actions)
        # 期望两组：单通道（slot 2、slot 3 → 合 1 条）+ 8 通道（slot 2、slot 3 → 合 1 条）
        assert len(simplified) == 2


class TestMergeTwoActionsCrossSlot:
    """``_merge_two_transfer_actions`` 跨 slot 合并时记录 ``_target_slots`` 序列（v2 §3.1.3）。"""

    def test_merge_two_different_target_slots(self):
        a = _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=2, target_well="A1")
        b = _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=3, target_well="A1")
        merged = _merge_two_transfer_actions(a, b)
        assert merged["_source_slot"] == 1
        assert merged["_target_wells"] == ["A1", "A1"]
        assert merged["_target_slots"] == [2, 3]
        # dis_vols / asp_vols 拼接
        assert merged["action_args"]["asp_vols"] == [8.3, 8.3]
        assert merged["action_args"]["dis_vols"] == [8.3, 8.3]

    def test_merge_chain_four_target_slots(self):
        """连续 4 条跨 slot 走 _merge_transfer_actions 主路径，应塌成 1 条。"""
        actions = [
            _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=slot, target_well="A1")
            for slot in (2, 3, 5, 6)
        ]
        merged = _merge_transfer_actions(actions)
        assert len(merged) == 1
        only = merged[0]
        assert only["_target_slots"] == [2, 3, 5, 6]
        assert only["_target_wells"] == ["A1", "A1", "A1", "A1"]
        assert len(only["action_args"]["dis_vols"]) == 4

    def test_merge_same_slot_preserves_legacy_target_slots(self):
        """同 slot 合并：_target_slots 仍逐项记录（全是同一 slot），保证导出阶段算法统一。"""
        a = _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=2, target_well="A1")
        b = _make_one_to_one_action(source_slot=1, source_well="A1", target_slot=2, target_well="B1")
        merged = _merge_two_transfer_actions(a, b)
        assert merged["_target_slots"] == [2, 2]
        # backward compat：_target_slot 标量仍为首条 slot（不破坏现有 export 单 slot 退化路径）
        assert merged["_target_slot"] == 2


# ==================== §12.6.1 修复后端到端形态：51b9a5 风格 ====================


class TestEndToEnd51b9a5Shape:
    """模拟 51b9a5 物理动作进入 simplify → merge_transfer_actions 管线，验证最终形态。

    51b9a5 物理动作摘要（详见 ``02-cross-slot-merge.md`` §12.2）：
    - source plate slot 2，12 孔（A1..A12）
    - 9 个 target plates（slot 3..11），每板 12 孔
    - 模式：src.A_i → 9 个 target plate 的 A_i 位置（列锚定）

    修复后预期形态：12 个 transfer_liquid，每个跨 9 板（list-targets 长 9）。
    """

    def _build_actions_51b9a5_first_two_columns(self) -> List[Dict[str, Any]]:
        """构造 51b9a5 前两列（A1、A2）的 18 条 1:1 transfer actions。"""
        out: List[Dict[str, Any]] = []
        for src_well in ("A1", "A2"):
            for tgt_slot in range(3, 12):  # slot 3..11 共 9 板
                out.append(
                    _make_one_to_one_action(
                        source_slot=2,
                        source_well=src_well,
                        target_slot=tgt_slot,
                        target_well=src_well,  # 列锚定：tgt 位置 = src 位置
                        sources_name="sources",
                        targets_name="samples",
                        asp_vol=3.0,
                        dis_vol=3.0,
                        tip_ltype="opentrons_96_tiprack_20ul",
                    )
                )
        return out

    def test_51b9a5_two_columns_yields_two_actions(self):
        """A1 一列 + A2 一列共 18 条 1:1 → simplify 后应为 2 条（每列 1 条 1:9）。"""
        actions = self._build_actions_51b9a5_first_two_columns()
        simplified = _simplify_transfer_actions(actions)
        assert len(simplified) == 2, (
            f"应得 2 条 transfer_liquid（A1 跨 9 板 + A2 跨 9 板），实际 {len(simplified)} 条"
        )

        # 第 1 条：source A1 跨 9 板
        a1_action = next(a for a in simplified if a["_source_wells"] == ["A1"])
        assert len(a1_action["_target_wells"]) == 9
        assert a1_action["_target_wells"] == ["A1"] * 9
        assert a1_action["_target_slots"] == list(range(3, 12))

        # 第 2 条：source A2 跨 9 板
        a2_action = next(a for a in simplified if a["_source_wells"] == ["A2"])
        assert len(a2_action["_target_wells"]) == 9
        assert a2_action["_target_wells"] == ["A2"] * 9
        assert a2_action["_target_slots"] == list(range(3, 12))

    def test_51b9a5_two_columns_not_cross_merged_after_pair_mergeable(self):
        """关键修复验证：simplify 出的 2 条（A1 序列、A2 序列）经 _merge_transfer_actions
        管线后不应坍缩到 1 条 —— _pair_mergeable 应拒绝跨 source well 的合并。
        """
        actions = self._build_actions_51b9a5_first_two_columns()
        simplified = _simplify_transfer_actions(actions)
        # 模拟 pipeline 中 simplify → _normalize_pairing_wells_for_merge → _merge_transfer_actions
        # 这里直接调最终合并函数（已包含上游的 normalize 等效行为：每条 _source_wells
        # 已经是 [single_well]，扩展为 [single_well] * N 后两条仍然不等）。
        from change_to_transfer_group import _normalize_pairing_wells_for_merge
        normalized = _normalize_pairing_wells_for_merge(simplified)
        merged = _merge_transfer_actions(normalized)
        assert len(merged) == 2, (
            f"§12.6.1 修复：A1 序列与 A2 序列不应跨 source well 合并，"
            f"实际 {len(merged)} 条（修复前 v2 错误会得到 1 条）"
        )
