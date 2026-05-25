"""P8 v1.3 — Stage 2 ``_load_python_var_chemistry_hints`` /
``_extract_liquid_steps_from_readme`` / ``Liquid_<n>`` 兜底单元测试。

对应 ``product_designs/protocol_convert/08-liquid-name-from-reagent-block.md`` §9.4。

覆盖范围
--------
- ``_load_python_var_chemistry_hints``：
  - 变量名含化学关键词 → direct hit；
  - 变量名 inline comment 含关键词 → direct hit；
  - RHS 别名链追溯（``samples = full_tube_map[12:]`` → ``full_tube_map`` 进 rhs_refs）；
  - 多层 BFS 追溯（深度 4 时仍命中）。
- ``_extract_liquid_steps_from_readme``：
  - ``### Protocol Steps`` 段提取 `Add <vol> <unit> of <liquid> to <wells_hint>`；
  - 单位词全写（microliters）+ 缩写（uL / mL）都支持；
  - 非 `Protocol Steps` / `Description` 段不抓取。
- ``_attach_liquid_name_to_reagents`` v1.3 端到端：
  - 当所有信号源 miss 时 ``Liquid_<n>`` 自增非空兜底；
  - ``python_var_hints`` 命中（包括别名链产物）写入 reagent.liquid_name；
  - ``readme_well_steps`` 在 mock/varname/.py 全部 miss 时反查写入。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


ROOT_DIR = Path(__file__).resolve().parents[3]
PROTOCOL_DIR = ROOT_DIR / "Protocols" / "protocol_converter"
if str(PROTOCOL_DIR) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_DIR))

from change_to_transfer_group import (  # noqa: E402
    _attach_liquid_name_to_reagents,
    _extract_liquid_steps_from_readme,
    _load_python_var_chemistry_hints,
)


# ==================== _load_python_var_chemistry_hints ====================


class TestLoadPythonVarChemistryHints:
    """`.py` 变量名 + 注释 + 别名链扫描。"""

    @pytest.fixture
    def make_protocol(self, tmp_path):
        """创建临时 ``original/<name>/<name>.ot2.apiv2.py`` 并返回 (original_root, name)。"""
        def _factory(name: str, py_body: str) -> tuple:
            original_root = tmp_path / "original"
            proto_root = original_root / name
            proto_root.mkdir(parents=True, exist_ok=True)
            (proto_root / f"{name}.ot2.apiv2.py").write_text(py_body, encoding="utf-8")
            return original_root, name
        return _factory

    def test_var_name_direct_hit(self, make_protocol):
        """变量名 token 含化学关键词（``methanol``）→ direct hit。"""
        original_root, name = make_protocol("p8_var_hit", """
def run(ctx):
    methanol = ctx.load_labware('nest_12_reservoir_15ml', '1')
    water = ctx.load_labware('opentrons_24_tuberack', '2')
    foo = ctx.load_labware('nest', '3')
""")
        hints = _load_python_var_chemistry_hints(original_root, name)
        assert hints.get("methanol") == ["methanol"]
        assert hints.get("water") == ["water"]
        assert "foo" not in hints, "无 chemistry token 的变量不应出现"

    def test_inline_comment_hit(self, make_protocol):
        """变量名本身不命中，但 inline comment 含关键词 → direct hit。"""
        original_root, name = make_protocol("p8_comment_hit", """
def run(ctx):
    rack_a = ctx.load_labware('rack', '1')  # contains EDTA Plasma samples
    rack_b = ctx.load_labware('rack', '2')  # PBS Diluent buffer
    rack_c = ctx.load_labware('rack', '3')  # nothing relevant
""")
        hints = _load_python_var_chemistry_hints(original_root, name)
        assert "plasma" in hints.get("rack_a", []), f"rack_a should contain 'plasma'; got {hints.get('rack_a')}"
        assert "pbs" in hints.get("rack_b", []) or "buffer" in hints.get("rack_b", []), (
            f"rack_b should contain 'pbs' or 'buffer'; got {hints.get('rack_b')}"
        )
        # rack_c 注释里没有任何关键词 → 不应出现
        assert "rack_c" not in hints, f"rack_c should not appear; got {hints.get('rack_c')}"

    def test_alias_chain_resolves_through_4_hops(self, make_protocol):
        """BFS 追溯多层别名链（00a6e5 真实场景）。"""
        original_root, name = make_protocol("p8_alias_chain", """
import itertools
def run(ctx):
    urine_tube_rack = [ctx.load_labware('opentrons_24_tuberack', slot) for slot in ['1', '2']]
    tube_map_double_nest = [
        [urine_tube_rack[j].rows()[i] for i in range(8)]
        for j in range(0, 2)
    ]
    tube_map_nest = list(itertools.chain.from_iterable(tube_map_double_nest))
    full_tube_map = list(itertools.chain.from_iterable(tube_map_nest))
    samples = full_tube_map[12:]
    negative_urine = full_tube_map[1:8]
