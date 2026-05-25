import json
import os
from pathlib import Path
from pprint import pprint
import re
from typing import List, Dict, Any, Optional, Tuple

_DEF_WELL_COUNTS = (384, 96, 48, 24)


# ============================================================================
# P4 — Reagent naming utilities
# ----------------------------------------------------------------------------
# 优先级链（自顶向下）：
#   1) 显式 mock 层 `define_liquid(name=...)` / `Well.load_liquid(liquid)` 写入的 liquid_name
#   2) Python 变量名（去掉数组下标 `[N]`，snake_case 化）
#   3) README.md 中抽取的语义名（按出现顺序填充未命名孔位）
#   4) 兜底自动生成 `Liquid_N`
#
# 过渡开关：环境变量 ``UNILAB_PROTOCOL_KEEP_LEGACY_NAMES=1`` 跳过 README 接管。
# ============================================================================

_REAGENT_STOP_WORDS = {
    # 容器与位置词
    "tube", "tubes", "well", "wells", "rack", "racks", "row", "rows",
    "column", "columns", "tip", "tips", "pipette", "pipettes", "deck",
    "labware", "plate", "plates", "module", "modules", "slot", "slots",
    "robot", "spot", "spots", "vial", "vials", "trough", "troughs",
    "reservoir", "reservoirs", "block", "blocks", "channel", "channels",
    # 单位与数量
    "ul", "ml", "ng", "mg", "ug", "kg",
    # 程序词 / 介词
    "step", "steps", "protocol", "process", "added", "add", "is", "are",
    "be", "the", "of", "to", "from", "in", "on", "at", "with", "and",
    "or", "for", "by", "an", "a", "this", "that", "these", "those",
    "all", "any", "each", "every",
    # 噪声词
    "default", "value", "specify", "user", "ot", "ot-2", "ot-app",
    "support", "tip-reuse", "tipreuse", "yes", "no", "below", "above",
    "left", "right", "top", "bottom",
    # 数字词形
    "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten",
}

# 段落白名单 / 黑名单（标题大小写不敏感）
_REAGENT_SECTIONS_EXCLUDE = {
    "labware", "pipettes", "process", "additional notes", "additional",
    "categories", "author", "deck setup", "modules", "robot", "internal",
    "support", "links",
}

# Markdown 标题
_RE_MD_HEADER = re.compile(r"^(#{1,6})\s+(.+?)\s*$", flags=re.MULTILINE)

# Reagent Setup 段落下列表项匹配（* Foo / - Foo / + Foo）；
# 仅取首段非链接文本（去掉 markdown 链接），且不接受过长/含数字的项（避免 labware 误命中）
_RE_REAGENT_LIST_ITEM = re.compile(
    r"^[\s>]*[*+\-]\s+([^\[\(\n]+)"
)

# Description / Protocol Steps 段落里的 "<vol> uL <reagent> is/are added to" 模式
_RE_REAGENT_IN_TEXT = re.compile(
    r"(?:\d+\s*(?:µ?[uU][lL]|[mM][lL])\s+)?(?:of\s+)?"
    r"([A-Za-z][A-Za-z]+(?:\s+[a-z]+){0,2})"
    r"\s+(?:is\s+added|are\s+added|is\s+a\s|are\s+a\s|to\s+tubes?|to\s+wells?)",
)

# 「<Reagent> is/are added/dispensed/...」首字母大写起步的启发式
_RE_REAGENT_VERB = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[a-z]+){0,2})\s+(?:is|are)\s+(?:added|aspirated|dispensed|diluted|loaded|filled|spotted)\b"
)


