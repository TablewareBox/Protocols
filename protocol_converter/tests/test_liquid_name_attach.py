"""P8 — Stage 2 ``_attach_liquid_name_to_reagents`` 单元测试。

对应 ``product_designs/protocol_convert/08-liquid-name-from-reagent-block.md`` §3.3 + §5。

覆盖范围
--------
- ``_attach_liquid_name_to_reagents`` 注入逻辑：
  - ``object="source" / "target"`` 才注入；``tiprack`` / ``trash`` 不写。
  - ``(slot, well)`` 命中 ``well_to_liquid_name`` → 写入；命中失败 → 字段省略。
  - 多 well reagent 取**首个**命中（典型场景：多通道整列扩展后取 A1）。
  - 已显式写过 ``liquid_name`` 的 entry 不覆盖（但 ``sources_2`` 这类系统占位名可被覆盖）。
  - 空 / None / 非 dict 输入 graceful 处理。
- ``load_liquid_locations`` 第 3 返回值 ``well_to_liquid_name``：
  - 仅当 detailed_action_json 的 ``liquid_locations[*].liquid_name`` 字段存在
    时填充；Python 变量名 fallback 不污染。
  - 内容是 raw 字符串（含空格 / 括号 / 中文），未经过 ``_to_reagent_key`` normalize。

本测试**不**触碰 Opentrons mock / 协议执行 / 文件 IO 重链路，仅测纯函数行为。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest


ROOT_DIR = Path(__file__).resolve().parents[3]
PROTOCOL_DIR = ROOT_DIR / "Protocols" / "protocol_converter"
if str(PROTOCOL_DIR) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_DIR))

from change_to_transfer_group import (  # noqa: E402
    _attach_liquid_name_to_reagents,
    load_liquid_locations,
)


# ==================== _attach_liquid_name_to_reagents ====================


class TestAttachLiquidNameToReagents:
    """注入器纯函数行为。"""

    def test_source_and_target_get_liquid_name(self):
        """source / target 类 entry 命中 (slot, well) → 注入 liquid_name。"""
        reagents: Dict[str, Dict[str, Any]] = {
            "src_1": {
                "slot": 2,
                "well": ["A1"],
                "labware": "nest_96_wellplate_100ul_pcr_full_skirt",
                "object": "source",
            },
            "tgt_1": {
                "slot": 5,
                "well": ["A1"],
                "labware": "nest_96_wellplate_2ml_deep",
                "object": "target",
            },
        }
        well_to_liquid_name = {
            (2, "A1"): "EDTA Plasma",
            (5, "A1"): "PBS Diluent",
        }
        _attach_liquid_name_to_reagents(reagents, well_to_liquid_name)
        assert reagents["src_1"]["liquid_name"] == "EDTA Plasma"
        assert reagents["tgt_1"]["liquid_name"] == "PBS Diluent"

    def test_tiprack_and_trash_skipped(self):
        """tiprack / trash 类 entry 不应被注入。"""
        reagents: Dict[str, Dict[str, Any]] = {
            "tiprack_12": {
                "slot": 12,
                "labware": "opentrons_96_tiprack_300ul",
                "object": "tiprack",
            },
            "trash": {
                "slot": 12,
                "labware": "opentrons_1_trash_1100ml_fixed",
                "object": "trash",
            },
        }
        # 即使 (12, "A1") 命中也不应注入 tiprack / trash
        well_to_liquid_name = {(12, "A1"): "Should Not Appear"}
        _attach_liquid_name_to_reagents(reagents, well_to_liquid_name)
        assert "liquid_name" not in reagents["tiprack_12"]
        assert "liquid_name" not in reagents["trash"]

    def test_no_explicit_hit_falls_back_to_well_to_varname(self):
        """``well_to_liquid_name`` 未命中时，使用非泛化 ``well_to_varname`` 作为 fallback。"""
        reagents: Dict[str, Dict[str, Any]] = {
            "src_1": {
                "slot": 2,
                "well": ["A1"],
                "labware": "x",
                "object": "source",
            },
        }
        well_to_liquid_name = {(99, "Z99"): "Anything"}
        well_to_varname = {(2, "A1"): "plasma"}
        _attach_liquid_name_to_reagents(
            reagents, well_to_liquid_name, well_to_varname=well_to_varname
        )
        assert reagents["src_1"]["liquid_name"] == "plasma"

    def test_generic_well_to_varname_falls_back_to_liquid_n(self):
        """v1.3 起：``sources``/``samples`` 这类泛化名被跳过 + reagent_key 也是系统占位名
        → 兜底走 ``Liquid_<idx>`` 自增（而非 reagent_key 原样）。
        """
        reagents: Dict[str, Dict[str, Any]] = {
            "sources_6": {
                "slot": 2,
                "well": ["A6"],
                "labware": "x",
                "object": "source",
            },
        }
        well_to_varname = {(2, "A6"): "sources"}  # 泛化变量名（来自 sources[5]）
        _attach_liquid_name_to_reagents(
            reagents, {}, well_to_varname=well_to_varname
        )
        # v1.3：无任何信号源命中时使用 Liquid_<n> 兜底（_derive_liquid_name_from_context
        # 因 workflow_name 为空也返回 ""）。
        assert reagents["sources_6"]["liquid_name"] == "Liquid_1"

    def test_no_explicit_and_no_varname_falls_back_to_liquid_n(self):
        """v1.3 起：explicit/varname/.py/README/context 全部缺失时走 ``Liquid_<idx>`` 兜底。"""
        reagents: Dict[str, Dict[str, Any]] = {
            "sources_2": {
                "slot": 2,
                "well": ["A1"],
                "labware": "x",
                "object": "source",
            },
        }
        _attach_liquid_name_to_reagents(reagents, {})
        assert reagents["sources_2"]["liquid_name"] == "Liquid_1"

    def test_first_well_hit_wins_for_multi_well_reagent(self):
        """reagent 含多 well（如 P1 多通道整列扩展）→ 取首个命中。"""
        reagents: Dict[str, Dict[str, Any]] = {
            "src_1": {
                "slot": 1,
                "well": ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"],
                "labware": "x",
                "object": "source",
            },
        }
        well_to_liquid_name = {
            (1, "A1"): "Mastermix",
            (1, "B1"): "Should Not Appear",
        }
        _attach_liquid_name_to_reagents(reagents, well_to_liquid_name)
        assert reagents["src_1"]["liquid_name"] == "Mastermix"

    def test_falls_through_to_later_well_when_first_misses(self):
        """首个 well 没命中、第二个命中 → 取第二个。"""
        reagents: Dict[str, Dict[str, Any]] = {
            "src_1": {
                "slot": 1,
                "well": ["A1", "B1"],
                "labware": "x",
                "object": "source",
            },
        }
        well_to_liquid_name = {(1, "B1"): "Buffer B"}
        _attach_liquid_name_to_reagents(reagents, well_to_liquid_name)
        assert reagents["src_1"]["liquid_name"] == "Buffer B"

    def test_existing_liquid_name_not_overwritten(self):
        """entry 已显式标注真实 liquid_name → 不覆盖（保留更高优先级注入器结果）。"""
        reagents: Dict[str, Dict[str, Any]] = {
            "src_1": {
                "slot": 2,
                "well": ["A1"],
                "labware": "x",
                "object": "source",
                "liquid_name": "Original Name",
            },
        }
        well_to_liquid_name = {(2, "A1"): "Should Not Overwrite"}
        _attach_liquid_name_to_reagents(reagents, well_to_liquid_name)
        assert reagents["src_1"]["liquid_name"] == "Original Name"

    def test_system_generated_existing_name_can_be_overwritten(self):
        """entry 的 liquid_name 若是系统占位名（如 ``sources_2``）应允许覆盖。"""
        reagents: Dict[str, Dict[str, Any]] = {
            "sources_2": {
                "slot": 2,
                "well": ["A1"],
                "labware": "x",
                "object": "source",
                "liquid_name": "sources_2",
            },
        }
        well_to_varname = {(2, "A1"): "plasma"}
        _attach_liquid_name_to_reagents(reagents, {}, well_to_varname=well_to_varname)
        assert reagents["sources_2"]["liquid_name"] == "plasma"

    def test_empty_or_none_inputs_graceful(self):
        """空 dict / None 不应报错；在无任何信号源时走 ``Liquid_<idx>`` 兜底（v1.3）。"""
        reagents: Dict[str, Dict[str, Any]] = {
            "src_1": {
                "slot": 2,
                "well": ["A1"],
                "labware": "x",
                "object": "source",
            },
        }
        # None / 空 well_to_liquid_name
        _attach_liquid_name_to_reagents(reagents, {})
        # v1.3：所有信号源均 miss → Liquid_<n> 自增兜底
        assert reagents["src_1"]["liquid_name"] == "Liquid_1"
        # 空 reagents 不报错
        _attach_liquid_name_to_reagents({}, {(1, "A1"): "X"})

    def test_invalid_slot_still_gets_liquid_n_fallback(self):
        """v1.3 起：source/target entry 始终保证非空 liquid_name；slot 非法时
        前几条信号源 miss，但 priority 6 ``Liquid_<idx>`` 仍兜底写入。
        """
        reagents: Dict[str, Dict[str, Any]] = {
            "src_1": {
                "slot": None,
                "well": ["A1"],
                "labware": "x",
                "object": "source",
            },
            "src_2": {
                "slot": "invalid",
                "well": ["A1"],
                "labware": "x",
                "object": "source",
            },
        }
        well_to_liquid_name = {(0, "A1"): "Anything"}
        _attach_liquid_name_to_reagents(reagents, well_to_liquid_name)
        # v1.3：即便 slot 解析失败，source entry 仍获 Liquid_<n> 兜底
        assert reagents["src_1"]["liquid_name"] == "Liquid_1"
        assert reagents["src_2"]["liquid_name"] == "Liquid_2"

    def test_str_well_form_accepted(self):
        """部分历史 reagent 块的 well 字段可能是 str 而非 list[str] → 兼容处理。"""
        reagents: Dict[str, Dict[str, Any]] = {
            "src_1": {
                "slot": 2,
                "well": "A1",  # str 形态（非典型，但应兼容）
                "labware": "x",
                "object": "source",
            },
        }
        well_to_liquid_name = {(2, "A1"): "Plasma"}
        _attach_liquid_name_to_reagents(reagents, well_to_liquid_name)
        assert reagents["src_1"]["liquid_name"] == "Plasma"

    def test_preserves_spaces_and_unicode(self):
        """注入的字符串保留原字符（空格 / 括号 / 中文）。"""
        reagents: Dict[str, Dict[str, Any]] = {
            "src_1": {
                "slot": 2,
                "well": ["A1"],
                "labware": "x",
                "object": "source",
            },
        }
        well_to_liquid_name = {(2, "A1"): "Tris HCl pH 8.0 (1×)  缓冲液"}
        _attach_liquid_name_to_reagents(reagents, well_to_liquid_name)
        assert reagents["src_1"]["liquid_name"] == "Tris HCl pH 8.0 (1×)  缓冲液"


# ==================== load_liquid_locations 第 3 返回值 ====================


class TestLoadLiquidLocationsThirdReturn:
    """``load_liquid_locations`` 返回的 ``well_to_liquid_name`` 行为。"""

    @pytest.fixture
    def isolated_detailed_action_json(self, tmp_path, monkeypatch):
        """构造临时 detailed_action_json/<protocol>.json，使 load_liquid_locations 能读取。

        Returns:
            (protocol_name, json_path, fake_root): 调用方在 json_path 写入测试 JSON。
        """
        protocol_name = "p8_test_liquid_name"
        # 把 change_to_transfer_group.__file__ 所在目录的 detailed_action_json 重定向到 tmp_path
        target_dir = tmp_path / "detailed_action_json"
        target_dir.mkdir(parents=True, exist_ok=True)
        json_path = target_dir / f"{protocol_name}.json"

        # 让 load_liquid_locations 内部 Path(__file__).parent 指向 tmp_path
        # 直接通过 monkeypatch 改 change_to_transfer_group 模块下的 Path(__file__).parent
        # 由于 load_liquid_locations 直接用 Path(__file__).parent，最佳办法是改 sys.modules 引用
        # 但这里更简洁的方案：mock detailed_action_json 路径所在的 Path(__file__).parent
        import change_to_transfer_group as ctg
        orig_file = Path(ctg.__file__)
        fake_self_file = tmp_path / "change_to_transfer_group.py"
        fake_self_file.write_text("# placeholder\n", encoding="utf-8")
        monkeypatch.setattr(ctg, "__file__", str(fake_self_file))
        return protocol_name, json_path, tmp_path

    def test_explicit_liquid_name_populates_third_return(self, isolated_detailed_action_json):
        """detailed_action_json 含 ``liquid_locations[*].liquid_name`` → 进入 well_to_liquid_name。"""
        protocol_name, json_path, _ = isolated_detailed_action_json
        json_path.write_text(json.dumps({
            "liquid_locations": {
                "src_var": {
                    "slot": 2,
                    "well": "A1",
                    "liquid_name": "EDTA Plasma",
                    "source": "load_liquid",
                },
                "diluent_var": {
                    "slot": 3,
                    "well": "A1",
                    "liquid_name": "PBS Diluent (1×)",
                    "source": "load_liquid",
                },
            },
        }), encoding="utf-8")

        well_to_varname, _readme, well_to_liquid_name = load_liquid_locations(protocol_name)

        assert well_to_liquid_name == {
            (2, "A1"): "EDTA Plasma",
            (3, "A1"): "PBS Diluent (1×)",
        }, f"显式 liquid_name 应原样填入第 3 返回值；实际 {well_to_liquid_name}"

    def test_no_liquid_name_field_means_empty_third_return(self, isolated_detailed_action_json):
        """detailed_action_json 仅含 Python 变量名（无 liquid_name 字段）→ well_to_liquid_name 应为空。

        重要：Python 变量名作为 fallback **不**污染 well_to_liquid_name；
        否则会导致 reagent block 里 liquid_name = "sources" 这种 noise。
        """
        protocol_name, json_path, _ = isolated_detailed_action_json
        json_path.write_text(json.dumps({
            "liquid_locations": {
                "sources[0]": {"slot": 2, "well": "A1"},
                "sources[1]": {"slot": 2, "well": "B1"},
                # 无 liquid_name 字段
            },
        }), encoding="utf-8")

        well_to_varname, _readme, well_to_liquid_name = load_liquid_locations(protocol_name)

        # well_to_varname 仍然填入（用作 reagent_key normalize）
        assert (2, "A1") in well_to_varname
        # well_to_liquid_name 必须空（避免污染 P8 通路）
        assert well_to_liquid_name == {}, (
            f"无 explicit liquid_name 时第 3 返回值应为空；实际 {well_to_liquid_name}"
        )

    def test_mixed_with_and_without_liquid_name(self, isolated_detailed_action_json):
        """同 detailed_action_json 内部分 entry 有 liquid_name、部分无 → 只有显式那部分进入第 3 返回值。"""
        protocol_name, json_path, _ = isolated_detailed_action_json
        json_path.write_text(json.dumps({
            "liquid_locations": {
                "named_var": {
                    "slot": 1,
                    "well": "A1",
                    "liquid_name": "Plasma",
                },
                "anon_var": {
                    "slot": 2,
                    "well": "B1",
                    # 无 liquid_name
                },
            },
        }), encoding="utf-8")

        _, _, well_to_liquid_name = load_liquid_locations(protocol_name)
        assert well_to_liquid_name == {(1, "A1"): "Plasma"}, well_to_liquid_name

    def test_missing_detailed_action_json(self, tmp_path, monkeypatch):
        """detailed_action_json 文件不存在 → 第 3 返回值为空 dict（与现有 well_to_varname 一致）。"""
        protocol_name = "p8_does_not_exist_protocol"
        import change_to_transfer_group as ctg
        fake_self_file = tmp_path / "change_to_transfer_group.py"
        fake_self_file.write_text("# placeholder\n", encoding="utf-8")
        monkeypatch.setattr(ctg, "__file__", str(fake_self_file))

        well_to_varname, readme_candidates, well_to_liquid_name = load_liquid_locations(protocol_name)
        assert well_to_varname == {}
        assert well_to_liquid_name == {}
        # readme_candidates 因 original 目录也不存在，应当为 []
        assert readme_candidates == []