""")
        hints = _load_python_var_chemistry_hints(original_root, name)
        # direct hit on urine_tube_rack (var name contains 'urine')
        assert hints.get("urine_tube_rack") == ["urine"]
        # direct hit on negative_urine (var name contains 'urine')
        assert "urine" in hints.get("negative_urine", [])
        # alias chain: samples → full_tube_map → tube_map_nest → tube_map_double_nest → urine_tube_rack
        assert "urine" in hints.get("samples", []), (
            f"samples should inherit 'urine' via 4-hop alias chain; got {hints.get('samples')}"
        )
        assert "urine" in hints.get("full_tube_map", []), (
            f"full_tube_map should inherit 'urine'; got {hints.get('full_tube_map')}"
        )

    def test_missing_py_file_returns_empty(self, tmp_path):
        """``.py`` 文件不存在 → 返回空 dict，不报错。"""
        original_root = tmp_path / "original"
        # 不创建任何文件
        hints = _load_python_var_chemistry_hints(original_root, "nonexistent")
        assert hints == {}

    def test_syntax_error_returns_empty(self, make_protocol):
        """``.py`` 含语法错误 → AST 解析失败时静默返回 ``{}``，不阻塞 pipeline。"""
        original_root, name = make_protocol("p8_syntax_err", "def run(ctx):\n    this is not valid python\n")
        hints = _load_python_var_chemistry_hints(original_root, name)
        assert hints == {}


# ==================== _extract_liquid_steps_from_readme ====================


class TestExtractLiquidStepsFromReadme:
    """README ``### Protocol Steps`` / ``### Description`` 段抽取。"""

    def test_protocol_steps_section_extraction(self):
        """`Add <vol> <unit> of <liquid> to <wells_hint>` 模式抽取。"""
        text = """# Some Protocol

## Description

Adds reagents.

### Protocol Steps
1. Add 60 microliters of Enzyme to all wells
2. Add 340 microliters of Buffer to all wells
3. Add 300 microliters of Negative Urine to A1 (blank)
4. Add 20 Microliters of Spike INSTD to all wells
"""
        steps = _extract_liquid_steps_from_readme(text)
        assert len(steps) >= 4
        liquids = [s["liquid"] for s in steps]
        assert "Enzyme" in liquids
        assert "Buffer" in liquids
        assert "Negative Urine" in liquids
        assert "Spike INSTD" in liquids

    def test_description_section_also_scanned(self):
        """``### Description`` 段也被纳入抽取范围。"""
        text = """# Protocol

### Description

This protocol will Add 100 mL of Water to all wells before incubation.

### Labware
* Some plate
"""
        steps = _extract_liquid_steps_from_readme(text)
        assert any(s["liquid"] == "Water" for s in steps), (
            f"应从 Description 段抓取 Water；实际 {steps}"
        )

    def test_non_target_section_skipped(self):
        """非 Protocol Steps / Description 段（如 Labware / Process）不抓取。"""
        text = """# Protocol

### Labware
1. Add 60 microliters of Enzyme to all wells

### Process
1. Add 100 mL of Methanol to all wells
"""
        steps = _extract_liquid_steps_from_readme(text)
        assert steps == [], f"非目标段不应被抓取；实际 {steps}"

    def test_unit_abbreviation_and_long_form(self):
        """单位词支持缩写 (uL/mL) + 全写 (microliters/milliliters)。"""
        text = """### Protocol Steps
1. Add 60 uL of Plasma to all wells
2. Add 100 mL of Buffer to A1
3. Add 50 microliters of Diluent to A2-A8
4. Add 200 milliliters of Saline to all wells
"""
        steps = _extract_liquid_steps_from_readme(text)
        liquids = [s["liquid"] for s in steps]
        assert "Plasma" in liquids
        assert "Buffer" in liquids
        assert "Diluent" in liquids
        # `Saline` 不在 _CHEMISTRY_KEYWORDS 里也无所谓——抽取器只关心 grammar
        assert "Saline" in liquids

    def test_empty_or_no_section(self):
        """空文本 / 无 Protocol Steps 段 → 空列表。"""
        assert _extract_liquid_steps_from_readme("") == []
        assert _extract_liquid_steps_from_readme("# Title\n\n### Labware\n1. Some plate") == []


# ==================== v1.3 端到端兜底行为 ====================