def _to_reagent_key(name: str) -> str:
    """统一 reagent 名称为 snake_case key。

    1. 小写
    2. 内部 `\\s/-` → '_'，多 `_` 合并、首尾去 `_`
    3. 移除标点 / 非 ASCII（保留字母/数字/下划线）
    4. 长度 > 32 截断
    5. 若结果为空字符串则返回 ''（调用方应回退）
    """
    if not isinstance(name, str) or not name:
        return ""
    s = name.strip().lower()
    s = re.sub(r"[\s\-/.]+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if len(s) > 32:
        s = s[:32].rstrip("_")
    return s


def _dedup_with_index(name: str, used: set) -> str:
    """若 ``name`` 已被 ``used`` 占用，追加 ``_2 / _3 / ...`` 直到唯一。"""
    if not name:
        return name
    if name not in used:
        return name
    counter = 2
    while f"{name}_{counter}" in used:
        counter += 1
    return f"{name}_{counter}"


def _split_markdown_sections(text: str) -> Dict[str, str]:
    """按 markdown 标题切分，返回 ``{header_lower: body_text}`` 字典。

    没有任何标题之前的内容归到 ``"_preamble"`` 键下。
    重复标题时后者覆盖前者（README 内通常不重）。
    """
    sections: Dict[str, str] = {}
    last_header = "_preamble"
    buf: List[str] = []
    for line in text.splitlines():
        m = _RE_MD_HEADER.match(line)
        if m:
            if buf:
                sections[last_header] = "\n".join(buf).strip()
            last_header = m.group(2).strip().lower()
            buf = []
        else:
            buf.append(line)
    if buf:
        sections[last_header] = "\n".join(buf).strip()
    return sections


def _clean_phrase_tokens(phrase: str) -> str:
    """对短语 token 做去停词 / 去空白处理；返回最终保留的短语（空格分隔）。

    返回空串表示该候选应被丢弃。
    """
    tokens = [t for t in re.split(r"\s+", phrase.strip().lower()) if t]
    # 末尾去停词（"diluent in" → "diluent"）
    while tokens and tokens[-1] in _REAGENT_STOP_WORDS:
        tokens.pop()
    # 首部去停词（"the diluent" → "diluent"）
    while tokens and tokens[0] in _REAGENT_STOP_WORDS:
        tokens.pop(0)
    if not tokens:
        return ""
    # 全是 stop_words → 丢弃
    if all(t in _REAGENT_STOP_WORDS for t in tokens):
        return ""
    # 单字符 / 过短 token 串 → 丢弃
    if len(" ".join(tokens)) < 3:
        return ""
    return " ".join(tokens)


def _extract_reagent_phrases(body: str) -> List[str]:
    """从单段 markdown body 中抽取 reagent 候选短语（小写、保留空格）。

    召回三种来源：
    - 列表项 ``* Foo bar`` / ``- Foo``（Reagent Setup 段落典型）
    - ``N uL <reagent> is/are added to`` / ``of <reagent> to`` 模式
    - ``<Reagent> is/are added/aspirated/...`` 首字母大写启发式

    所有候选先去停词、过滤标点；保留出现顺序、不去重（由上层 dedup）。
    """
    candidates: List[str] = []

    # 1) 列表项
    for line in body.splitlines():
        m = _RE_REAGENT_LIST_ITEM.match(line)
        if not m:
            continue
        raw = m.group(1).strip()
        # 去掉 markdown 链接 / 行内代码 / 括号注释
        raw = re.sub(r"\[([^\]]*)\]\([^\)]*\)", r"\1", raw)
        raw = re.sub(r"`[^`]*`", "", raw)
        raw = re.sub(r"\(.*?\)", "", raw)
        # 拿到第一个分号 / 冒号前的主短语
        raw = re.split(r"[;:|]", raw)[0]
        # 含数字（如 "300 µL filter tips"）很可能是 labware，跳过
        if re.search(r"\d", raw):
            continue
        cleaned = _clean_phrase_tokens(raw)
        if cleaned:
            candidates.append(cleaned)

    # 2) 文本内 "<reagent> is/are added to" / "uL <reagent> to"
    for m in _RE_REAGENT_IN_TEXT.finditer(body):
        cleaned = _clean_phrase_tokens(m.group(1))
        if cleaned:
            candidates.append(cleaned)

    # 3) 首字母大写启发式
    for m in _RE_REAGENT_VERB.finditer(body):
        cleaned = _clean_phrase_tokens(m.group(1))
        if cleaned:
            candidates.append(cleaned)

    return candidates


def load_reagents_from_readme(protocol_dir) -> List[str]:
    """从 ``original/<name>/README.md`` 抽取试剂语义名候选。

    返回按 "出现位置 + 频次" 加权排序的候选列表（小写、保留空格，
    **未** snake_case 化；消费方需再调用 :func:`_to_reagent_key`）。

    扫描时跳过 Labware / Pipettes / Process / Additional Notes / Categories /
    Author / Deck Setup / Modules / Robot 等无关段落（见
    :data:`_REAGENT_SECTIONS_EXCLUDE`）；其余段落统一进入抽取流程。

    文件不存在或解析失败时返回 ``[]``。
    """
    if not isinstance(protocol_dir, Path):
        protocol_dir = Path(protocol_dir)
    readme_path = protocol_dir / "README.md"
    if not readme_path.exists():
        return []
    try:
        text = readme_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []

    sections = _split_markdown_sections(text)
    # 按 (出现次数, 首次出现 section 顺序) 排序
    order: List[str] = []
    counts: Dict[str, int] = {}
    section_order: Dict[str, int] = {}
    section_idx_counter = 0
    for header, body in sections.items():
        h = header.strip().lower()
        if h in _REAGENT_SECTIONS_EXCLUDE:
            continue
        for noun in _extract_reagent_phrases(body):
            if noun in _REAGENT_STOP_WORDS:
                continue
            if noun not in counts:
                counts[noun] = 0
                section_order[noun] = section_idx_counter
                order.append(noun)
            counts[noun] += 1
        section_idx_counter += 1

    # 优先按频次降序、再按首次 section 顺序升序；同分时保持插入顺序
    order.sort(key=lambda n: (-counts[n], section_order[n]))
    return order
# ============================================================================


# ============================================================================
# P5 — Workflow envelope metadata utilities
# ----------------------------------------------------------------------------
# load_protocol_metadata(protocol_dir) 把以下两路信息合并成统一外壳元数据：
#   1) ``Protocols/protocol_converter/detailed_action_json/<name>.json`` 顶层
#      ``metadata`` 段（来自 mock 层 ``ProtocolRecorder.dump_detailed_action_json``）
#   2) ``original/<name>/<file>.ot2.apiv2.py`` 顶层 ``metadata = {...}`` 字典的
#      正则兜底解析
#   3) ``original/<name>/README.md`` 中 ``## Categories`` 段的列表项 → tags
#
# 输出结构（被 export_transfer_actions 注入顶层 ``metadata`` 段消费）：
#   {
#       "workflow_name": "Pooling and Normalization via CSV",
#       "tags": ["Sample Prep", "Plate Filling"],
#       "raw": {"protocolName": "...", "author": "...", "apiLevel": "..."}
#   }
# ============================================================================

# ``## Categories`` 段标题；支持多语言备选（中文 / 日文）。行首匹配，允许尾随空格。
_RE_CATEGORIES_HEADER = re.compile(
    r"^##\s*(Categories|Category|分类|タグ|カテゴリ)\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)

# README 一级标题；只取第一行 ``# Title`` 作为最低优先级的 workflow_name fallback。
_RE_README_H1 = re.compile(r"^#\s+(.+?)\s*$", flags=re.MULTILINE)

# Categories 段下属列表项；允许任意缩进 + ``*`` / ``-`` / ``+`` bullet。
_RE_CATEGORIES_BULLET = re.compile(r"^[\s\t]*[\*\-\+]\s+(.+?)\s*$")

# 原 .py 文件顶层 ``protocolName`` 字段提取（兜底）。
# 支持单引号、双引号、三引号字符串；三引号字符串允许跨行（``re.DOTALL``）。
_RE_PROTOCOL_NAME = re.compile(
    r"['\"]protocolName['\"]\s*:\s*"
    r"(?:"
    r"\"{3}(.+?)\"{3}"        # """..."""
    r"|'{3}(.+?)'{3}"          # '''...'''
    r"|\"([^\"\n]*)\""         # "..."
    r"|'([^'\n]*)'"            # '...'
    r")",
    flags=re.DOTALL,
)


def _normalize_metadata_value(raw: str) -> str:
    """协议 metadata 字段值清洗：合并连续空白 + 首尾 strip。

    用于三引号字符串里跨行 + 缩进的场景，把
    ``"NEBNext...:\\n    E7770S Section 1\\n    Part 1..."`` 折叠成
    单行可读字符串。
    """
    if not raw:
        return ""
    return re.sub(r"\s+", " ", raw).strip()


def _strip_markdown_link(text: str) -> str:
    """把 ``[label](url)`` 折叠为 ``label``；其余字符保留。"""
    return re.sub(r"\[([^\]]*)\]\([^\)]*\)", r"\1", text)


def _parse_readme_title(text: str) -> str:
    """从 README 文本抽第一行 ``# Title`` 作为 workflow_name 兜底。

    用于协议 ``metadata`` 缺失 / 显式为空 ``protocolName`` 的场景（如
    ``0a23c6``、``7e4c3e-1``）。仅取首个 ``# `` 标题；返回去链接、去空白后的纯文本。
    """
    if not text:
        return ""
    m = _RE_README_H1.search(text)
    if not m:
        return ""
    title = _strip_markdown_link(m.group(1)).strip()
    return re.sub(r"\s+", " ", title)


def _parse_readme_categories(text: str) -> List[str]:
    """从 README 文本中抽 ``## Categories`` 段的列表项 → tags。

    解析规则（详见 [05 §3.2.2](../../product_designs/protocol_convert/05-workflow-envelope-and-metadata.md#322-_parse_readme_categoriestext-str---liststr)）：

    1. 用 ``^##\\s*(Categories|...)\\s*$`` 定位段标题（行首，允许尾随空格）。
    2. 从段标题下一行开始扫描，逐行收集 ``* item`` / ``- item`` / ``+ item``。
    3. 遇到下一个 ``##`` 标题或 EOF 收尾；连续空行不影响（缩进子项也算 tag）。
    4. 单条 tag 去 markdown 链接、内部空白合并、去重保序。
    5. 若未找到段或段内无列表项，返回空列表。
    """
    if not text:
        return []
    m = _RE_CATEGORIES_HEADER.search(text)
    if not m:
        return []
    body_start = m.end()
    # 截到下一个 ``##`` 标题（同级或更高，保守起见匹配任何 ``^## ``）
    next_header = re.search(r"^##\s+", text[body_start:], flags=re.MULTILINE)
    body = text[body_start:body_start + next_header.start()] if next_header else text[body_start:]

    tags: List[str] = []
    seen: set = set()
    for line in body.splitlines():
        # 跳过空行 / 非列表项；但不 break，允许列表项之间空一行
        bm = _RE_CATEGORIES_BULLET.match(line)
        if not bm:
            continue
        raw = _strip_markdown_link(bm.group(1)).strip()
        # 内部多空白合并
        raw = re.sub(r"\s+", " ", raw)
        if not raw:
            continue
        if raw in seen:
            continue
        seen.add(raw)
        tags.append(raw)
    return tags


def _join_python_line_continuations(text: str) -> str:
    """模拟 Python 字符串的 backslash + LF 续行——把 ``\\\\\\n`` 直接吃掉。

    Opentrons 协议偶有作者把长 ``protocolName`` 字面量用
    ``'MP Biomedicals magGENic Plant DNA Kit: Nucleic Acid \\<LF>Purification'``
    断行。我们在正则匹配前把 ``\\\\\\n`` 折叠掉，等效于 Python 词法分析后的字面量。
    """
    return re.sub(r"\\\r?\n", "", text)


def _read_protocol_name_from_py(protocol_dir: Path) -> str:
    """正则在 ``*.ot2.apiv2.py`` / ``*.py`` 中匹配 ``protocolName`` 字段。

    兜底使用：仅在 detailed_action_json 缺失 ``metadata.protocolName`` 时调用。
    支持：

    - 单引号 / 双引号 / 三引号字符串（如 ``0479ad`` 的多行长描述）。
    - Python 字符串 backslash 续行（如 ``00a577``）。

    匹配失败返回空字符串。
    """
    py_candidates = list(protocol_dir.glob("*.ot2.apiv2.py")) + list(protocol_dir.glob("*.py"))
    for py in py_candidates:
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        text = _join_python_line_continuations(text)
        # 限定 ``metadata = {...}`` 块内匹配；如果协议没有显式 metadata 字典，
        # 直接全文找 ``protocolName`` 即可（同款 Opentrons 协议通常没有歧义）。
        meta_match = re.search(r"metadata\s*=\s*\{(.+?)\}", text, flags=re.DOTALL)
        body = meta_match.group(1) if meta_match else text
        name_match = _RE_PROTOCOL_NAME.search(body)
        if name_match:
            # 4 个 group 分别对应 """ / ''' / " / ' 四种字符串风格，取首个非空
            value = next((g for g in name_match.groups() if g is not None), "")
            return _normalize_metadata_value(value)
    return ""


# 通用 metadata 字段提取：支持单引号 / 双引号 / 三引号 + 跨行缩进
_RE_METADATA_FIELD = re.compile(
    r"['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*:\s*"
    r"(?:"
    r"\"{3}(.+?)\"{3}"
    r"|'{3}(.+?)'{3}"
    r"|\"([^\"\n]*)\""
    r"|'([^'\n]*)'"
    r")",
    flags=re.DOTALL,
)


def _parse_metadata_block_from_py(protocol_dir: Path) -> Dict[str, str]:
    """正则解析 ``*.py`` 中的 ``metadata = {...}`` 块，返回 ``{key: str_value}``。

    兜底使用：仅在 detailed_action_json 缺失 ``metadata`` 段时调用。
    保持 Opentrons 原命名（protocolName / author / source / apiLevel）。
    与 :func:`_read_protocol_name_from_py` 一致地支持三引号 + 跨行字符串。
    """
    raw: Dict[str, str] = {}
    py_candidates = list(protocol_dir.glob("*.ot2.apiv2.py")) + list(protocol_dir.glob("*.py"))
    for py in py_candidates:
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        text = _join_python_line_continuations(text)
        meta_match = re.search(r"metadata\s*=\s*\{(.+?)\}", text, flags=re.DOTALL)
        if not meta_match:
            continue
        body = meta_match.group(1)
        for m in _RE_METADATA_FIELD.finditer(body):
            key = m.group(1).strip()
            if key in raw:
                continue
            # 4 个 value group：""" / ''' / " / '
            value = next((g for g in m.groups()[1:] if g is not None), "")
            raw[key] = _normalize_metadata_value(value)
        if raw:
            break
    return raw


def load_protocol_metadata(protocol_dir) -> Dict[str, Any]:
    """加载协议级元数据，供 ``export_transfer_actions`` 写入顶层 ``metadata`` 段。

    优先级（自顶向下）：

    1. ``detailed_action_json/<name>.json`` 的顶层 ``metadata`` 段
       （Stage 1 mock 层捕获并写入；最稳）。
    2. ``original/<name>/<file>.ot2.apiv2.py`` 顶层 ``metadata = {...}`` 字典
       的正则兜底（detailed_action_json 缺失 / 老数据时）。

    Tags 单独来自 ``original/<name>/README.md`` 的 ``## Categories`` 段。

    返回:
        {
            "workflow_name": str,
            "tags": List[str],
            "raw": Dict[str, str]
        }

    缺失项以空字符串 / 空列表占位；不抛异常。
    """
    if not isinstance(protocol_dir, Path):
        protocol_dir = Path(protocol_dir)

    name = protocol_dir.name
    workflow_name = ""
    raw: Dict[str, Any] = {}

    # 1) 优先：detailed_action_json/<name>.json 的 metadata 段（mock 层产物）
    detailed_path = Path(__file__).parent / "detailed_action_json" / f"{name}.json"
    if detailed_path.exists():
        try:
            with detailed_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            meta = data.get("metadata") if isinstance(data, dict) else None
            if isinstance(meta, dict):
                raw = {str(k): v for k, v in meta.items()}
                workflow_name = str(raw.get("protocolName") or "").strip()
        except Exception:
            pass

    # 2) 兜底：在 *.py 中正则提取 ``metadata = {...}``
    if not workflow_name:
        workflow_name = _read_protocol_name_from_py(protocol_dir)
    if not raw:
        raw = _parse_metadata_block_from_py(protocol_dir)
        # 同步把 protocolName 兜底回填到 raw
        if workflow_name and "protocolName" not in raw:
            raw["protocolName"] = workflow_name

    # 3) Tags + README 一级标题兜底
    tags: List[str] = []
    readme = protocol_dir / "README.md"
    readme_text = ""
    if readme.exists():
        try:
            readme_text = readme.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            readme_text = ""
    if readme_text:
        try:
            tags = _parse_readme_categories(readme_text)
        except Exception:
            tags = []
        # 4) README ``# Title`` 作为 workflow_name 最低优先级 fallback：仅在
        #    ``protocolName`` 缺失 / 空时使用（如 ``063611`` 用 ``title`` 字段、
        #    ``0a23c6`` 显式 ``protocolName=""``、``7e4c3e-1`` metadata 只有 apiLevel）
        if not workflow_name:
            workflow_name = _parse_readme_title(readme_text)

    return {
        "workflow_name": workflow_name,
        "tags": tags,
        "raw": raw,
    }
# ============================================================================

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

def _normalize_pose_z_kind(pose_z: Optional[str]) -> str:
    """归一化 pose_z：``"top"`` / ``"bottom"`` / ``""``（未知）。``None`` 视为 ``"bottom"``
    （因为 Opentrons ``aspirate(v, well)`` 默认在 bottom）。"""
    if pose_z is None:
        return "bottom"
    s = str(pose_z)
    if s.startswith("top"):
        return "top"
    if s.startswith("bottom"):
        return "bottom"
    return ""


def _is_air_gap_pattern(aspirate_list: List[Tuple[float, Optional[str]]]) -> bool:
    """判断 N-aspirate 块是否为 air-gap 模式（含至少一个 ``top`` pose_z）。

    若全部是 ``bottom`` / ``None``，则视为「N 次累积 bottom-aspirate」，
    需在 :func:`get_action_list` 中预合并为单次 ``sum`` 吸取。
    """
    return any(_normalize_pose_z_kind(p) == "top" for _, p in aspirate_list)


def _apply_pose_z_volumes(aspirate_list: List[Tuple[float, Optional[str]]]) -> Tuple[Optional[float], float, Optional[float]]:
    """
    根据相邻 aspirate 的 pose_z 分配体积参数。

    aspirate_list: ``[(vol, pose_z), ...]``，``pose_z`` 为 ``"top"`` / ``"bottom"`` / ``None``。
    返回 ``(blow_out_air_volume_before, asp_vol, blow_out_air_volume)``。

    - 1 个 aspirate：直接 asp_vol = v1，无 blow。
    - 2 个相邻 aspirate（air-gap 模式）：
        - (top, bottom) -> 第一个=blow_out_air_volume_before，第二个=asp_vols
        - (bottom, top) -> 第一个=asp_vols，第二个=blow_out_air_volume
        - (bottom, bottom) -> 不应到达此分支（应被 :func:`get_action_list` 预合并），
          但作为防御回退为 (None, v1+v2, None)。
    - 3 个相邻 aspirate（air-gap+asp+blowout 三联）：
        第一个=blow_out_air_volume_before，第二个=asp_vols，第三个=blow_out_air_volume。
        本函数仅在三个 aspirate 中**至少有一个 top pose_z** 时被调用三联；纯 bottom
        三联应在 :func:`get_action_list` 中预合并。
    - 其他情况：asp_vol = sum，无 blow。
    """
    if not aspirate_list:
        return None, 0.0, None
    n = len(aspirate_list)
    if n == 1:
        return None, aspirate_list[0][0], None
    if n == 2:
        v1, p1 = aspirate_list[0]
        v2, p2 = aspirate_list[1]
        k1, k2 = _normalize_pose_z_kind(p1), _normalize_pose_z_kind(p2)
        if k1 == "top" and k2 == "bottom":
            return v1, v2, None
        if k1 == "bottom" and k2 == "top":
            return None, v1, v2
        # (bottom, bottom) / 未知 pose_z：上游应已预合并，否则回退为 sum。
        return None, v1 + v2, None
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
        # P1 多通道意图：若该 phase 内有任一 aspirate / dispense / mix 等记录了 channels==8，
        # 则视为 multi 通道 phase；否则单通道（默认 1）。在同一 phase 内不允许 single 与 multi 混用，
        # 这与 Opentrons API 的「一个 phase 共用一支 pipette」假设一致。
        phase_channels = 8 if any(
            isinstance(s, dict) and int(s.get('channels', 1) or 1) == 8 for s in phase
        ) else 1
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
                                pre_asp_from_target_vol,
                                phase_channels))
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
                    if st['action'] == "pick_tip":
                        break
                    if st['action'] == "aspirate":
                        # 后置 back-aspirate from target：dispense 之后 aspirate 的 source
                        # 等于已收集的某个 dispense target（mix-after / 排液后回吸再排）。
                        # 这种 aspirate 不算下一个 transfer 的开始，应吸记到 pre_asp_target 并
                        # 继续收集后续 dispense；典型如 640a85：
                        #   aspirate(3, slot=4, A1) + air_gap(2) + dispense(2, slot=7, top)
                        #   aspirate(7, slot=7, A12, bottom)   ← back-asp from target
                        #   dispense(10, slot=7, A12, bottom)
                        bs = (st.get('source', {}).get('slot'), st.get('source', {}).get('well'))
                        if seen_valid_dispense and bs in {d[0] for d in dispenses}:
                            pre_asp_from_target_vol += float(st.get('vol', 0))
                            k += 1
                            continue
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

                # 无有效 source 或 aspirate 块为空（例如 aspirate 全被判为回吸）时勿写入 transfers，
                # 否则分组后会出现 aspirate=[None]，set_liquid_info 解包失败，整协议被 generate 吞成空列表。
                if dispenses and (src_key is None or not asp_block):
                    i = j
                    continue

                # N 次连续 bottom-aspirate（无 top air-gap）等价于一次累积吸取，
                # 合并为 1-tuple 以避免被 _apply_pose_z_volumes 误识别为
                # "air-gap+asp+blowout" 三联导致 asp_vol/dis_vol 被减为 0。
                # 这种模式典型出现于 distribute (1 source → N targets)：
                #     for _ in range(N): m300.aspirate(v, src.bottom(1))
                #     for d in d_set:    m300.dispense(v, d.bottom(z))
                if len(asp_block) >= 2 and not _is_air_gap_pattern(asp_block):
                    asp_block = [(sum(v for v, _ in asp_block), asp_block[0][1])]

                # 若 1 个 aspirate + 多个 dispense，且 asp_vol >= sum(disp_vols)，拆成多个 1:1 transfer
                asp_total = sum(a[0] for a in asp_block)
                if len(asp_block) == 1 and len(dispenses) >= 2 and asp_total >= sum(d[1] for d in dispenses):
                    first_split = True
                    for tgt_key, vol_d, dis_fr, liq_h, delay_seconds in dispenses:
                        single_asp = [(vol_d, asp_block[0][1])]
                        # 回吸只在第一笔 split 上记账，避免在拆分中被重复计算
                        split_pre_asp = pre_asp_from_target_vol if first_split else 0.0
                        transfers.append((src_key, tgt_key, single_asp, vol_d, dis_fr, current_tip_rack_slot, True, before_mix, after_mix, liq_h, has_touch_tip, delay_seconds, air_gap_before_vol, air_gap_after_vol, split_pre_asp, phase_channels))
                        first_split = False
                elif dispenses:
                    # ---- air gap + 分段 dispense 守恒模式合并 ----
                    # 涵盖两种典型形态（统称「tip 内 air-gap 装饰 + 多段 dispense 还原」）：
                    #
                    #   形态 1：aspirate 端含 top air-gap（_is_air_gap_pattern==True）
                    #     aspirate(main, src, bottom) + aspirate(ag, src, top)
                    #     dispense(ag, tgt, top)      + dispense(main, tgt, bottom)
                    #
                    #   形态 2：aspirate 全 bottom 但有独立 air_gap step（air_gap_aft > 0）
                    #     aspirate(main, src, bottom) + air_gap(ag)
                    #     dispense(ag, tgt, top)      + dispense(main, tgt, bottom)
                    #
                    # 共同特征：tip 内总体积 = asp_main + air_gap = sum(dispenses)，
                    # 且全部 dispense 指向同一 target。
                    #
                    # 旧实现只取 dispenses[0] → 下游公式 `adj_dis = dis[0] - blow` 把
                    # 主液量错算为 0（典型如 `00222e` 与 `7855ef-part3`）。
                    # 改为：把整段 dispenses 视为同一笔 transfer 的「分段排放」，
                    # dis_vol = sum(dispenses)；下游 air-gap 公式与 ot_style_air_gap
                    # 检测会还原主排液 vol = asp_main。
                    _pb_dry, _asp_main_dry, _pa_dry = _apply_pose_z_volumes(asp_block)
                    tip_total = (_asp_main_dry or 0) + (_pb_dry or 0) + (_pa_dry or 0) \
                                + (air_gap_before_vol or 0) + (air_gap_after_vol or 0)
                    dis_total_block = sum(d[1] for d in dispenses)
                    has_air_signal = (
                        _is_air_gap_pattern(asp_block)
                        or air_gap_before_vol
                        or air_gap_after_vol
                    )
                    same_target = all(d[0] == dispenses[0][0] for d in dispenses)
                    if (len(dispenses) >= 2 and has_air_signal
                            and abs(dis_total_block - tip_total) < 0.5
                            and same_target):
                        # 主排液段：选 vol 与 asp_main 最接近的 dispense（其 pose_z
                        # 通常是 bottom，用其 liquid_height / flow_rate / delay 作为代表）。
                        main_d_idx = min(range(len(dispenses)),
                                         key=lambda i_: abs(dispenses[i_][1] - (_asp_main_dry or 0)))
                        tgt_key, _, dis_fr, liq_h, delay_seconds = dispenses[main_d_idx]
                        merged_vol_d = dis_total_block
                        transfers.append((src_key, tgt_key, asp_block, merged_vol_d, dis_fr, current_tip_rack_slot, False, before_mix, after_mix, liq_h, has_touch_tip, delay_seconds, air_gap_before_vol, air_gap_after_vol, pre_asp_from_target_vol, phase_channels))
                    else:
                        tgt_key, vol_d, dis_fr, liq_h, delay_seconds = dispenses[0]
                        transfers.append((src_key, tgt_key, asp_block, vol_d, dis_fr, current_tip_rack_slot, False, before_mix, after_mix, liq_h, has_touch_tip, delay_seconds, air_gap_before_vol, air_gap_after_vol, pre_asp_from_target_vol, phase_channels))

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
        tr_channels = int(t[15]) if len(t) > 15 else 1
        tip_type = _tip_type_key(tip_slot)
        # channels 纳入分组键，避免同一 (src, tgt, tip_type) 但 multi/single 混用的 transfer 被合并
        key = (src, tgt, tip_type, tr_channels) if is_split else (src, tgt[0], tip_type, tr_channels)
        if key not in source_to_transfers:
            source_to_transfers[key] = []
        source_to_transfers[key].append((tgt, asp_block, dis_vol, dis_fr, tip_slot, before_mix, after_mix, liquid_height, touch_tip, delay_seconds, air_gap_bef, air_gap_aft, pre_asp_tgt))

    action_list = []
    for key, tlist in source_to_transfers.items():
        source_well = key[0]
        # 通道数：分组键的第 4 位（is_split/非 split 两种 key 结构共用）；缺失时按单通道处理
        group_channels = int(key[3]) if len(key) > 3 else 1
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
            # ---- Opentrons 风格 air_gap：dispense.vol 不含 air，独立 blow_out 清理 ----
            # 形如 aspirate(N, bottom) + air_gap(M) + dispense(N, bottom) [+ blow_out]：
            # dispense.vol == aspirate 主液总量；air_gap(M) 是 tip 末端缓冲气泡，
            # 通过紧随的 blow_out step 单独排到 trash，不参与 dis_vol 计算。
            # 旧实现把 air_gap_aft 加到 blow_after 后 adj_dis = dis_vol - air_gap_aft，
            # 当 air_gap_aft > dis_vol（如 aspirate(4)+air_gap(5)）时直接被 clip 到 0
            # 进而 asp_vol 也被覆盖为 0，整列体积归零。
            #
            # 判别：_apply_pose_z_volumes 未推断出 blow（asp_block 非 air-gap 形态），
            # 且 dis_vol ≈ asp_block 主液总量，且只有 air_gap_aft（无 air_gap_bef）。
            asp_main_total = sum(v for v, _ in asp_block)
            ot_style_air_gap = (
                air_gap_aft
                and not air_gap_bef
                and not (blow_before or blow_after)
                and abs(dis_vol - asp_main_total) < 0.5
            )
            if ot_style_air_gap:
                # 保留 air_gap 量到 blow_out_air_volume（runtime 需要知道空气量），
                # 但 asp_vol / dis_vol 都保持 aspirate 主液量，不互相扣减。
                blow_after = air_gap_aft
                adj_dis = dis_vol
                # asp_vol 已经是 _apply_pose_z_volumes 返回的 asp_main_total，无需覆盖
            else:
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
            "channels": group_channels,
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


def load_liquid_locations(
    protocol_name: str,
    original_dir: Optional[str] = None,
) -> Tuple[Dict[Tuple[int, str], str], List[str], Dict[Tuple[int, str], str]]:
    """加载 ``detailed_action_json/<name>.json`` 中的 liquid_locations 映射，
    并解析 ``original/<name>/README.md`` 中的语义名候选。

    返回:
        well_to_varname (dict[tuple[int, str], str]):
            ``(slot, well) → reagent_key``。已经过 :func:`_to_reagent_key`
            标准化；若 detailed_action_json 中含 ``liquid_name`` 字段（来自
            mock 层 ``define_liquid`` / ``load_liquid``）则优先采用，否则
            使用 Python 变量名（去掉数组下标 ``[N]``）。
        readme_candidates (list[str]):
            README.md 中抽取的语义名候选（小写、保留空格、**未** snake_case
            化）。消费方应再调用 :func:`_to_reagent_key`。文件不存在或解析
            失败时为空列表。
        well_to_liquid_name (dict[tuple[int, str], str]):
            P8 新增。``(slot, well) → 原始 liquid_name``（**未** ``_to_reagent_key``
            normalize，可含空格 / 中文 / 括号等）。仅当 detailed_action_json
            的 ``liquid_locations[*].liquid_name`` 字段存在（即 mock 端
            ``Well.load_liquid(liquid=...)`` 被调用过）时填充。消费方
            ``export_transfer_actions`` 用它给 reagent block entry 写
            可选 ``liquid_name`` 字段（详见 ``product_designs/protocol_convert/08-liquid-name-from-reagent-block.md``）。

    说明:
        ``original_dir`` 默认指向当前包 ``./original``；既允许相对当前工作
        目录的写法，也允许显式传 ``Path`` 对象（用于测试 / 跨脚本调用）。
    """
    well_to_varname: Dict[Tuple[int, str], str] = {}
    well_to_liquid_name: Dict[Tuple[int, str], str] = {}
    detailed_action_file = Path(__file__).parent / "detailed_action_json" / f"{protocol_name}.json"

    if not detailed_action_file.exists():
        print(f"  未找到 {detailed_action_file}，将使用自动生成的液体名称")
    else:
        try:
            with open(detailed_action_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            liquid_locs = data.get("liquid_locations", {})
            for var_name, loc_info in liquid_locs.items():
                try:
                    slot = int(loc_info.get("slot", 0))
                except (TypeError, ValueError):
                    continue
                well = loc_info.get("well", "")
                if not (slot and well):
                    continue
                # 优先级 1：mock 层显式名（define_liquid / Well.load_liquid 实参）
                explicit = loc_info.get("liquid_name")
                raw_name = explicit if explicit else re.sub(r"\[\d+\]$", "", var_name)
                normalized = _to_reagent_key(raw_name) or raw_name
                well_to_varname[(slot, well)] = normalized
                # P8：保留 raw liquid_name（未 normalize）作为独立通道。
                # 仅当 mock 端显式给了 liquid_name（``well.load_liquid`` 实参）才填充；
                # Python 变量名 fallback 不写（避免污染 reagent block）。
                if explicit:
                    well_to_liquid_name[(slot, str(well))] = str(explicit)

            print(f"  加载了 {len(well_to_varname)} 个原始试剂位置映射")
            if well_to_liquid_name:
                print(f"  P8: 加载了 {len(well_to_liquid_name)} 个显式 liquid_name 绑定")
        except Exception as e:
            print(f"  加载 liquid_locations 失败: {e}，将使用自动生成的液体名称")
            well_to_varname = {}
            well_to_liquid_name = {}

    # README 候选（无论 detailed_action_json 是否存在均尝试加载）
    if original_dir is None:
        original_dir = str(Path(__file__).parent / "original")
    readme_candidates: List[str] = []
    try:
        readme_candidates = load_reagents_from_readme(Path(original_dir) / protocol_name)
        if readme_candidates:
            print(f"  README 抽取出 {len(readme_candidates)} 个 reagent 候选名")
    except Exception as e:
        print(f"  解析 README 失败: {e}，将退化到变量名/Liquid_N 命名")

    return well_to_varname, readme_candidates, well_to_liquid_name


# ============================================================================
# P8 v1.3 — `.py` 源码 + README 段落 → 化学名抽取（§9.4）
# ----------------------------------------------------------------------------
# 当 mock 端没有显式 ``define_liquid`` / ``Well.load_liquid``、且 reagent_key
# 也是泛化容器名（如 ``full_tube_map`` / ``samples``）时，前两条通路全部失效。
# v1.3 在 §3.3 优先级链中插入两条新信号源：
#   (a) ``original/<name>/<name>.ot2.apiv2.py`` 扫描 → 变量名注释 / 关键词 /
#       别名链（``samples = full_tube_map[12:]`` → 追溯到 ``urine_tube_rack``）。
#   (b) ``original/<name>/README.md`` 的 ``### Protocol Steps`` 段落抽取
#       「Add <vol> <unit> of <liquid> to <wells_hint>」三元组，按 wells_hint
#       反查 reagent_key（不再仅用作 reagent_key 命名）。
# ============================================================================

# 用于 .py 源码扫描 + README 反查的化学语义关键词清单（按 §9.4 §3.3.A 维护）。
# 命中即返回字符串原样（不做 snake_case；外部根据上下文再决定是否过滤泛化词）。
_CHEMISTRY_KEYWORDS: Tuple[str, ...] = (
    # 基础溶剂
    "ethanol", "alcohol", "methanol", "isopropanol",
    "water", "pbs", "buffer", "plasma", "serum",
    "agar", "media", "sample",
    # §9.4 扩充（生物医药 + 缓冲液）
    "urine", "blood", "saliva", "dna", "rna", "enzyme",
    "spike", "primer", "master_mix", "mastermix",
    "reagent", "dilution", "diluent", "eluent",
    "wash", "lysis", "elution", "supernatant",
    "beads", "beadsbuffer", "magnetic_beads",
    "gel", "dye", "oil", "mineral_oil",
    "tris", "edta", "mgcl2", "nacl", "hcl", "naoh",
    "glucose", "glycerol", "formamide",
    "acetonitrile", "dmso", "dmf", "chloroform",
    "control", "blank", "standard", "calibrator", "calibrant",
)

# Python 变量赋值行匹配：``name = ...`` 或 ``name: type = ...``；仅顶层 / 函数体内一层缩进。
_RE_PY_VAR_ASSIGN = re.compile(
    r"^[ \t]*([a-zA-Z_][a-zA-Z0-9_]*)\s*(?::\s*[^=]+)?\s*=\s*(.+?)(?:\s*#\s*(.*))?$"
)
# 变量名是否含化学语义关键词（按 `_` 切分 token 检查；与 _CHEMISTRY_KEYWORDS 严格相等）
def _var_name_chemistry_hits(var_name: str) -> List[str]:
    tokens = [t for t in str(var_name or "").lower().split("_") if t]
    keyword_set = set(_CHEMISTRY_KEYWORDS)
    hits: List[str] = []
    for tok in tokens:
        if tok in keyword_set:
            hits.append(tok)
    # 整名命中（如 `magnetic_beads`）也算
    full = "_".join(tokens)
    if full and full in keyword_set and full not in hits:
        hits.append(full)
    return hits


def _comment_chemistry_hits(comment: str) -> List[str]:
    if not comment:
        return []
    lower = comment.lower()
    hits: List[str] = []
    for kw in _CHEMISTRY_KEYWORDS:
        # 简化策略：直接做子串匹配；带 `_` 的关键词也按子串匹配（容忍 `master mix` / `master_mix`）
        kw_norm = kw.replace("_", " ")
        if kw in lower or kw_norm in lower:
            if kw not in hits:
                hits.append(kw)
    return hits


def _extract_rhs_var_refs(rhs: str) -> List[str]:
    """从赋值右侧表达式抽取被引用的其它变量名（用于别名链追溯）。

    简化策略：抓出所有 `[a-zA-Z_][a-zA-Z0-9_]*` 标识符；过滤掉 Python 关键字 /
    内置 / 常见 OpenTrons API 名词，剩下视作变量引用。
    """
    if not rhs:
        return []
    tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", rhs)
    _DROP = {
        # Python keywords / builtins / common Opentrons identifiers
        "for", "in", "if", "else", "and", "or", "not", "lambda", "True", "False", "None",
        "list", "tuple", "dict", "set", "range", "len", "sum", "min", "max", "int", "float",
        "str", "bool", "zip", "map", "filter", "enumerate", "sorted", "reversed", "iter",
        "itertools", "chain", "from_iterable", "math", "ceil", "floor", "round",
        "rows", "columns", "wells", "wells_by_name", "rows_by_name", "columns_by_name",
        "load_labware", "load_instrument", "load_module", "load_liquid", "define_liquid",
        "ctx", "self", "label", "slot", "deck", "name", "description",
        # numbers / single letters
    }
    out: List[str] = []
    for tok in tokens:
        if tok in _DROP:
            continue
        if tok.isdigit():
            continue
        if len(tok) == 1:
            continue
        if tok not in out:
            out.append(tok)
    return out


def _ast_collect_var_refs(node: Any) -> List[str]:
    """递归遍历 AST 节点，抽取所有 ``Name`` (变量引用) 节点的 id。

    用于赋值 RHS 的别名链追溯：不依赖正则，因此对多行表达式 / 列表推导 /
    嵌套函数调用都鲁棒。
    """
    import ast
    refs: List[str] = []

    class _NameCollector(ast.NodeVisitor):
        def visit_Name(self, n: Any) -> None:
            if n.id not in refs:
                refs.append(n.id)
            # ast.Name 是叶节点，不需要 generic_visit

    _NameCollector().visit(node)
    return refs


def _load_python_var_chemistry_hints(
    original_dir: Path,
    protocol_name: str,
) -> Dict[str, List[str]]:
    """P8 v1.3 §3.3.A — 扫描 ``original/<name>/<name>.ot2.apiv2.py``。

    返回:
        ``Dict[var_name, List[chemistry_keyword]]``。命中来源（按优先级递减）：
        1. 赋值行 inline comment 含关键词；
        2. 变量名本身命中（按 ``_`` 切分 token）；
        3. RHS 别名链追溯（``foo = bar[i:j]`` → 把 ``bar`` 的命中关键词继承给 ``foo``，
           深度 ≤ 6 防环）。

    实现：
        - 用 ``ast.parse`` 解析整个文件，遍历所有 ``Assign`` / ``AnnAssign`` /
          ``AugAssign`` / ``For`` 节点拿到 (target_name, rhs_node)；
        - 用 ``tokenize`` 抽取赋值行 inline comments；
        - 对每个 var_name 累计 direct_hits（关键词命中）+ rhs_refs（别名链来源）。

    设计约束：
        - 只读文件，不执行；找不到 ``.py`` 文件返回 ``{}``；
        - 同一 var 多次命中合并去重，按出现顺序保留；
        - AST 解析失败（语法错误 / 编码异常）静默返回 ``{}``，**不**阻塞 export 管线。
    """
    if not isinstance(original_dir, Path):
        original_dir = Path(original_dir)
    protocol_root = original_dir / protocol_name
    if not protocol_root.exists():
        return {}
    # 兼容 `<name>.ot2.apiv2.py` 与 `<name>.py` 两种命名
    candidates = [
        protocol_root / f"{protocol_name}.ot2.apiv2.py",
        protocol_root / f"{protocol_name}.py",
    ]
    try:
        candidates.extend(sorted(protocol_root.glob("*.py")))
    except Exception:
        pass

    py_file: Optional[Path] = None
    for cand in candidates:
        if cand.exists() and cand.is_file():
            py_file = cand
            break
    if py_file is None:
        return {}

    try:
        text = py_file.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {}

    # 1) 用 tokenize 收集 ``line_no → comment``（用于变量赋值行的 inline comment 命中）
    import io
    import tokenize
    line_comments: Dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                # tok.start = (line_no, col)
                line_no = tok.start[0]
                # 去掉 leading `#` 与空白
                comment = tok.string.lstrip("#").strip()
                if comment:
                    line_comments[line_no] = comment
    except (tokenize.TokenizeError, IndentationError):
        # tokenize 失败不影响后续 AST 解析
        line_comments = {}

    # 2) AST 解析全文，遍历所有赋值节点
    import ast
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}

    direct_hits: Dict[str, List[str]] = {}
    rhs_refs: Dict[str, List[str]] = {}

    def _add_hit(var_name: str, hit: str) -> None:
        if not var_name or not hit:
            return
        existing = direct_hits.setdefault(var_name, [])
        if hit not in existing:
            existing.append(hit)

    def _add_refs(var_name: str, refs: List[str]) -> None:
        if not var_name or not refs:
            return
        existing = rhs_refs.setdefault(var_name, [])
        for r in refs:
            if r != var_name and r not in existing and not r.startswith("_"):
                existing.append(r)

    def _process_assignment(target: Any, value_node: Any, line_no: int) -> None:
        # 提取被赋值的 target 名（仅支持 Name；Tuple/List 解构在协议里罕见，跳过）
        if isinstance(target, ast.Name):
            var_name = target.id
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                _process_assignment(elt, value_node, line_no)
            return
        else:
            return
        if not var_name or var_name.startswith("_"):
            return
        # 1) inline comment 命中
        comment = line_comments.get(line_no, "")
        for hit in _comment_chemistry_hits(comment):
            _add_hit(var_name, hit)
        # 2) 变量名命中
        for hit in _var_name_chemistry_hits(var_name):
            _add_hit(var_name, hit)
        # 3) RHS 别名链
        if value_node is not None:
            refs = _ast_collect_var_refs(value_node)
            # 过滤掉 Python 内置 / 常见 API 名
            _DROP = {
                "list", "tuple", "dict", "set", "range", "len", "sum", "min",
                "max", "int", "float", "str", "bool", "zip", "map", "filter",
                "enumerate", "sorted", "reversed", "iter", "itertools",
                "math", "ctx", "self", "True", "False", "None",
            }
            refs = [r for r in refs if r not in _DROP and not r.startswith("_") and len(r) > 1]
            _add_refs(var_name, refs)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                _process_assignment(tgt, node.value, node.lineno)
        elif isinstance(node, ast.AnnAssign):
            _process_assignment(node.target, node.value, node.lineno)
        elif isinstance(node, ast.AugAssign):
            _process_assignment(node.target, node.value, node.lineno)
        elif isinstance(node, ast.For):
            # ``for tube, well in zip(samples, ...):`` 中 ``samples`` 作为 iter
            # 的 RHS 引用，可以帮助识别 tube/well → samples 的关联。
            _process_assignment(node.target, node.iter, node.lineno)

    if not direct_hits and not rhs_refs:
        return {}

    # 别名链：BFS 追溯 `var → ref → ref_of_ref` 最多 6 层。
    # 6 层来自 00a6e5：samples → full_tube_map → tube_map_nest →
    # tube_map_double_nest → urine_tube_rack（4 跳）；预留 +2 给更复杂协议。
    resolved: Dict[str, List[str]] = {k: list(v) for k, v in direct_hits.items()}
    for var_name in list(rhs_refs.keys()):
        if var_name in resolved and resolved[var_name]:
            continue
        seen = {var_name}
        queue = list(rhs_refs.get(var_name, []))
        depth = 0
        inherited: List[str] = []
        while queue and depth < 6:
            next_queue: List[str] = []
            for ref in queue:
                if ref in seen:
                    continue
                seen.add(ref)
                if ref in direct_hits:
                    for hit in direct_hits[ref]:
                        if hit not in inherited:
                            inherited.append(hit)
                # 继续追溯 ref 的 RHS
                for deeper in rhs_refs.get(ref, []):
                    if deeper not in seen:
                        next_queue.append(deeper)
            queue = next_queue
            depth += 1
        if inherited:
            resolved[var_name] = inherited

    return resolved