class TestV13EndToEndFallback:
    """v1.3 ``Liquid_<n>`` 兜底 + python_var_hints / readme_well_steps 通路。"""

    def test_liquid_n_counter_increments_per_call(self):
        """v1.3：多个 entry 都走 Liquid_<n> 兜底时计数器自增。"""
        reagents: Dict[str, Dict[str, Any]] = {
            "sources_2": {"slot": 2, "well": ["A1"], "labware": "x", "object": "source"},
            "sources_3": {"slot": 2, "well": ["B1"], "labware": "x", "object": "source"},
            "targets_4": {"slot": 5, "well": ["C1"], "labware": "x", "object": "target"},
        }
        _attach_liquid_name_to_reagents(reagents, {})
        assert reagents["sources_2"]["liquid_name"] == "Liquid_1"
        assert reagents["sources_3"]["liquid_name"] == "Liquid_2"
        assert reagents["targets_4"]["liquid_name"] == "Liquid_3"

    def test_python_var_hints_inject_via_alias_chain(self):
        """`python_var_hints[reagent_key]` 命中即使 reagent_key 本身非化学词也注入。"""
        reagents: Dict[str, Dict[str, Any]] = {
            "samples_5": {"slot": 2, "well": ["A1"], "labware": "x", "object": "source"},
            "full_tube_map": {"slot": 4, "well": ["A1"], "labware": "x", "object": "source"},
        }
        python_var_hints = {
            "samples": ["urine"],  # base key (samples_5 → samples)
            "full_tube_map": ["urine"],
        }
        _attach_liquid_name_to_reagents(
            reagents,
            {},
            python_var_hints=python_var_hints,
        )
        assert reagents["samples_5"]["liquid_name"] == "urine"
        assert reagents["full_tube_map"]["liquid_name"] == "urine"

    def test_readme_well_steps_reverse_lookup(self):
        """``readme_well_steps`` 在前 4 条信号源 miss 时按 wells_hint 反查。"""
        reagents: Dict[str, Dict[str, Any]] = {
            "src_a": {"slot": 1, "well": ["A1"], "labware": "x", "object": "source"},
            "src_all": {"slot": 1, "well": ["B1"], "labware": "x", "object": "source"},
        }
        # README 抽取：液体名 + wells_hint
        readme_well_steps = [
            {"liquid": "EDTA Plasma", "wells_hint": "A1"},
            {"liquid": "Buffer", "wells_hint": "all wells"},
        ]
        # 触发 wells_hint=all wells 的匹配需要 liquid token 与 reagent_key 有重叠；
        # 这里 `src_all` 不含 buffer token，因此 'all wells' 不应命中 → Liquid_n 兜底。
        _attach_liquid_name_to_reagents(
            reagents,
            {},
            readme_well_steps=readme_well_steps,
        )
        assert reagents["src_a"]["liquid_name"] == "EDTA Plasma"
        # src_all 因 reagent_key 与 'Buffer' token 无关联 → 走 Liquid_<n>
        assert reagents["src_all"]["liquid_name"] == "Liquid_1"

    def test_priority_chain_order_v13(self):
        """优先级链：mock (1) > .py hint (3) > Liquid_<n> (6)。"""
        # 三个 entry：第一个有 mock；第二个仅有 .py hint；第三个全无
        reagents: Dict[str, Dict[str, Any]] = {
            "a": {"slot": 1, "well": ["A1"], "labware": "x", "object": "source"},
            "b": {"slot": 2, "well": ["A1"], "labware": "x", "object": "source"},
            "c": {"slot": 3, "well": ["A1"], "labware": "x", "object": "source"},
        }
        well_to_liquid_name = {(1, "A1"): "EDTA Plasma"}  # only entry `a`
        python_var_hints = {"b": ["urine"]}  # only entry `b`
        _attach_liquid_name_to_reagents(
            reagents,
            well_to_liquid_name,
            python_var_hints=python_var_hints,
        )
        assert reagents["a"]["liquid_name"] == "EDTA Plasma"  # priority 1
        assert reagents["b"]["liquid_name"] == "urine"  # priority 3
        assert reagents["c"]["liquid_name"] == "Liquid_1"  # priority 6

    def test_existing_liquid_n_counter_preserved(self):
        """已有 ``Liquid_3`` 占位时新增 entry 从 ``Liquid_4`` 起，避免冲突。"""
        reagents: Dict[str, Dict[str, Any]] = {
            "preset": {
                "slot": 1, "well": ["A1"], "labware": "x", "object": "source",
                "liquid_name": "Liquid_3",  # 系统占位名，将被 v1.3 视为可覆盖
            },
            "new_a": {"slot": 2, "well": ["A1"], "labware": "x", "object": "source"},
        }
        _attach_liquid_name_to_reagents(reagents, {})
        # preset 被识别为系统占位名 + 无新信号源 → 重新走 Liquid_<n> 兜底
        # （但计数器从 4 起步，避免与已存在的 Liquid_3 冲突）
        new_a_name = reagents["new_a"]["liquid_name"]
        preset_name = reagents["preset"]["liquid_name"]
        assert new_a_name.startswith("Liquid_")
        assert preset_name.startswith("Liquid_")
        # 计数器应该 ≥ 4（因为 Liquid_3 已被使用）
        assert int(new_a_name.split("_")[1]) >= 4 or int(preset_name.split("_")[1]) >= 4

    def test_tiprack_trash_still_omit_liquid_name(self):
        """v1.3 不影响 tiprack / trash 行为：仍**不**写入 liquid_name。"""
        reagents: Dict[str, Dict[str, Any]] = {
            "tiprack_7": {"slot": 7, "labware": "opentrons_96_tiprack_300ul", "object": "tiprack"},
            "trash": {"slot": 12, "labware": "opentrons_1_trash_1100ml_fixed", "object": "trash"},
        }
        _attach_liquid_name_to_reagents(reagents, {})
        assert "liquid_name" not in reagents["tiprack_7"]
        assert "liquid_name" not in reagents["trash"]