# README ``### Protocol Steps`` / ``### Description`` 中
# ``Add <vol> <unit> of <liquid> to <wells_hint>`` 模式
# 单位词支持缩写（uL/mL/μL）+ 全写（microliters/milliliters/microlitres/millilitres）。
# 单位 + ``of`` 之间 ``of`` 在大多数协议里都存在；把"liquid"限定为 ``of`` 之后开始，
# 避免把 ``microliters of Enzyme`` 整段误抓为 liquid 名。
_RE_README_ADD_STEP = re.compile(
    r"""
    \bAdd\s+
    (?:[0-9.,]+\s*)?                                                # 体积数字（可选）
    (?:                                                             # 单位（可选）
        µ?[uU]?[lL]                                                 #   uL / L
      | [mM][lL]                                                    #   mL
      | [Mm]icro[lL]ite?r?s?                                        #   microliter(s) / microlitre(s)
      | [Mm]illi[lL]ite?r?s?                                        #   milliliter(s) / millilitre(s)
    )?\s*
    (?:of\s+)                                                       # `of` 连接词（**强制**）
    (?P<liquid>[A-Za-z][A-Za-z0-9\-]*(?:\s+[A-Za-z][A-Za-z0-9\-]*){0,3})  # 试剂名 1-4 个词
    \s+to\s+
    (?P<wells>(?:all\s+wells|[A-H][0-9]+(?:[\s\-,]+(?:[A-H][0-9]+|to))*|tubes?\s+[A-H0-9\-, ]+|rows?\s+[A-H,\- ]+))
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)


def _extract_liquid_steps_from_readme(readme_text: str) -> List[Dict[str, str]]:
    """P8 v1.3 §3.3.B — 从 README ``Protocol Steps`` 段抽取「液体 → wells_hint」映射。

    返回:
        ``List[{"liquid": str, "wells_hint": str}]``，按出现顺序。``wells_hint``
        保留原字符串（如 ``"all wells"`` / ``"A1"`` / ``"A2-A8"`` / ``"tubes 2-12"``）。
        消费方按 (slot, well) 反查时再做拆解。

    设计约束：
        - 仅扫描 ``### Protocol Steps`` 与 ``### Description`` 段，避免误捕
          README ``Process`` 段中的"Add Labware"等流程描述。
        - 找不到段或正则未命中返回 ``[]``，不阻塞 export 管线。
    """
    if not readme_text:
        return []
    sections = _split_markdown_sections(readme_text)
    interesting_headers = ("protocol steps", "description")
    body_parts: List[str] = []
    for header, body in sections.items():
        if str(header).strip().lower() in interesting_headers:
            body_parts.append(body)
    if not body_parts:
        return []
    full_body = "\n".join(body_parts)

    results: List[Dict[str, str]] = []
    for match in _RE_README_ADD_STEP.finditer(full_body):
        liquid = (match.group("liquid") or "").strip()
        wells_hint = (match.group("wells") or "").strip()
        if not liquid or not wells_hint:
            continue
        # 过滤掉显然是停用词起头的（"add liquids"/"add tubes" 等）
        lower_first = liquid.split()[0].lower()
        if lower_first in {"liquids", "tubes", "wells", "all", "the", "more", "additional"}:
            continue
        results.append({"liquid": liquid, "wells_hint": wells_hint})
    return results


def _attach_liquid_name_to_reagents(
    reagents: Dict[str, Dict[str, Any]],
    well_to_liquid_name: Dict[Tuple[int, str], str],
    well_to_varname: Optional[Dict[Tuple[int, str], str]] = None,
    workflow_name: str = "",
    readme_candidates: Optional[List[str]] = None,
    python_var_hints: Optional[Dict[str, List[str]]] = None,
    readme_well_steps: Optional[List[Dict[str, str]]] = None,
) -> None:
    """P8 — 为 ``source`` / ``target`` 类 reagent entry 注入 ``liquid_name`` 字段。

    设计（v1.3）：
        - **强制非空**（仅 ``source`` / ``target``）：源代码 / README / 关键词
          推断三条通路全部 miss 时，使用 ``Liquid_<idx>`` 自增兜底；字段绝不省略。
        - ``object`` 为 ``tiprack`` / ``trash`` 的 entry 不写 ``liquid_name``。
        - 同一 reagent_key 含多个 well 时，按 well 顺序取**首个命中**作为
          代表 liquid_name（典型场景：reagent 块经 P1 多通道扩展为整列 8 孔，
          同列同液，取 A1 即可）。
        - 已显式写过 ``liquid_name`` 的 entry：若是化学名则保留；若是 ``sources_2``
          这类系统占位名允许覆盖。

    参数（v1.3 新增 ``python_var_hints`` / ``readme_well_steps``）：
        reagents: ``export_transfer_actions`` 已构造好的 reagent 字典（**原地**修改）。
        well_to_liquid_name: ``(slot, well) → 原始 liquid_name``。来自
            :func:`load_liquid_locations` 第 3 返回值。
        well_to_varname: ``(slot, well) → reagent_key 基名``。来自
            :func:`load_liquid_locations` 第 1 返回值；仅在 explicit 名缺失时作为
            fallback 写入 ``liquid_name``，避免 Stage 3 回落到 ``sources_2`` 这类
            编号后缀 key。
        python_var_hints: ``var_name → [keyword, ...]``，来自
            :func:`_load_python_var_chemistry_hints`。用于"reagent_key 自身泛化
            （samples/full_tube_map）但通过别名链能映射到 urine/blood 等
            化学名"的场景。
        readme_well_steps: ``[{"liquid": str, "wells_hint": str}, ...]``，来自
            :func:`_extract_liquid_steps_from_readme`。当 reagent entry 的物理
            well 在 README 中被显式提及（``Add Plasma to A1``）时，取 README 原字符。
    """
    if not reagents:
        return

    _GENERIC_VAR_NAMES = {"source", "sources", "target", "targets", "sample", "samples", "liquid"}

    def _is_system_generated_name(name: str) -> bool:
        value = str(name or "").strip().lower()
        if not value:
            return True
        if value in _GENERIC_VAR_NAMES:
            return True
        return bool(re.match(r"^(source|sources|target|targets|sample|samples|liquid)_\d+$", value))

    keyword_set = set(_CHEMISTRY_KEYWORDS)

    def _hints_for_reagent_key(reagent_key: str) -> List[str]:
        """从 python_var_hints 找 reagent_key 对应的 chemistry hints。

        匹配策略：
            1. 直接命中 ``python_var_hints[reagent_key]``；
            2. 去掉 ``_<digits>`` 后缀再查（``samples_6`` → ``samples`` → hints）；
            3. 把 reagent_key 自身按 token 切分，命中关键词即返回。
        """
        if not python_var_hints:
            return []
        if reagent_key in python_var_hints:
            return python_var_hints[reagent_key]
        base = re.sub(r"_\d+$", "", reagent_key)
        if base and base in python_var_hints:
            return python_var_hints[base]
        # token 命中（如 reagent_key=`enzyme_mix` → 直接含 `enzyme`）
        token_hits = _var_name_chemistry_hits(reagent_key)
        return token_hits

    def _readme_step_match(reagent_key: str, wells: List[str]) -> Optional[str]:
        """根据 reagent_key + 物理 wells 在 README steps 里找匹配的液体名。"""
        if not readme_well_steps:
            return None
        rkey_lower = str(reagent_key or "").lower()
        for step in readme_well_steps:
            liquid = str(step.get("liquid") or "").strip()
            hint = str(step.get("wells_hint") or "").strip().lower()
            if not liquid or not hint:
                continue
            # wells_hint = "all wells" → 任何 entry 都可匹配（仅当 reagent_key 与液体名同基）
            if "all" in hint and "well" in hint:
                liquid_tokens = re.split(r"[\s_\-]+", liquid.lower())
                if any(tok in rkey_lower for tok in liquid_tokens if tok):
                    return liquid
                continue
            # wells_hint 含具体 well（如 "A1" / "A2-A8" / "tubes 2-12"）
            for well in wells:
                if not well:
                    continue
                if str(well).upper() in hint.upper():
                    return liquid
        return None

    def _derive_liquid_name_from_context(reagent_key: str, entry: Dict[str, Any]) -> str:
        """从协议上下文推断液体名（无显式名时兜底）。

        目标：尽量输出语义名（如 ``agar`` / ``sample`` / ``water`` / ``ethanol``），
        而不是 ``sources_6`` 这类流程变量名。
        """
        primary_text = " ".join([
            str(reagent_key or ""),
            str(entry.get("labware", "") or ""),
        ]).lower()
        workflow_text = str(workflow_name or "").lower()
        obj = str(entry.get("object") or "").lower()

        for kw in _CHEMISTRY_KEYWORDS:
            if kw in primary_text:
                if kw == "media":
                    return "medium"
                return kw

        # README 候选中若包含明确化学词（water/alcohol/...），可用于 source/target 的语义补全。
        if readme_candidates:
            for cand in readme_candidates:
                cand_text = str(cand or "").lower()
                for kw in _CHEMISTRY_KEYWORDS:
                    if kw in cand_text:
                        if kw == "media":
                            return "medium"
                        return kw

        # 场景化兜底：Agar Plating 中 target/source 分别映射 agar/sample
        if "agar" in workflow_text and obj == "target":
            return "agar"
        if workflow_name and obj == "source":
            return "sample"
        if workflow_name and obj == "target":
            return "target"
        return ""  # v1.3：空串触发外层 ``Liquid_<idx>`` 兜底

    # v1.3 ``Liquid_<idx>`` 自增计数器（仅在所有信号源都 miss 时启用）
    liquid_fallback_counter = {"n": 0}
    used_fallback_names: set = set()

    def _next_fallback_name() -> str:
        while True:
            liquid_fallback_counter["n"] += 1
            name = f"Liquid_{liquid_fallback_counter['n']}"
            if name not in used_fallback_names:
                used_fallback_names.add(name)
                return name

    # 预扫描已有 ``Liquid_<n>`` 占位名以避免冲突
    for _entry in reagents.values():
        if isinstance(_entry, dict):
            ln = str(_entry.get("liquid_name") or "")
            m = re.match(r"^Liquid_(\d+)$", ln)
            if m:
                used_fallback_names.add(ln)
                liquid_fallback_counter["n"] = max(
                    liquid_fallback_counter["n"], int(m.group(1))
                )

    for reagent_key, entry in reagents.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("object") not in ("source", "target"):
            continue
        existing_name = entry.get("liquid_name")
        # 若已有的 liquid_name 仍是 sources_6 / samples_2 这类系统占位名，则允许重写为语义名。
        if existing_name and not _is_system_generated_name(str(existing_name)):
            continue
        if existing_name:
            entry.pop("liquid_name", None)
        slot_raw = entry.get("slot")
        slot_int: Optional[int]
        if slot_raw is None:
            slot_int = None
        else:
            try:
                slot_int = int(slot_raw)
            except (TypeError, ValueError):
                slot_int = None
        wells = entry.get("well") or []
        if isinstance(wells, str):
            wells = [wells]

        # —— 优先级 1：mock 端 explicit liquid_name ——
        if slot_int is not None:
            for w in wells:
                ln = well_to_liquid_name.get((slot_int, str(w)))
                if ln:
                    entry["liquid_name"] = str(ln)
                    break

        # —— 优先级 2：Stage 1 Python 变量名（非泛化 **且** token 含化学关键词）——
        # 例如 ``negative_urine`` / ``buffer`` / ``enzyme`` 直接 hit；
        # 而 ``full_tube_map`` / ``plate_wells_by_row`` / ``chunk`` 这种容器型
        # 变量名虽非泛化，但不含化学关键词，应跳过让位给后续 .py 别名链。
        if not entry.get("liquid_name") and well_to_varname and slot_int is not None:
            for w in wells:
                fallback_name = well_to_varname.get((slot_int, str(w)))
                if not fallback_name:
                    continue
                fname_low = str(fallback_name).strip().lower()
                if fname_low in _GENERIC_VAR_NAMES:
                    continue
                # 只接受 token 命中化学关键词的变量名（避免 plate_wells_by_row 这类）
                if _var_name_chemistry_hits(fallback_name):
                    entry["liquid_name"] = str(fallback_name)
                    break

        # —— 优先级 3：``.py`` 别名链 / 注释命中（v1.3 §3.3.A）——
        if not entry.get("liquid_name"):
            py_hits = _hints_for_reagent_key(str(reagent_key))
            if py_hits:
                # 取第一个非泛化命中
                for hit in py_hits:
                    if hit and hit.lower() not in _GENERIC_VAR_NAMES:
                        entry["liquid_name"] = hit
                        break

        # —— 优先级 4：README well_hint 反查（v1.3 §3.3.B）——
        if not entry.get("liquid_name"):
            ln = _readme_step_match(str(reagent_key), [str(w) for w in wells])
            if ln:
                entry["liquid_name"] = ln

        # —— 优先级 5：关键词推断（labware / workflow_name / readme_candidates）——
        if not entry.get("liquid_name"):
            ctx_name = _derive_liquid_name_from_context(str(reagent_key), entry)
            if ctx_name:
                entry["liquid_name"] = ctx_name

        # —— 优先级 5.5（v1.3 兜底前的最后机会）：Stage 1 非泛化变量名
        # （**即使不含化学关键词**），保留以与原 v1.1 行为兼容（避免某些
        # 协议 v1 已有非空 liquid_name 却被 v1.3 改写成 Liquid_<idx>）。
        if not entry.get("liquid_name") and well_to_varname and slot_int is not None:
            for w in wells:
                fallback_name = well_to_varname.get((slot_int, str(w)))
                if fallback_name and str(fallback_name).strip().lower() not in _GENERIC_VAR_NAMES:
                    entry["liquid_name"] = str(fallback_name)
                    break

        # —— 优先级 6（v1.3）：``Liquid_<idx>`` 自增兜底 ——
        # source/target entry 严格保证非空（即便所有信号源都 miss）
        if not entry.get("liquid_name"):
            entry["liquid_name"] = _next_fallback_name()


def process_protocol(protocol_name):
    """处理单个protocol，返回action_list和labware_data"""
    print(f"Processing {protocol_name}...")
    
    # 获取action_list - 寻找对应的steps文件
    steps_dir = Path(__file__).parent / "steps"
    
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


def set_liquid_info(results, well_to_varname=None, readme_candidates=None):
    """
    设置液体信息：为每个protocol的每个phase分配液体名称。

    新逻辑：
    1. 同一个phase中所有被aspirate的孔位 = 同一种液体
    2. 同一个phase中所有被dispense的孔位 = 同一种液体
    3. 如果孔位已经在之前的phase中分配过液体，使用已有的液体名称
    4. 更新labware_info中的liquid_type和liquid_input_wells
    5. **命名优先级链（自顶向下）**：
        a. ``well_to_varname[(slot, well)]`` —— 来自 detailed_action_json 的
           显式 ``liquid_name``（``define_liquid`` / ``Well.load_liquid``）
           或 Python 变量名（已 snake_case 标准化）
        b. ``readme_candidates`` —— 按 README 出现顺序填充 well_to_varname 未
           覆盖的孔位（仅在 ``UNILAB_PROTOCOL_KEEP_LEGACY_NAMES`` 未启用时生效）
        c. 自动生成 ``Liquid_N`` 兜底

    Args:
        results: ``{protocol_name: (action_list, labware_info)}``，会原地修改。
        well_to_varname: ``(slot, well) → reagent_key``；values 必须已经
            :func:`_to_reagent_key` 标准化。
        readme_candidates: README 中按出现顺序排序的语义名候选列表（小写、
            保留空格）；本函数会在内部 :func:`_to_reagent_key` 化后消费。
    """

    if well_to_varname is None:
        well_to_varname = {}
    if readme_candidates is None:
        readme_candidates = []

    # 过渡开关：保留旧 Liquid_N 命名（不接管 README 候选）。
    _keep_legacy = os.environ.get("UNILAB_PROTOCOL_KEEP_LEGACY_NAMES", "").strip().lower() in {"1", "true", "yes"}
    if _keep_legacy:
        readme_candidates = []

    for protocol_name, (action_list, labware_info) in results.items():
        print(f"处理 {protocol_name} 的液体信息...")

        # 跟踪每个孔位对应的液体名称: {(slot, well): liquid_name}
        well_to_liquid = {}

        # 液体计数器，用于生成唯一的液体名称
        liquid_counter = 1
        # 已使用的液体名称（用于避免重复）
        used_liquid_names = set()
        # README 候选游标（按 (slot, well) 首次出现顺序逐个消费）
        readme_cursor = 0
        # 已 snake_case 化的 README 候选缓存（保留原顺序）
        _readme_keys: List[str] = [k for k in (_to_reagent_key(c) for c in readme_candidates) if k]

        def get_liquid_name_for_well(slot, well):
            """为孔位获取液体名称（README > varname > Liquid_N）。"""
            nonlocal liquid_counter, readme_cursor
            well_key = (slot, well)

            # 如果已经分配过，直接返回
            if well_key in well_to_liquid:
                return well_to_liquid[well_key]

            # 优先级 1：well_to_varname（mock 显式名 / Python 变量名）
            if well_key in well_to_varname:
                original_name = well_to_varname[well_key]
                base_key = _to_reagent_key(original_name) or original_name
                if base_key and base_key not in used_liquid_names:
                    used_liquid_names.add(base_key)
                    return base_key
                if base_key:
                    new_name = _dedup_with_index(base_key, used_liquid_names)
                    used_liquid_names.add(new_name)
                    return new_name

            # 优先级 2：README 候选（按出现顺序消费；跳过已占用名称）
            while readme_cursor < len(_readme_keys):
                cand = _readme_keys[readme_cursor]
                readme_cursor += 1
                if not cand or cand in used_liquid_names:
                    continue
                used_liquid_names.add(cand)
                return cand

            # 优先级 3：Liquid_N 兜底
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
                first_asp = action["aspirate"][0]
                if first_asp is None or not isinstance(first_asp, (list, tuple)) or len(first_asp) < 2:
                    action["source_liquids"] = []
                    print(f"  跳过无效 aspirate 记录 (phase {phase_idx}): {action.get('aspirate')!r}")
                else:
                    slot, well = first_asp[0], first_asp[1]
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




def _has_nonzero_vols(args: Dict[str, Any]) -> bool:
    """判定一个 transfer_liquid 的 action_args 是否两端体积都非零。

    用于在 generate_transfer_actions 末尾兜底剔除无效 transfer：
      - asp_vols 全 0：源端没真正吸液（探针预热、被 back-aspirate 切断后只剩 air-gap 段）
      - dis_vols 全 0：目标端没真正排液（mix-in-place 被错误识别为 transfer、
        以及 air-gap 不平衡导致 dis 被减为 0 但 asp 仍保留主液量的情况）

    两端任意一端全 0 都不构成「液体净流入目标」，物理上无意义，过滤掉比让
    runtime 静默执行更安全。同时拒绝两端全为空数组（无任何 transfer 数据）。
    """
    asp = args.get("asp_vols") or []
    dis = args.get("dis_vols") or []
    if not asp and not dis:
        return False
    asp_nonzero = any(float(v or 0) > 0 for v in asp) if asp else False
    dis_nonzero = any(float(v or 0) > 0 for v in dis) if dis else False
    return asp_nonzero and dis_nonzero


def generate_transfer_actions(protocol_name):
    """
    生成transfer_liquid格式的actions
    只包含同时有aspirate和dispense的有效phases
    """
    try:
        action_list, labware_info = process_protocol(protocol_name)
        results = {protocol_name: (action_list, labware_info)}
        
        # 加载原始试剂名称映射 + README 语义名候选
        # P8：第 3 元 ``well_to_liquid_name`` 是原始 liquid_name（未 normalize），
        # 由 ``export_transfer_actions`` 写入 reagent block 的可选 ``liquid_name`` 字段；
        # ``set_liquid_info`` 阶段不消费它。
        well_to_varname, readme_candidates, _well_to_liquid_name = load_liquid_locations(protocol_name)

        # 设置液体信息（静默处理）
        import sys
        from io import StringIO
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        results = set_liquid_info(results, well_to_varname, readme_candidates)
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
            # P1 多通道意图：phase 通道数 == 8 时写 use_channels；单通道省略（保持向后兼容）
            phase_channels = int(phase.get('channels', 1) or 1)
            if phase_channels == 8:
                action_args['use_channels'] = [0, 1, 2, 3, 4, 5, 6, 7]
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
        # 3. P1 多通道展开：对带 use_channels 的 transfer_liquid，把 asp_vols / dis_vols 等
        #    逐项数组按 use_channels 长度 ×N 复制（每个列锚条目展开为 N 个等值条目）。
        transfer_actions = _replicate_per_channel_for_multi(transfer_actions)
        # 4. 兜底过滤：剔除「asp_vols 全 0 且 dis_vols 全 0」的 transfer。
        #    这种 transfer 通常来自：
        #      - 探针位置预热的 0-vol aspirate/dispense（如 0e7175 协议起始）
        #      - source==target 的 mix-in-place（被错误归并为 transfer）
        #      - 复杂 mix-dilute 模式下，主排液段被 back-aspirate 切断只剩 air-gap 段
        #    它们没有物理意义且会让 runtime 报「empty transfer」错误，比静默执行更安全。
        transfer_actions = [
            a for a in transfer_actions
            if _has_nonzero_vols(a.get("action_args") or {})
        ]

        return transfer_actions, updated_labware_info
        
    except Exception as e:
        print(f"生成 transfer actions 失败: {e}")
        return [], []


def _simplify_transfer_actions(transfer_actions):
    """
    简化：若多个1:1 transfer的source都是同一孔位，合并为1:N（如 l1[C1]->96个target）。
    这样 l1[C1]->A1, l1[C1]->B1, ... 会合并成 l1[C1]->[A1,B1,...,96孔]

    P2 v2（``02-cross-slot-merge.md`` §3.1.2）：分组键去 ``tgt_slot``，跨任意 target slot
    的 1:1 也可合并；合并后通过 ``_target_slots: list[int]`` 平铺记录每次 dispense 的目标
    slot，export 阶段据此反查 reagent_key。
    """
    if len(transfer_actions) <= 1:
        return transfer_actions

    # 按 (source_slot, source_well, tip_labware_type, use_channels) 分组（v2：去掉 tgt_slot）
    # tip rack 类型不同的 transfer 不能合并；multi / single 通道不同的 transfer 也不能合并
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
        uc = action.get('action_args', {}).get('use_channels')
        uc_key = tuple(uc) if isinstance(uc, list) else None
        key = (src_slot, src_wells[0], tip_ltype, uc_key)
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
        target_slots: List[Any] = []          # v2 新增：与 target_wells 平行的 dispense slot 序列
        for a in actions:
            target_wells.append(a['_target_wells'][0])
            target_slots.append(a.get('_target_slot'))
            args['asp_vols'].append(a['action_args']['asp_vols'][0])
            args['dis_vols'].append(a['action_args']['dis_vols'][0])
            args['asp_flow_rates'].append(a['action_args'].get('asp_flow_rates', [7.6])[0])
            args['dis_flow_rates'].append(a['action_args'].get('dis_flow_rates', [7.6])[0])
            if 'blow_out_air_volume' in a['action_args']:
                args.setdefault('blow_out_air_volume', []).append(a['action_args']['blow_out_air_volume'][0])
            if 'blow_out_air_volume_before' in a['action_args']:
                args.setdefault('blow_out_air_volume_before', []).append(
                    a['action_args']['blow_out_air_volume_before'][0])
            if 'pre_aspirate_from_target' in a['action_args']:
                args.setdefault('pre_aspirate_from_target', []).append(
                    a['action_args']['pre_aspirate_from_target'][0])
            if 'liquid_height' in a['action_args']:
                args.setdefault('liquid_height', []).append(a['action_args']['liquid_height'][0])
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

        # _target_slot 标量：保留首条 slot（兼容旧 export 单 slot 退化路径）；
        # _target_slots：v2 跨 slot 反查 reagent_key 的真值来源。
        simplified.append({
            "action": "transfer_liquid",
            "action_args": args,
            "_source_slot": first['_source_slot'],
            "_source_wells": source_wells,
            "_target_slot": first['_target_slot'],
            "_target_slots": target_slots,
            "_target_wells": target_wells,
            "_tip_labware_type": first.get('_tip_labware_type', ''),
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
    """相邻两条是否满足合并条件。

    P2 v2（参见 ``product_designs/protocol_convert/02-cross-slot-merge.md`` §3.1.1）：
    **放宽 ``_target_slot`` 约束** —— 只要源 slot / tip 量程档 / use_channels 维度一致，
    跨任意 ``_target_slot`` 也允许合并。target 维度由 export 阶段下沉到 reagent_key list
    去区分（见 §3.1.4）。

    P2 v2 修复（§12.6.1，2026-05-21）：**收紧 ``_source_wells`` 等价约束** —— 合并轴
    严格限制在 target 维度。若两条相邻 transfer 的 source wells 不同（即每次 aspirate
    来自不同的 source 孔位），即便它们来自同一 source slot 也禁止合并，否则会丢失
    「source.A_i ↔ target.A_i」的列锚定语义（典型场景：51b9a5 跨 9 plate 分发 A1..A12）。
    """
    sa = a.get('_source_slot')
    sb = b.get('_source_slot')
    if sa is None or sb is None:
        return False
    if sa != sb:                                # 仅要求源 slot 相同（v2）
        return False
    if a.get('_tip_labware_type', '') != b.get('_tip_labware_type', ''):
        return False
    # multi / single 通道不同的 transfer 不可合并；缺失视为单通道默认
    if a.get('action_args', {}).get('use_channels') != b.get('action_args', {}).get('use_channels'):
        return False
    aw, tw = a.get('_source_wells', []), a.get('_target_wells', [])
    bw, uw = b.get('_source_wells', []), b.get('_target_wells', [])
    if len(aw) != len(tw) or len(bw) != len(uw):
        return False
    # P2 v2 §12.6.1：source 端「物理 well 集合」必须等价，合并轴只在 target 维度。
    # 用 set 而非 list 是为了兼容链式合并 —— 一条已合并 transfer 的 _source_wells
    # 可能是 [A1, A1, A1, ...]（同 well 多次 aspirate 平铺扩展），与一条未合并的
    # _source_wells = [A1] 在物理上等价；但 [A1] 与 [A2] 必须分开。
    # 多通道场景下 [A1..H1] vs [A2..H2] 等列锚不同的批次自然不等 → 不合并。
    if set(aw) != set(bw):
        return False
    return True


def _merge_two_transfer_actions(first: Dict[str, Any], second: Dict[str, Any]) -> Dict[str, Any]:
    """将相邻两条 mergeable 的 transfer 合并为一条（字段处理与旧版多段合并一致）。

    P2 v2：除原有的 ``_target_wells`` 平铺外，额外维护 ``_target_slots: list[int]``
    与 ``_target_wells`` 长度严格对齐（每次 dispense 一条），用于 export 阶段反查
    reagent_key（详见 ``02-cross-slot-merge.md`` §3.1.3）。
    """
    args = first['action_args'].copy()
    target_wells = list(first.get('_target_wells', []))
    source_wells = list(first.get('_source_wells', []))
    # _target_slots（v2 新增）：与 _target_wells 平行 —— 每次 dispense 落到哪个 slot
    first_target_slots = first.get('_target_slots')
    if first_target_slots is None:
        # 兼容上游未填 _target_slots 的旧路径：用首条 _target_slot 标量扩展到 len(_target_wells)
        first_slot_scalar = first.get('_target_slot')
        target_slots: List[Any] = (
            [first_slot_scalar] * len(target_wells) if first_slot_scalar is not None else []
        )
    else:
        target_slots = list(first_target_slots)
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
        # 平行维护 _target_slots
        a_slots = a.get('_target_slots')
        if a_slots is None:
            a_slot_scalar = a.get('_target_slot')
            if a_slot_scalar is not None:
                target_slots.extend([a_slot_scalar] * len(a.get('_target_wells', [])))
        else:
            target_slots.extend(a_slots)
        args['asp_vols'].extend(a['action_args']['asp_vols'])
        args['dis_vols'].extend(a['action_args']['dis_vols'])
        args['asp_flow_rates'].extend(a['action_args'].get('asp_flow_rates', []))
        args['dis_flow_rates'].extend(a['action_args'].get('dis_flow_rates', []))
        if 'blow_out_air_volume' in a['action_args']:
            if 'blow_out_air_volume' not in args:
                args['blow_out_air_volume'] = []
            args['blow_out_air_volume'].extend(a['action_args']['blow_out_air_volume'])
        if 'blow_out_air_volume_before' in a['action_args']:
            if 'blow_out_air_volume_before' not in args:
                args['blow_out_air_volume_before'] = []
            args['blow_out_air_volume_before'].extend(a['action_args']['blow_out_air_volume_before'])
        if 'pre_aspirate_from_target' in a['action_args']:
            if 'pre_aspirate_from_target' not in args:
                args['pre_aspirate_from_target'] = []
            args['pre_aspirate_from_target'].extend(a['action_args']['pre_aspirate_from_target'])
        if 'liquid_height' in a['action_args']:
            if 'liquid_height' not in args:
                args['liquid_height'] = []
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

    # _target_slot 标量：保留首条 slot，作为 export 阶段单 slot 退化路径的兼容入口；
    # 实际 dispense 落点全部由 _target_slots（list[int]）决定。
    return {
        "action": "transfer_liquid",
        "action_args": args,
        "_source_slot": first.get('_source_slot'),
        "_source_wells": source_wells,
        "_target_slot": first.get('_target_slot'),
        "_target_slots": target_slots,
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


# P1 多通道展开：multi 协议 transfer_liquid 的逐项数组需要按 use_channels 长度 ×N 复制
# 每个列锚条目展开为 N 个等值条目（multi pipette 物理上 N 个 tip 同时操作，体积一致）。
PER_CHANNEL_REPLICATE_FIELDS = (
    'asp_vols',
    'dis_vols',
    'asp_flow_rates',
    'dis_flow_rates',
    'blow_out_air_volume',
    'blow_out_air_volume_before',
    'pre_aspirate_from_target',
    'liquid_height',
    'delays',
)


def _replicate_each(values: List[Any], n: int) -> List[Any]:
    """[v0, v1, ..., v_{M-1}] → [v0]*n + [v1]*n + ... + [v_{M-1}]*n"""
    out: List[Any] = []
    for v in values:
        out.extend([v] * n)
    return out


def _replicate_per_channel_for_multi(transfer_actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """对带 use_channels 的 transfer_liquid 做最终 ×len(use_channels) 复制。

    P1 v4 约定：multi 协议下 `asp_vols / dis_vols / ...` 长度从列锚条目数 M 扩展为 8 × M，
    逐元素对应「第 floor(i/8) 个列锚条目、第 (i mod 8) 个通道」。
    sources / targets 形态保持不变（仍为 reagent_key 级引用），物理孔位通过 reagent[key].well 表达。
    """
    for action in transfer_actions:
        args = action.get('action_args')
        if not isinstance(args, dict):
            continue
        uc = args.get('use_channels')
        if not isinstance(uc, list) or len(uc) <= 1:
            continue
        n = len(uc)
        for field in PER_CHANNEL_REPLICATE_FIELDS:
            value = args.get(field)
            if isinstance(value, list) and value:
                args[field] = _replicate_each(value, n=n)
    return transfer_actions


# P1 多通道展开：reagent.well 按 labware 类型自动扩展
# - plate：列锚 well 展开为整列（96 well plate 默认 8 行 ABCDEFGH）
# - reservoir / trough：保持原样（单孔池子，runtime 按 8 通道共享单孔解释）
# - tiprack：保持原样（不在 multi reagent 集合中）
_RE_384_TOKEN = re.compile(r'(?<!\d)384(?!\d)')


def _labware_kind_of(labware_str: str) -> str:
    """根据 labware 类型字符串关键字推断 plate / reservoir / tiprack / plate_384。

    - plate（默认）：96 wellplate / 96 deep well 等，multi pipette 按 ABCDEFGH 8 行整列展开。
    - plate_384：含独立 `384` 数字 token（如 `_384_` / `_384` 结尾 / `384well`），multi pipette
      实际只取 8 行（间隔 1 行），不简单按 8 行展开；当前不展开（按 v4 §9 Q1.5 的留白），
      等 P1.5 / 后续 PR 单独处理。
    - reservoir / trough：单孔池子，保持原样。
    - tiprack：tip 资源，不在 multi reagent 集合中。
    """
    s = (labware_str or '').lower()
    if 'tiprack' in s or 'tip_rack' in s:
        return 'tiprack'
    if 'reservoir' in s or 'trough' in s:
        return 'reservoir'
    if _RE_384_TOKEN.search(s):
        return 'plate_384'
    return 'plate'


def _column_of_anchor(anchor_well: str, rows: str = 'ABCDEFGH') -> List[str]:
    """ "A1" → ["A1", "B1", ..., "H1"]；"A12" → ["A12", ..., "H12"]
    非 A 行起始或无数字列号则返回 [anchor_well]（不扩展）。"""
    if not anchor_well or len(anchor_well) < 2:
        return [anchor_well]
    if anchor_well[0] != 'A':
        return [anchor_well]
    col_index = anchor_well[1:]
    if not col_index.isdigit():
        return [anchor_well]
    return [f"{row}{col_index}" for row in rows]


def _expand_reagent_wells_for_multi(reagents: Dict[str, Dict[str, Any]],
                                    multi_reagent_keys: set) -> None:
    """对参与 multi-channel transfer 的 reagent，按 labware 类型扩展 well 列表。

    - plate 类（默认）：每个列锚 well 扩展为该列的 8 个物理 wells（去重保序）。
    - reservoir / trough / tiprack：保持原样。
    - 已经是 8 行整列（即不只 A 行）的 well 列表不再二次扩展。
    """
    for key in multi_reagent_keys:
        info = reagents.get(key)
        if not info:
            continue
        kind = _labware_kind_of(info.get('labware', ''))
        if kind != 'plate':
            continue
        original = info.get('well') or []
        if not original:
            continue
        expanded: List[str] = []
        seen = set()
        for w in original:
            for full_well in _column_of_anchor(w):
                if full_well not in seen:
                    seen.add(full_well)
                    expanded.append(full_well)
        info['well'] = expanded


def _collect_multi_reagent_keys(transfer_actions: List[Dict[str, Any]]) -> set:
    """收集所有出现在带 use_channels 的 transfer_liquid 中的 sources / targets reagent_key。"""
    keys: set = set()
    for action in transfer_actions:
        args = action.get('action_args') or {}
        uc = args.get('use_channels')
        if not (isinstance(uc, list) and len(uc) > 1):
            continue
        for field in ('sources', 'targets'):
            v = args.get(field)
            if isinstance(v, str) and v:
                keys.add(v)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and item:
                        keys.add(item)
    return keys


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
    # 注意：reagent 块构建仅需 well_to_varname；readme_candidates 已在
    # set_liquid_info 阶段消费到 transfer_actions.sources/targets，因此此处忽略。
    # P8：本函数仅用于打印 reagent 摘要，不写出 JSON，因此忽略 well_to_liquid_name。
    well_to_varname, _readme_candidates, _well_to_liquid_name = load_liquid_locations(protocol_name)

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
    # 注意：export 阶段 reagent 块的 key 来自 transfer_actions.sources/targets
    # （已被 set_liquid_info 用 README 优先级链命名），此处 well_to_varname
    # 仅作 fallback 用以补全 wells/slot；readme_candidates 不再消费。
    # P8：``well_to_liquid_name`` 是 mock 端 ``Well.load_liquid`` 实参原文
    # （未 normalize），最终注入到每个 source/target reagent entry 的
    # 可选 ``liquid_name`` 字段，供 Stage 3 ``workflow/common.py``
    # 优先于 reagent_key 写入 ``set_liquid_from_plate.param.liquid_names``。
    well_to_varname, readme_candidates, well_to_liquid_name = load_liquid_locations(protocol_name)

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

    def _register_target_reagent_key(liquid_base, slot, wells_for_slot):
        """注册（或复用）一个 target reagent_key。

        P2 v2：跨 slot 与单 slot 共用入口。``(liquid_base, slot, sorted(wells))``
        三元组命中已有 reagent 时直接复用；同 ``liquid_base`` 落到新的 slot/wells
        时生成 ``<base>_2/_3`` 后缀键，与原有 ``_next_unique_key`` 行为一致。
        """
        well_key = (liquid_base, slot, tuple(sorted(wells_for_slot)))
        if well_key in target_well_to_key:
            return target_well_to_key[well_key]
        if liquid_base not in reagents:
            target_well_to_key[well_key] = liquid_base
            reagents[liquid_base] = {
                "slot": slot,
                "well": _normalize_reagent_wells(wells_for_slot),
                "labware": slot_to_type.get(slot, ""),
                "object": "target"
            }
            return liquid_base
        new_key = _next_unique_key(liquid_base, target_key_counter)
        target_well_to_key[well_key] = new_key
        reagents[new_key] = {
            "slot": slot,
            "well": _normalize_reagent_wells(wells_for_slot),
            "labware": slot_to_type.get(slot, ""),
            "object": "target"
        }
        return new_key

    for action in transfer_actions:
        targets = action['action_args']['targets']
        target_slot = action.get('_target_slot')
        target_slots_list = action.get('_target_slots') or []
        target_wells = action.get('_target_wells', [])

        # 解析 liquid_base（merge 后 args['targets'] 已写为 tgt_base 单字符串）
        if isinstance(targets, list):
            liquid_base = targets[0] if targets else ''
        else:
            liquid_base = targets

        # ----- P2 v2 跨 slot 路径 -----
        # _target_slots 含 ≥2 个 unique 值 → 按 dispense 顺序拼 reagent_key list；
        # 同时为每个 unique slot 注册一个独立 reagent_key（共享 liquid_base）。
        if (
            liquid_base
            and target_slots_list
            and target_wells
            and len(target_slots_list) == len(target_wells)
            and len(set(target_slots_list)) > 1
        ):
            per_slot_wells: Dict[Any, List[str]] = {}
            for s, w in zip(target_slots_list, target_wells):
                per_slot_wells.setdefault(s, []).append(w)
            slot_to_key: Dict[Any, str] = {}
            for s, wells_for_slot in per_slot_wells.items():
                slot_to_key[s] = _register_target_reagent_key(liquid_base, s, wells_for_slot)
            target_reagent_keys = [slot_to_key[s] for s in target_slots_list]
            # 若所有 key 退化为同一字符串（理论上仅有 1 unique slot 时才会发生）→ 写回 str；
            # 否则保留 list 形态作为 dispense → reagent_key 顺序权威。
            if len(set(target_reagent_keys)) == 1:
                action['action_args']['targets'] = target_reagent_keys[0]
            else:
                action['action_args']['targets'] = target_reagent_keys
            continue

        # ----- 单 slot / 旧路径 -----
        targets_iter = [targets] if isinstance(targets, str) else (list(targets) if targets else [])
        for liquid in targets_iter:
            if target_slot is not None and target_wells:
                new_key = _register_target_reagent_key(liquid, target_slot, target_wells)
                action['action_args']['targets'] = new_key
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

    # P1 多通道：对参与 use_channels=[0..7] transfer 的 plate 类 reagent，
    # 把 well 列表从列锚（A1）扩展为整列 8 wells（A1..H1）；reservoir / trough 保持原样。
    multi_reagent_keys = _collect_multi_reagent_keys(transfer_actions)
    if multi_reagent_keys:
        _expand_reagent_wells_for_multi(reagents, multi_reagent_keys)

    # P5 — 协议级元数据：workflow_name + tags + raw（Opentrons metadata 原样）
    # 提前加载 metadata，以便 P8 在 fallback 推断液体名时使用 workflow_name 上下文。
    original_root = Path(__file__).parent / "original"
    original_dir = original_root / protocol_name
    metadata_obj = load_protocol_metadata(original_dir)

    # P8 v1.3 — 加载 .py 别名/注释扫描 + README well_hint 反查（§9.4 §3.3.A / §3.3.B）
    python_var_hints: Dict[str, List[str]] = {}
    readme_well_steps: List[Dict[str, str]] = []
    try:
        python_var_hints = _load_python_var_chemistry_hints(original_root, protocol_name)
        if python_var_hints:
            print(f"  P8 v1.3: .py 扫描得 {len(python_var_hints)} 个变量 chemistry hint")
    except Exception as e:
        print(f"  P8 v1.3: .py 扫描失败 {e}（不阻塞）")
    try:
        readme_path = original_dir / "README.md"
        if readme_path.exists():
            readme_text = readme_path.read_text(encoding="utf-8", errors="ignore")
            readme_well_steps = _extract_liquid_steps_from_readme(readme_text)
            if readme_well_steps:
                print(f"  P8 v1.3: README 抽取得 {len(readme_well_steps)} 个 (liquid, wells_hint) 步骤")
    except Exception as e:
        print(f"  P8 v1.3: README 抽取失败 {e}（不阻塞）")

    # P8 — 给每个 source / target reagent entry 注入 ``liquid_name`` 字段。
    # v1.3 起 source/target entry 字段强制非空（缺省走 ``Liquid_<idx>`` 兜底）。
    # 优先级链：mock define_liquid > Stage 1 变量名 > .py 别名/注释 > README 段落
    #           > 关键词推断 > Liquid_<idx>。
    # 详见 ``product_designs/protocol_convert/08-liquid-name-from-reagent-block.md`` §3.3 / §9.4。
    _attach_liquid_name_to_reagents(
        reagents,
        well_to_liquid_name,
        well_to_varname=well_to_varname,
        workflow_name=str(metadata_obj.get("workflow_name") or ""),
        readme_candidates=readme_candidates,
        python_var_hints=python_var_hints,
        readme_well_steps=readme_well_steps,
    )

    # 移除内部字段，不输出到JSON
    # P2 v2：``_target_slots`` 是 export 阶段反查 reagent_key 的辅助序列，同样不进入 JSON
    for action in transfer_actions:
        action.pop('_source_slot', None)
        action.pop('_source_wells', None)
        action.pop('_target_slot', None)
        action.pop('_target_slots', None)
        action.pop('_target_wells', None)
        action.pop('_tip_labware_type', None)

    # P5 — 协议级元数据：workflow_name + tags + raw（Opentrons metadata 原样）
    # 优先 mock 层落盘的 detailed_action_json.metadata；兜底 *.py 正则；
    # tags 单独从 README ## Categories 段抽。
    if not metadata_obj.get("workflow_name"):
        print(f"  [warn] {protocol_name}: workflow_name 为空（detailed_action_json + *.py 均未取到 protocolName）")
    if not metadata_obj.get("tags"):
        print(f"  [warn] {protocol_name}: tags 为空（README.md 中未找到 ## Categories 或段内无列表项）")

    # 顶层字段顺序：metadata → workflow → reagent，便于 diff 时 metadata 段独立审阅
    output_data = {
        "metadata": {
            "workflow_name": metadata_obj.get("workflow_name", ""),
            "tags": list(metadata_obj.get("tags") or []),
            "raw": metadata_obj.get("raw") or {},
        },
        "workflow": transfer_actions,
        "reagent": reagents,
    }

    if output_file is None:
        output_file = f"{protocol_name}_transfer_actions.json"
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)
    
    print(f"Transfer actions已导出到: {output_file}")
    return output_data


def copy_core_protocols(
    source_dir: Optional[str] = None,
    target_dir: Optional[str] = None,
    classification_file: Optional[str] = None,
    overwrite: bool = True,
) -> Dict[str, Any]:
    """筛选 ``classification_results.json`` 中标记 ``is_core: true`` 的核心 protocol，
    并从 ``source_dir`` 单独复制一份到 ``target_dir``。

    参数：
        source_dir: 转换好的 transfer_actions 来源目录。默认: 同级 ``transfer_actions_copy4``。
        target_dir: 核心协议目标目录。默认: 同级 ``core_protocol``。
        classification_file: 分类结果 JSON。默认: 同级 ``classification_results.json``。
        overwrite: 目标已存在时是否覆盖；False 时计入 ``skipped``。

    返回：``{"copied": [...], "missing": [...], "skipped": [...]}``，并在
    ``target_dir`` 下落盘 ``core_protocol_summary.json``。
    """
    import shutil

    script_dir = Path(__file__).parent
    src = Path(source_dir) if source_dir else script_dir / "transfer_actions_copy4"
    dst = Path(target_dir) if target_dir else script_dir / "core_protocol"
    cls_file = (
        Path(classification_file)
        if classification_file
        else script_dir / "classification_results.json"
    )

    print(f"\n[copy_core] 开始筛选核心 protocol")
    print(f"  source: {src}")
    print(f"  target: {dst}")
    print(f"  classification: {cls_file}")

    if not src.exists():
        print(f"[copy_core] 源目录不存在，已跳过: {src}")
        return {"copied": [], "missing": [], "skipped": [], "error": "source_dir_missing"}

    if not cls_file.exists():
        print(f"[copy_core] 分类文件不存在，已跳过: {cls_file}")
        return {"copied": [], "missing": [], "skipped": [], "error": "classification_missing"}

    try:
        with open(cls_file, "r", encoding="utf-8") as f:
            classification_data = json.load(f)
    except Exception as e:
        print(f"[copy_core] 读取分类文件失败: {e}")
        return {"copied": [], "missing": [], "skipped": [], "error": str(e)}

    core_names = [
        p.get("name", "")
        for p in classification_data.get("protocols", [])
        if p.get("is_core") is True and p.get("name")
    ]

    dst.mkdir(parents=True, exist_ok=True)

    copied: List[str] = []
    missing: List[str] = []
    skipped: List[str] = []

    for name in core_names:
        src_file = src / f"{name}.json"
        if not src_file.exists():
            missing.append(name)
            continue
        dst_file = dst / f"{name}.json"
        if dst_file.exists() and not overwrite:
            skipped.append(name)
            continue
        shutil.copy2(src_file, dst_file)
        copied.append(name)

    summary_file = dst / "core_protocol_summary.json"
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "source_dir": str(src),
                "target_dir": str(dst),
                "classification_file": str(cls_file),
                "total_core": len(core_names),
                "copied_count": len(copied),
                "missing_count": len(missing),
                "skipped_count": len(skipped),
                "copied": copied,
                "missing": missing,
                "skipped": skipped,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    print(f"[copy_core] 完成")
    print(f"  核心标记: {len(core_names)} 个")
    print(f"  已复制: {len(copied)}")
    print(f"  源缺失: {len(missing)}")
    if skipped:
        print(f"  已存在跳过: {len(skipped)} (overwrite=False)")
    if missing:
        print(f"  缺失示例: {missing[:5]}")
    print(f"  总览: {summary_file}")

    return {"copied": copied, "missing": missing, "skipped": skipped}


def batch_generate_transfer_actions(output_dir="transfer_actions", copy_core: bool = True):
    """批量生成所有协议的transfer actions。

    Args:
        output_dir: 输出目录。
        copy_core: 批量结束后是否将分类为核心（``is_core: true``）的 protocol
            复制一份到 ``core_protocol/``。默认 True；CLI 可通过 ``--no-copy-core`` 关闭。
    """
    import os
    
    steps_dir = Path(__file__).parent / "steps"
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
    skipped_empty: List[str] = []
    failed: List[Dict[str, str]] = []

    for i, protocol in enumerate(protocols, 1):
        try:
            print(f"[{i}/{len(protocols)}] 处理 {protocol}...")
            
            transfer_actions, labware_info = generate_transfer_actions(protocol)
            
            if not transfer_actions:
                print(f"  跳过 - 没有有效actions")
                skipped_empty.append(protocol)
                continue
            
            # 导出单个协议的actions，文件名就是方案名.json
            output_file = os.path.join(output_dir, f"{protocol}.json")
            export_data = export_transfer_actions(protocol, output_file)
            
            # 计算液体种类数量（sources/targets 可能为 str 或 list）
            all_liquids: set = set()
            for action in transfer_actions:
                for key in ("sources", "targets"):
                    v = action["action_args"].get(key)
                    if isinstance(v, str):
                        all_liquids.add(v)
                    elif isinstance(v, list):
                        for item in v:
                            if isinstance(item, str):
                                all_liquids.add(item)
            
            results_summary.append({
                "protocol": protocol,
                "actions_count": len(transfer_actions),
                "liquids_count": len(all_liquids)
            })
            
            success_count += 1
            print(f"  成功 - {len(transfer_actions)} actions")
            
        except Exception as e:
            print(f"  失败: {e}")
            failed.append({"protocol": protocol, "error": str(e)})
            continue
    
    # 生成总览文件
    summary_file = os.path.join(output_dir, "batch_summary.json")
    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump({
            "total_protocols": len(protocols),
            "successful_protocols": success_count,
            "skipped_empty_count": len(skipped_empty),
            "failed_count": len(failed),
            "skipped_empty": skipped_empty,
            "failed": failed,
            "results": results_summary
        }, f, indent=2, ensure_ascii=False)
    
    print(f"\n批量处理完成!")
    print(f"  成功: {success_count}/{len(protocols)}")
    print(f"  跳过(无有效 transfer): {len(skipped_empty)}")
    print(f"  失败(异常): {len(failed)}")
    print(f"  输出目录: {output_dir}")
    print(f"  总览文件: {summary_file}")

    if copy_core:
        copy_core_protocols(source_dir=output_dir)
    else:
        print("[copy_core] 已通过参数关闭核心 protocol 复制（copy_core=False）")


if __name__ == "__main__":
    # 选择运行模式
    import sys

    argv = sys.argv[1:]

    # 专门的筛选/复制开关：默认启用核心 protocol 复制；--no-copy-core 关闭
    copy_core_flag = True
    if "--no-copy-core" in argv:
        copy_core_flag = False
        argv = [a for a in argv if a != "--no-copy-core"]

    if argv and argv[0] == "copy-core":
        # 独立模式：仅根据 classification_results.json 复制核心 protocol，不重跑批量
        # 用法: python change_to_transfer_group.py copy-core [source_dir] [target_dir]
        src = argv[1] if len(argv) > 1 else None
        dst = argv[2] if len(argv) > 2 else None
        copy_core_protocols(source_dir=src, target_dir=dst)
    elif argv and argv[0] == "batch":
        # 批量模式 - 支持自定义输出目录
        output_dir = argv[1] if len(argv) > 1 else "transfer_actions"
        batch_generate_transfer_actions(output_dir, copy_core=copy_core_flag)
    else:
        # 无参数或与 batch 无关的首参：默认输出到 transfer_actions_copy4 并批量处理全部 steps
        # 生成 transfer_actions_copy4 在 change_to_transfer_group.py 的同级目录
        script_dir = os.path.dirname(os.path.abspath(__file__))
        output_dir = (
            os.path.join(script_dir, argv[0])
            if argv
            else os.path.join(script_dir, "transfer_actions_copy4")
        )

        batch_generate_transfer_actions(output_dir, copy_core=copy_core_flag)
