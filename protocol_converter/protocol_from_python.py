"""
从 protocol_converter/original 中的 *.ot2.apiv2.py 协议文件生成 steps JSON。
通过 mock 执行协议并捕获操作，输出与 prcxi 解析 log 后相同的 steps 格式。

用法:
  python protocol_from_python.py           # 批量处理 original/ 下所有协议
  python protocol_from_python.py 0e39fc   # 仅处理 0e39fc
  python run_steps_from_python.py [name]  # 同上（独立入口，无 prcxi 依赖）

支持: 基础液体操作 (aspirate/dispense/mix/delay/pick_tip/drop_tip)。
部分协议因使用 load_module/transfer/flow_rate 等 API 可能失败。
"""
import json
import os
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional

# 默认 flow_rate (p20/p300 常用)
_DEFAULT_FLOW_RATE = 7.6


def _stringify_for_json(value: Any) -> Any:
    """递归把 metadata 值序列化为 JSON 安全的标量 / list / dict。

    Opentrons 协议的 ``metadata`` 字段通常都是字符串 / 数字 / bool / None，
    但偶有作者会塞 Path / 自定义对象。统一兜底为 ``str(value)``。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_stringify_for_json(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _stringify_for_json(v) for k, v in value.items()}
    return str(value)

# 标准 well ordering
_96_WELL_ORDER = [f"{r}{c}" for c in range(1, 13) for r in "ABCDEFGH"]
_384_WELL_ORDER = [f"{r}{c}" for c in range(1, 25) for r in "ABCDEFGHIJKLMNOP"]
_12_RESERVOIR_ORDER = [f"A{c}" for c in range(1, 13)]
# 24 孔板: 4 行 x 6 列 (A1-D6)
_24_WELLPLATE_ORDER = [f"{r}{c}" for c in range(1, 7) for r in "ABCD"]
# 24 孔 tube rack: 4 行 x 6 列 (A1-D6)
_24_TUBE_ORDER = [f"{r}{c}" for c in range(1, 7) for r in "ABCD"]

# OT-2 固定废液槽的标准 load_name（用于从 opentrons_shared_data 加载真实定义）
_TRASH_LOAD_NAME = "opentrons_1_trash_1100ml_fixed"


def _make_trash_labware(protocol_dir: "Path") -> "MockLabware":
    """从 opentrons_shared_data 加载真实 trash 定义，构建 slot 12 的 MockLabware。"""
    defn = load_labware_def(protocol_dir, _TRASH_LOAD_NAME)
    order = _flatten_ordering(defn["ordering"]) if defn and "ordering" in defn else ["A1"]
    return MockLabware(12, None, _TRASH_LOAD_NAME, order, defn=defn)


class NumericString(str):
    """字符串数字，同时兼容与 int 比较。"""

    def __eq__(self, other):
        if isinstance(other, int):
            return int(self) == other
        return super().__eq__(other)

    def __int__(self):
        return int(str(self))


class ValueProxy:
    """兼容 get_values 单值的两种用法：
    1) x = get_values("x")
    2) [x] = get_values("x")
    """

    def __init__(self, value):
        self.value = value

    def __iter__(self):
        yield self.value

    def __getitem__(self, key):
        return [self.value][key]

    def __len__(self):
        return 1

    def __repr__(self):
        return repr(self.value)

    def __str__(self):
        return str(self.value)

    def __bool__(self):
        return bool(self.value)

    def __int__(self):
        return int(self.value)

    def __float__(self):
        return float(self.value)

    def __index__(self):
        return int(self.value)

    def __getattr__(self, item):
        return getattr(self.value, item)

    def _bin(self, other, op):
        other_val = other.value if isinstance(other, ValueProxy) else other
        return op(self.value, other_val)

    def __eq__(self, other):
        return self._bin(other, lambda a, b: a == b)

    def __lt__(self, other):
        return self._bin(other, lambda a, b: a < b)

    def __le__(self, other):
        return self._bin(other, lambda a, b: a <= b)

    def __gt__(self, other):
        return self._bin(other, lambda a, b: a > b)

    def __ge__(self, other):
        return self._bin(other, lambda a, b: a >= b)

    def __add__(self, other):
        return self._bin(other, lambda a, b: a + b)

    def __radd__(self, other):
        return self._bin(other, lambda a, b: b + a)

    def __sub__(self, other):
        return self._bin(other, lambda a, b: a - b)

    def __rsub__(self, other):
        return self._bin(other, lambda a, b: b - a)

    def __mul__(self, other):
        return self._bin(other, lambda a, b: a * b)

    def __rmul__(self, other):
        return self._bin(other, lambda a, b: b * a)

    def __truediv__(self, other):
        return self._bin(other, lambda a, b: a / b)

    def __floordiv__(self, other):
        return self._bin(other, lambda a, b: a // b)

    def __rfloordiv__(self, other):
        return self._bin(other, lambda a, b: b // a)

    def __mod__(self, other):
        return self._bin(other, lambda a, b: a % b)

    def __rmod__(self, other):
        return self._bin(other, lambda a, b: b % a)



def _get_well_order(load_name: str) -> List[str]:
    """根据 load_name 返回 well 顺序"""
    n = load_name.lower()
    if "384" in n:
        return _384_WELL_ORDER
    if "12_reservoir" in n or ("reservoir" in n and "15ml" in n):
        return _12_RESERVOIR_ORDER
    if "24" in n:
        if " tuberack" in n or "tuberack" in n:
            return _24_TUBE_ORDER
        # 24-well plate (corning_24_wellplate, nest 24 等)
        if "wellplate" in n or "well" in n:
            return _24_WELLPLATE_ORDER
        return _24_WELLPLATE_ORDER  # 默认 24 孔用 wellplate 顺序
    return _96_WELL_ORDER


def load_labware_def(protocol_dir: Path, load_name: str) -> Optional[Dict]:
    """从 protocol 的 labware 目录或 opentrons_shared_data 加载定义"""
    # 1. 优先从 protocol 自带的自定义 labware 目录匹配
    labware_dir = protocol_dir / "labware"
    if labware_dir.exists():
        base = load_name.replace(" ", "_").replace("-", "_")
        for f in labware_dir.glob("*.json"):
            if base in f.stem.lower() or f.stem.lower() in base:
                with open(f, "r", encoding="utf-8") as fp:
                    return json.load(fp)

    # 2. 从 opentrons_shared_data 读取标准 Opentrons 耗材定义
    try:
        import importlib.util
        spec = importlib.util.find_spec("opentrons_shared_data")
        if spec and spec.origin:
            shared_data = Path(spec.origin).parent / "data" / "labware" / "definitions" / "2"
            # 优先最新版本（版本号越大越新）
            defn_dir = shared_data / load_name
            if defn_dir.is_dir():
                versions = sorted(
                    (f for f in defn_dir.glob("*.json")),
                    key=lambda f: int(f.stem) if f.stem.isdigit() else 0,
                    reverse=True,
                )
                if versions:
                    with open(versions[0], "r", encoding="utf-8") as fp:
                        return json.load(fp)
    except Exception:
        pass

    return None


def _flatten_ordering(ordering: Any) -> List[str]:
    """从 labware definition 的 ordering 展平为 well 列表"""
    if isinstance(ordering, list):
        wells = []
        for row in ordering:
            if isinstance(row, list):
                wells.extend(row)
            else:
                wells.append(row)
        return wells
    return []


class MockWell:
    """模拟 Well，携带 parent labware 信息"""

    def __init__(self, name, labware: "MockLabware" = None):
        # name 可能是 _impl 对象（当协议用 super().__init__(well._impl) 时）
        if isinstance(name, str):
            self._name = name
        elif hasattr(name, '_name'):
            self._name = name._name
        elif hasattr(name, 'well_name'):
            self._name = name.well_name
        else:
            self._name = "A1"
        self._labware = labware
        self.liq_vol = 0.0  # 部分协议会设置此属性

    def __repr__(self):
        return f"MockWell({self._name})"

    def __str__(self):
        return self._name

    def _format_pose_z(self, position: str, z=0) -> str:
        z_val = float(z)
        if z_val == 0:
            return position
        z_text = str(int(z_val)) if z_val.is_integer() else str(z_val)
        return f"{position}({z_text})"

    def top(self, z=0):
        _pt = type("Point", (), {"x": 0, "y": 0, "z": 0})()
        loc = type("Loc", (), {
            "_well": self,
            "_name": self._name,
            "_labware": self._labware,
            "_pose_z": self._format_pose_z("top", z),
            "point": _pt,
            "move": lambda s, p=None: s,
            "top": lambda s, dz=0: self.top(dz),
            "bottom": lambda s, dz=0: self.bottom(dz),
        })()
        return loc

    def bottom(self, z=0):
        _pt = type("Point", (), {"x": 0, "y": 0, "z": 0})()
        loc = type("Loc", (), {
            "_well": self,
            "_name": self._name,
            "_labware": self._labware,
            "_pose_z": self._format_pose_z("bottom", z),
            "point": _pt,
            "move": lambda s, p=None: s,
            "top": lambda s, dz=0: self.top(dz),
            "bottom": lambda s, dz=0: self.bottom(dz),
        })()
        return loc

    def move(self, point=None):
        return self  # wick() 中 well.bottom().move(Point(...)) 用

    def _well_defn(self):
        # 当协议通过 WellH(Well) 子类调用 super().__init__(well._impl) 时，
        # _labware=None 但 self.well 指向原始 MockWell，从那里读取定义
        orig = getattr(self, 'well', None)
        if orig is not None and orig is not self and hasattr(orig, '_labware') and orig._labware:
            return orig._labware._defn.get("wells", {}).get(orig._name, {})
        if self._labware:
            return self._labware._defn.get("wells", {}).get(self._name, {})
        return {}

    @property
    def diameter(self):
        d = self._well_defn().get("diameter")
        return float(d) if d is not None else None

    @property
    def width(self):
        v = self._well_defn().get("xDimension")
        return float(v) if v is not None else 6.86

    @property
    def length(self):
        v = self._well_defn().get("yDimension")
        return float(v) if v is not None else 6.86

    def center(self):
        return self  # 供 move_to 等使用

    @property
    def geometry(self):
        d = self.depth
        w = self.width
        dia = self.diameter or w
        mv = self.max_volume
        return type("Geo", (), {"depth": d, "height": d, "width": w, "x": 0, "y": 0, "_depth": d, "_width": w, "_diameter": dia, "max_volume": mv})()

    @property
    def max_volume(self):
        # 从 labware 定义 JSON 的 wells[name].totalLiquidVolume 读取
        if self._labware:
            well_defn = self._labware._defn.get("wells", {}).get(self._name, {})
            if "totalLiquidVolume" in well_defn:
                return float(well_defn["totalLiquidVolume"])
        return 200.0

    @property
    def well_name(self):
        return self._name

    @property
    def parent(self):
        return self._labware

    @property
    def display_name(self):
        return self._name

    def load_liquid(self, *args, **kwargs):
        # 解析人写名称：兼容 well.load_liquid(liquid=Liquid) /
        # well.load_liquid(Liquid) / well.load_liquid(volume=200, liquid=Liquid)
        liquid_obj = kwargs.get("liquid")
        if liquid_obj is None:
            for arg in args:
                if hasattr(arg, "name") or isinstance(arg, str):
                    liquid_obj = arg
                    break
        if liquid_obj is None:
            return
        if hasattr(liquid_obj, "name"):
            name = getattr(liquid_obj, "name", None)
        elif isinstance(liquid_obj, str):
            name = liquid_obj
        else:
            name = None
        if not name or not self._labware:
            return
        recorder = getattr(self._labware, "_recorder", None)
        if recorder is None or not hasattr(recorder, "record_liquid_definition"):
            return
        try:
            recorder.record_liquid_definition(
                var_name=str(name),
                slot=self._labware._slot,
                well=self._name,
                liquid_name=str(name),
                source="load_liquid",
            )
        except Exception:
            # 不影响协议执行
            pass

    @property
    def point(self):
        return type("Point", (), {"x": 0, "y": 0, "z": 0})()

    @property
    def has_tip(self):
        return getattr(self, "_has_tip_val", True)

    @has_tip.setter
    def has_tip(self, val):
        self._has_tip_val = val

    @property
    def depth(self):
        v = self._well_defn().get("depth")
        return float(v) if v is not None else 10.0

    @property
    def _impl(self):
        return type("Impl", (), {"geometry": self.geometry})()


class MockLabware:
    """模拟 Labware"""

    def __init__(self, slot: int, label: str, load_name: str, well_order: List[str], defn: Dict = None):
        self._slot = int(slot) if isinstance(slot, str) else slot
        self._label = label
        self._load_name = load_name
        self._defn = defn or {}
        self._wells = [MockWell(w, self) for w in well_order]
        self._wells_by_name = {w._name: w for w in self._wells}

    @property
    def display_name(self) -> str:
        """优先用 Python label，其次用 labware 定义里的 displayName，最后用 load_name"""
        if self._label and self._label != self._load_name:
            return self._label
        dn = self._defn.get("metadata", {}).get("displayName")
        return dn if dn else self._label

    def wells_by_name(self):
        return self._wells_by_name

    def rows(self):
        # 按行分组，96 孔: A1-A12, B1-B12, ...
        rows = {}
        for w in self._wells:
            r = w._name[0]
            if r not in rows:
                rows[r] = []
            rows[r].append(w)
        return [rows[r] for r in "ABCDEFGHIJKLMNOP"[: len(rows)]]

    def columns(self, *args):
        # 按列分组，96 孔: A1-H1, A2-H2, ...
        cols = {}
        for w in self._wells:
            c = int(w._name[1:]) if len(w._name) > 1 and w._name[1:].isdigit() else 1
            if c not in cols:
                cols[c] = []
            cols[c].append(w)
        for c in cols:
            cols[c].sort(key=lambda w: w._name)
        all_cols = [cols[c] for c in sorted(cols.keys())]
        # 支持 columns(n) 形式——返回第 n 列（0-indexed）
        if args:
            idx = int(args[0])
            return all_cols[idx] if 0 <= idx < len(all_cols) else []
        return all_cols

    def well(self, key):
        """单个 well 访问，等同于 wells_by_name()[key]"""
        return self._wells_by_name.get(str(key))

    def wells(self, *keys):
        if keys:
            return [self._wells_by_name[k] for k in keys if k in self._wells_by_name]
        return self._wells

    def __getitem__(self, key):
        return self._wells_by_name.get(key)

    @property
    def parent(self):
        return None

    def columns_by_name(self):
        cols = {}
        for w in self._wells:
            c = w._name[1:] if len(w._name) > 1 and w._name[1:].isdigit() else "1"
            if c not in cols:
                cols[c] = []
            cols[c].append(w)
        return cols

    def rows_by_name(self):
        rows = {}
        for w in self._wells:
            r = w._name[0]
            if r not in rows:
                rows[r] = []
            rows[r].append(w)
        return rows

    @property
    def highest_z(self):
        if self._defn:
            z = self._defn.get("dimensions", {}).get("zDimension")
            if z is not None:
                return float(z)
        return 200.0

    def next_tip(self, channels=1, starting_tip=None):
        if channels == 8:
            for col in self.columns():
                if col and all(getattr(w, "has_tip", True) for w in col):
                    return col[0]
            return None
        started = starting_tip is None
        for well in self._wells:
            if not started:
                if well is starting_tip:
                    started = True
                else:
                    continue
            if getattr(well, "has_tip", True):
                return well
        return None

    def reset(self):
        for well in self._wells:
            well.has_tip = True


class MockPipette:
    """模拟 Pipette，记录所有液体操作"""

    def __init__(self, name: str, recorder: "ProtocolRecorder", tip_racks=None, mount="left"):
        self._name = name
        self._recorder = recorder
        self._current_volume = 0.0
        self._has_tip = False
        self._last_location = None
        self._flow_rate = _DEFAULT_FLOW_RATE
        self._tip_racks = tip_racks or []
        self._max_volume = 300.0 if "1000" in name else 20.0
        self._min_volume = 0.5
        self._mount = mount
        self._default_speed = 400.0
        self._starting_tip = None
        self._last_tip_picked_up_from = None

    @property
    def current_volume(self):
        return self._current_volume

    @property
    def flow_rate(self):
        fr = self._flow_rate
        return type("FlowRate", (), {"aspirate": fr, "dispense": fr, "blow_out": 100.0})()

    @flow_rate.setter
    def flow_rate(self, val):
        self._flow_rate = float(val) if hasattr(val, "__float__") else getattr(val, "aspirate", 7.6)

    @property
    def has_tip(self):
        return self._has_tip

    @property
    def max_volume(self):
        return self._max_volume

    @property
    def min_volume(self):
        return self._min_volume

    @property
    def tip_racks(self):
        return self._tip_racks

    @tip_racks.setter
    def tip_racks(self, val):
        self._tip_racks = val if isinstance(val, list) else [val]

    @property
    def mount(self):
        return self._mount

    @property
    def starting_tip(self):
        return self._starting_tip

    @starting_tip.setter
    def starting_tip(self, val):
        self._starting_tip = val

    @property
    def trash_container(self):
        return _make_trash_labware(Path(__file__).parent)

    @property
    def name(self):
        return self._name

    @property
    def default_speed(self):
        return self._default_speed

    @default_speed.setter
    def default_speed(self, val):
        self._default_speed = float(val)

    @property
    def hw_pipette(self):
        class HwPipette:
            def __init__(_s, model, name):
                _s.model, _s.name = model, name
            def __getitem__(_s, k):
                return getattr(_s, str(k), None)
        return HwPipette(self._name, self._name)

    def reset_tipracks(self, *args, **kwargs):
        for rack in self.tip_racks:
            if hasattr(rack, "reset"):
                rack.reset()

    def _get_next_tip(self):
        if self._starting_tip is not None and getattr(self._starting_tip, "has_tip", False):
            tip = self._starting_tip
            self._starting_tip = None
            return tip
        channels = self.channels
        for rack in self.tip_racks:
            if hasattr(rack, "next_tip"):
                tip = rack.next_tip(channels)
                if tip is not None:
                    return tip
        return None

    @property
    def well_bottom_clearance(self):
        return type("Clearance", (), {"aspirate": 1.0, "dispense": 1.0})()

    @well_bottom_clearance.setter
    def well_bottom_clearance(self, val):
        pass

    def pick_up_tip(self, well=None, **kwargs):
        if well is None:
            well = self._get_next_tip()
            if well is None:
                self.reset_tipracks()
                well = self._get_next_tip()
        self._has_tip = True
        self._current_volume = 0.0
        channels = self.channels
        if well is not None:
            lab = well._labware
            tip_well = well._name
            if hasattr(well, "has_tip"):
                well.has_tip = False
            # multi 模式：整列消耗 tip（与 Opentrons 真实行为一致）
            if channels == 8 and lab is not None and hasattr(lab, "columns_by_name"):
                col_key = tip_well[1:] if tip_well and tip_well[1:].isdigit() else ""
                col_wells = lab.columns_by_name().get(col_key, [])
                for w in col_wells:
                    if hasattr(w, "has_tip"):
                        w.has_tip = False
            self._recorder.record_pick_tip(tip_well, lab.display_name, lab._slot, channels=channels)
        else:
            fallback_slot = self._tip_racks[0]._slot if self._tip_racks else 1
            fallback_name = self._tip_racks[0].display_name if self._tip_racks else ""
            self._recorder.record_pick_tip("A1", fallback_name, fallback_slot, channels=channels)

    def drop_tip(self, well=None, **kwargs):
        self._has_tip = False
        self._current_volume = 0.0
        channels = self.channels
        if well is not None:
            lab = well._labware
            self._recorder.record_drop_tip(well._name, lab.display_name, lab._slot, channels=channels)
        else:
            self._recorder.record_drop_tip("A1", "Opentrons Fixed Trash", 12, channels=channels)

    def return_tip(self, well=None):
        if well is not None and hasattr(well, "has_tip"):
            well.has_tip = True
        self.drop_tip(well)

    def aspirate(self, volume, well=None, rate=1.0, **kwargs):
        self._current_volume += float(volume)
        target = well or self._last_location
        if target is not None and hasattr(target, "_name") and hasattr(target, "_labware"):
            lab = target._labware
            if lab is None:
                return
            pose_z = getattr(target, "_pose_z", None)
            self._last_location = target
            self._recorder.record_aspirate(
                float(volume), target._name, lab.display_name, lab._slot,
                pose_z=pose_z, channels=self.channels,
            )

    def dispense(self, volume=None, well=None, rate=1.0, **kwargs):
        req_vol = float(volume) if volume is not None else self._current_volume
        vol = min(req_vol, self._current_volume)
        self._current_volume -= vol
        target = well or self._last_location
        if target is not None and hasattr(target, "_name") and hasattr(target, "_labware"):
            lab = target._labware
            if lab is None:
                return
            pose_z = getattr(target, "_pose_z", None)
            self._last_location = target
            if vol == -1 or vol < 0:
                self._recorder.record_dispense(
                    -1, target._name, lab.display_name, lab._slot,
                    is_blowout=True, pose_z=pose_z, channels=self.channels,
                )
            else:
                self._recorder.record_dispense(
                    vol, target._name, lab.display_name, lab._slot,
                    pose_z=pose_z, channels=self.channels,
                )

    def blow_out(self, well=None, location=None):
        self._current_volume = 0
        target = well or location or self._last_location
        if target is not None:
            lab = getattr(target, "_labware", None)
            if lab is None:
                return
            pose_z = getattr(target, "_pose_z", None)
            self._last_location = target
            self._recorder.record_blow_out(
                target._name, lab.display_name, lab._slot,
                pose_z=pose_z, channels=self.channels,
            )
        else:
            self._recorder.record_blow_out(
                "A1", "Opentrons Fixed Trash", 12, channels=self.channels,
            )

    def air_gap(self, volume=0, *args, **kwargs):
        vol = float(volume)
        self._current_volume += vol
        if vol > 0:
            self._recorder.record_air_gap(vol, channels=self.channels)

    def touch_tip(self, well=None, **kwargs):
        target = well or self._last_location
        if target is not None:
            lab = getattr(target, "_labware", None)
            if lab is None:
                return
            pose_z = getattr(target, "_pose_z", None)
            self._last_location = target
            self._recorder.record_touch_tip(
                target._name, lab.display_name, lab._slot,
                pose_z=pose_z, channels=self.channels,
            )
        else:
            self._recorder.record_touch_tip(channels=self.channels)

    def transfer(self, volume, source, dest, **kwargs):
        """transfer(vol, src, dst) 或 transfer(vol, [s1,s2], [d1,d2])，vol 可为列表"""
        src_list = [source] if hasattr(source, "_name") else list(source)
        dst_list = [dest] if hasattr(dest, "_name") else list(dest)
        mix_before = kwargs.get("mix_before")
        mix_after = kwargs.get("mix_after")
        new_tip = kwargs.get("new_tip", "always")
        touch_tip = kwargs.get("touch_tip", False)
        blow_out = kwargs.get("blow_out", False)
        blowout_location = kwargs.get("blowout_location")

        auto_pick_once = new_tip == "once"
        auto_pick_always = new_tip == "always"

        # volume 可以是单值或与 src/dst 等长的列表
        n = max(len(src_list), len(dst_list))
        if isinstance(volume, (list, tuple)):
            vol_list = list(volume)
        else:
            vol_list = [volume] * n

        if auto_pick_once and not self.has_tip:
            self.pick_up_tip()

        for s, d, v in zip(src_list, dst_list, vol_list):
            if auto_pick_always and not self.has_tip:
                self.pick_up_tip()
            if mix_before:
                self.mix(mix_before[0], mix_before[1], s)
            self.aspirate(v, s)
            self.dispense(v, d)
            if mix_after:
                self.mix(mix_after[0], mix_after[1], d)
            if touch_tip:
                self.touch_tip(d)
            if blow_out:
                if blowout_location == "source well":
                    self.blow_out(s)
                elif blowout_location == "trash":
                    self.blow_out()
                else:
                    self.blow_out(d)
            if auto_pick_always and self.has_tip:
                self.drop_tip()

        if auto_pick_once and self.has_tip:
            self.drop_tip()

    def distribute(self, volume, source, dest, **kwargs):
        new_tip = kwargs.get("new_tip", "always")
        src = (list(source)[0] if list(source) else None) if not hasattr(source, "_name") else source
        dest_list = list(dest) if not (hasattr(dest, "_name") or hasattr(dest, "_labware")) else [dest]
        n = len(dest_list)
        if isinstance(volume, (list, tuple)):
            vol_list = [float(v) for v in volume]
        else:
            vol_list = [float(volume)] * n
        if new_tip == "always" and not self.has_tip:
            self.pick_up_tip()
        for d, v in zip(dest_list, vol_list):
            if src:
                self.aspirate(v, src)
            self.dispense(v, d)
        if new_tip == "always" and self.has_tip:
            self.drop_tip()

    def _normalize_wells(self, wells):
        if wells is None:
            return []
        if hasattr(wells, "_name"):
            return [wells]
        return list(wells)

    def mix(self, repetitions, volume, well=None, *args, **kwargs):
        target = well or self._last_location
        if target is not None:
            lab = getattr(target, "_labware", None)
            if lab is None:
                return
            pose_z = getattr(target, "_pose_z", None)
            self._last_location = target
            self._recorder.record_mix(
                repetitions, volume, target._name, lab.display_name, lab._slot,
                pose_z=pose_z, channels=self.channels,
            )

    def move_to(self, *args, **kwargs):
        if args:
            loc = args[0]
            if hasattr(loc, "_name") and hasattr(loc, "_labware"):
                self._last_location = loc
            elif hasattr(loc, "_well") and loc._well is not None:
                self._last_location = loc._well

    def home(self, **kwargs):
        pass

    def consolidate(self, volume, source, dest, **kwargs):
        """多源合并到单目标；默认 new_tip='once'，自动管理 pick_up/drop_tip"""
        new_tip = kwargs.get("new_tip", "once")
        blow_out = kwargs.get("blow_out", False)
        blowout_location = kwargs.get("blowout_location")
        mix_before = kwargs.get("mix_before")
        mix_after = kwargs.get("mix_after")

        sources = list(source) if hasattr(source, "__iter__") and not hasattr(source, "_name") else [source]
        if isinstance(volume, (list, tuple)):
            vol_list = [float(v) for v in volume]
        else:
            vol_list = [float(volume)] * len(sources)

        if new_tip in ("once", "always") and not self.has_tip:
            self.pick_up_tip()

        total = 0.0
        for s, v in zip(sources, vol_list):
            if mix_before:
                self.mix(mix_before[0], mix_before[1], s)
            self.aspirate(v, s)
            total += v

        self.dispense(total, dest)

        if mix_after:
            self.mix(mix_after[0], mix_after[1], dest)

        if blow_out:
            if blowout_location == "source well":
                self.blow_out(sources[-1] if sources else None)
            elif blowout_location == "trash":
                self.blow_out()
            else:
                self.blow_out(dest)

        if new_tip in ("once", "always") and self.has_tip:
            self.drop_tip()

    @property
    def type(self):
        return self._name

    @property
    def _implementation(self):
        mount = self._mount
        return type("PipImpl", (), {"get_mount": lambda s: mount})()

    @property
    def channels(self):
        return 8 if "multi" in self._name.lower() else 1


class ProtocolRecorder:
    """记录协议执行时的操作"""

    def __init__(self):
        self.actions: List[Dict] = []
        # P4 — mock 层显式液体记录：
        #   defined_liquids[name] = {"description": ..., "display_color": ...}
        #   liquid_locations[name] = {"slot": int, "well": str,
        #                             "liquid_name": name, "source": "load_liquid"}
        # 下游 detailed_action_json 生成器可消费这些字段填充
        # ``detailed_action_json/<name>.json`` 的 ``liquid_locations``。
        self.defined_liquids: Dict[str, Dict[str, Any]] = {}
        self.liquid_locations: Dict[str, Dict[str, Any]] = {}
        # P5 — mock 层捕获原 .py 协议顶层的 ``metadata = {...}`` 字典，
        # 字段保持 Opentrons 原命名（protocolName / author / source / apiLevel），
        # 不重命名为 snake_case。下游 ``change_to_transfer_group.py`` 的
        # ``load_protocol_metadata`` 优先从此处取值，正则解析 *.py 是兜底。
        self.protocol_metadata: Dict[str, Any] = {}

    def record_protocol_metadata(self, metadata: Any) -> None:
        """从 ``exec`` 命名空间抓到的 ``metadata`` dict 注入 recorder。"""
        if not isinstance(metadata, dict):
            return
        try:
            normalized = {str(k): _stringify_for_json(v) for k, v in metadata.items()}
        except Exception:
            return
        self.protocol_metadata = normalized

    def dump_detailed_action_json(self, out_path: Path) -> None:
        """把 mock 层捕获的 ``metadata`` / ``liquid_locations`` 写入
        ``detailed_action_json/<name>.json``。

        与旧 ``modified_code.py`` 注入方案兼容：若目标文件已存在，只 patch
        本次新增的字段（``metadata`` + 来自 mock 层 ``load_liquid`` 的
        ``liquid_locations`` 条目），保留已有 ``event_logs`` 等字段不动。
        """
        out_path = Path(out_path)
        existing: Dict[str, Any] = {}
        if out_path.exists():
            try:
                with out_path.open("r", encoding="utf-8") as fp:
                    loaded = json.load(fp)
                    if isinstance(loaded, dict):
                        existing = loaded
            except Exception:
                existing = {}

        existing.setdefault("event_logs", [])
        existing_liquid_locs = existing.get("liquid_locations") or {}
        if not isinstance(existing_liquid_locs, dict):
            existing_liquid_locs = {}

        for var_name, loc_info in self.liquid_locations.items():
            if not isinstance(loc_info, dict):
                continue
            merged = dict(existing_liquid_locs.get(var_name) or {})
            merged.update(loc_info)
            existing_liquid_locs[var_name] = merged
        existing["liquid_locations"] = existing_liquid_locs

        if self.protocol_metadata:
            existing["metadata"] = self.protocol_metadata

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fp:
            json.dump(existing, fp, indent=2, ensure_ascii=False, default=str)

    def record_define_liquid(self, name: str, description: Any = None, display_color: Any = None):
        """记录 ``ProtocolContext.define_liquid(name, description, display_color)`` 调用。"""
        if not name:
            return
        self.defined_liquids[str(name)] = {
            "description": description,
            "display_color": display_color,
        }

    def record_liquid_definition(
        self,
        var_name: str,
        slot: int,
        well: str,
        liquid_name: Optional[str] = None,
        source: str = "load_liquid",
    ):
        """记录孔位与液体名的绑定（来自 ``Well.load_liquid`` 调用）。"""
        if not var_name or not well or slot is None:
            return
        try:
            slot_int = int(slot)
        except (TypeError, ValueError):
            slot_int = 0
        self.liquid_locations[str(var_name)] = {
            "slot": slot_int,
            "well": str(well),
            "liquid_name": str(liquid_name or var_name),
            "source": str(source),
        }

    @staticmethod
    def _maybe_set_channels(act: Dict, channels: int) -> None:
        # 仅当 multi pipette（channels != 1）时才写入 channels 字段，
        # 保证单通道协议的 step JSON 与历史 baseline 字节级一致。
        if channels and int(channels) != 1:
            act["channels"] = int(channels)

    def record_pick_tip(self, well: str, tip_type: str, slot: int, channels: int = 1):
        act = {
            "action": "pick_tip",
            "tip_rack": {"well": well, "type": tip_type, "slot": slot}
        }
        self._maybe_set_channels(act, channels)
        self.actions.append(act)

    def record_aspirate(self, vol: float, well: str, labware: str, slot: int, pose_z: str = None, channels: int = 1):
        act = {
            "action": "aspirate",
            "vol": vol,
            "source": {"well": well, "labware": labware, "slot": slot},
            "flow_rate": _DEFAULT_FLOW_RATE
        }
        if pose_z:
            act["pose_z"] = pose_z
        self._maybe_set_channels(act, channels)
        self.actions.append(act)

    def record_dispense(self, vol: float, well: str, labware: str, slot: int, is_blowout=False, pose_z: str = None, channels: int = 1):
        act = {
            "action": "dispense",
            "vol": vol,
            "target": {"well": well, "labware": labware, "slot": slot},
            "flow_rate": _DEFAULT_FLOW_RATE
        }
        if pose_z:
            act["pose_z"] = pose_z
        self._maybe_set_channels(act, channels)
        self.actions.append(act)

    def record_delay(self, seconds: float = 0, minutes: float = 0):
        self.actions.append({
            "action": "delay",
            "minutes": int(minutes),
            "seconds": float(seconds)
        })

    def record_mix(self, reps: int, vol, well: str, labware: str, slot: int, pose_z: str = None, channels: int = 1):
        vol_val = float(vol[0]) if isinstance(vol, (list, tuple)) and vol else float(vol)
        act = {
            "action": "mix",
            "vol": vol_val,
            "position": {"well": well, "labware": labware, "slot": slot},
            "flow_rate": _DEFAULT_FLOW_RATE,
            "mix_time": int(reps)
        }
        if pose_z:
            act["pose_z"] = pose_z
        self._maybe_set_channels(act, channels)
        self.actions.append(act)

    def record_blow_out(self, well: str, labware: str, slot: int, pose_z: str = None, channels: int = 1):
        act = {
            "action": "blow_out",
            "at": {"well": well, "labware": labware, "slot": slot}
        }
        if pose_z:
            act["pose_z"] = pose_z
        self._maybe_set_channels(act, channels)
        self.actions.append(act)

    def record_touch_tip(self, well: str = None, labware: str = None, slot: int = None, pose_z: str = None, channels: int = 1):
        act = {"action": "touch_tip"}
        if well is not None and labware is not None and slot is not None:
            act["location"] = {"well": well, "labware": labware, "slot": slot}
        if pose_z:
            act["pose_z"] = pose_z
        self._maybe_set_channels(act, channels)
        self.actions.append(act)

    def record_air_gap(self, vol: float, channels: int = 1):
        act = {
            "action": "air_gap",
            "vol": vol
        }
        self._maybe_set_channels(act, channels)
        self.actions.append(act)

    def record_drop_tip(self, well: str, labware: str, slot: int, channels: int = 1):
        act = {
            "action": "drop_tip",
            "location": {"well": well, "labware": labware, "slot": slot}
        }
        self._maybe_set_channels(act, channels)
        self.actions.append(act)


def build_mock_ctx(protocol_dir: Path, fields: List[Dict], recorder: ProtocolRecorder):
    """构建 mock 的 ctx 和依赖，供协议 run(ctx) 使用"""
    # 解析 fields 默认值，按 name 索引
    field_by_name = {}
    for f in fields:
        default = f.get("default")
        if default is not None:
            field_by_name[f.get("name", "")] = default
        elif f.get("type") == "dropDown":
            opts = f.get("options", [])
            field_by_name[f.get("name", "")] = opts[0]["value"] if opts else "left"
        else:
            field_by_name[f.get("name", "")] = None

    # Some xGEN protocols ship with a broken default tip-reuse branch that
    # references undefined parked-tip variables. Force a safe executable
    # default for mock generation so steps reflect the actual liquid handling.
    if protocol_dir.name.startswith("sci-idt-xgen-") and field_by_name.get("TIPREUSE") == "YES":
        field_by_name["TIPREUSE"] = "NO"

    def get_values(*names):
        result = []
        for n in names:
            v = field_by_name.get(n)
            if v is None and n in ("mount", "p20_mount", "p300_mount", "p1000_mount", "mount_p20", "mount_m20"):
                v = "left"
            if isinstance(v, str) and v.isdigit():
                v = NumericString(v)
            result.append(v)
        return ValueProxy(result[0]) if len(result) == 1 else result

    loaded_labwares = {
        12: _make_trash_labware(protocol_dir)
    }
    loaded_modules = []

    def load_labware(load_name: str, location, label: str = None, **kwargs):
        if isinstance(location, (list, tuple)):
            slot = int(location[-1]) if location and str(location[-1]).isdigit() else (int(location[0]) if location and str(location[0]).isdigit() else 1)
        else:
            try:
                slot = int(location) if location is not None else 1
            except (TypeError, ValueError):
                slot = 1
        load_name_str = str(load_name) if load_name else "generic_96_wellplate"
        defn = load_labware_def(protocol_dir, load_name_str)
        if defn and "ordering" in defn:
            order = _flatten_ordering(defn["ordering"])
        else:
            order = _get_well_order(load_name_str)
        lab = MockLabware(slot, label or load_name, load_name, order, defn=defn)
        # 注入 recorder 引用，让 MockWell.load_liquid 能写入 liquid_locations
        lab._recorder = recorder
        loaded_labwares[slot] = lab
        return lab

    class MockDeck:
        def __init__(self):
            self._labwares = {}

        def __getitem__(self, slot):
            return self._labwares.get(slot)

        def __delitem__(self, slot):
            self._labwares.pop(slot, None)

        def __setitem__(self, slot, val):
            self._labwares[slot] = val

        def position_for(self, slot):
            slot_num = int(slot) if isinstance(slot, str) and slot.isdigit() else slot
            lab = self._labwares.get(slot_num)
            if lab is not None:
                return lab.wells()[0].top()
            return type("DeckPosition", (), {"move": lambda s, p=None: s})()

    deck = MockDeck()
    deck._labwares = loaded_labwares

    _loaded_instruments = {}

    class MockContext:
        max_speeds = {}

        def load_labware(self, load_name, location, label=None, **kwargs):
            return load_labware(load_name, location, label, **kwargs)

        @property
        def loaded_labwares(self):
            return loaded_labwares

        @property
        def deck(self):
            return deck

        def load_instrument(self, name, mount, tip_racks=None):
            m = mount or "left"
            pip = MockPipette(name, recorder, tip_racks, mount=m)
            _loaded_instruments[m] = pip
            return pip

        @property
        def loaded_instruments(self):
            return _loaded_instruments

        def load_module(self, module_name, location=None, configuration=None):
            mod_loc = int(location) if location is not None else 7

            class MockModule:
                def __init__(self, slot):
                    self._slot = slot
                    self.labware = None
                    self.lid_position = "closed"
                    self.status = "disengaged"

                def load_labware(self, load_name, location_or_label=None, label=None, **kw):
                    lab = label or kw.get("label")
                    if location_or_label is None:
                        slot = self._slot
                    elif isinstance(location_or_label, (int, float)) or (isinstance(location_or_label, str) and location_or_label.isdigit()):
                        slot = int(location_or_label)
                    else:
                        # 非数字字符串为 label，如 load_labware(labware_tempmod, 'Reagent Plate at 4 Degrees C')
                        lab = lab or location_or_label
                        slot = self._slot
                    self.labware = load_labware(load_name, slot, lab)
                    return self.labware

                def disengage(self):
                    self.status = "disengaged"

                def engage(self, h=None, height=None, height_from_base=None, **kwargs):
                    self.status = "engaged"

                def set_temperature(self, t=None, celsius=None, **kw):
                    pass

                def set_block_temperature(self, t=None, celsius=None, **kw):
                    pass

                def deactivate(self):
                    pass

                def deactivate_lid(self):
                    pass

                def deactivate_block(self):
                    pass

                def open_lid(self):
                    self.lid_position = "open"

                def close_lid(self):
                    self.lid_position = "closed"

                def close_labware_latch(self):
                    pass

                def open_labware_latch(self):
                    pass

                @property
                def labware_latch_status(self):
                    return "idle_closed"

                def set_lid_temperature(self, t=None, celsius=None, **kw):
                    pass

                def execute_profile(self, steps=None, repetitions=1, **kw):
                    pass

                def set_and_wait_for_temperature(self, celsius=None, **kw):
                    pass

                def set_and_wait_for_shake_speed(self, rpm=None, **kw):
                    pass

                def deactivate_shaker(self):
                    pass

                def deactivate_heater(self):
                    pass

                def wait_for_temperature(self, celsius=None, **kw):
                    pass

                def start_set_temperature(self, celsius=None, **kw):
                    pass

                def await_temperature(self, celsius=None, **kw):
                    pass

                def set_target_block_temperature(self, celsius=None, **kw):
                    pass

                def set_target_lid_temperature(self, celsius=None, **kw):
                    pass

                def set_target_temperature(self, celsius=None, **kw):
                    pass

                def wait_for_block_temperature(self, **kw):
                    pass

                def wait_for_lid_temperature(self, **kw):
                    pass

                @property
                def current_temperature(self):
                    return 25.0

                @property
                def target_temperature(self):
                    return 25.0

                @property
                def temperature(self):
                    return 25.0

                @property
                def current_speed(self):
                    return 0

                @property
                def target_speed(self):
                    return 0

                def __getattr__(self, name):
                    if name.startswith('_'):
                        raise AttributeError(name)
                    return lambda *a, **k: None

            mod = MockModule(mod_loc)
            loaded_modules.append(mod)
            return mod

        def delay(self, seconds=0, minutes=0, msg=None):
            recorder.record_delay(seconds=seconds, minutes=minutes)

        def pause(self, msg=None):
            pass

        def comment(self, msg=None):
            pass

        def set_rail_lights(self, on=None):
            pass

        def home(self):
            pass

        @property
        def rail_lights_on(self):
            return True

        @rail_lights_on.setter
        def rail_lights_on(self, val):
            pass

        @property
        def loaded_modules(self):
            return loaded_modules

        def commands(self):
            """部分协议调用 ctx.commands()"""
            return []

        @property
        def _implementation(self):
            """ctx._implementation._hw_manager 用法的兼容支持"""
            return self

        @property
        def _hw_manager(self):
            class _DummyInstrument:
                """吸收 _attached_instruments[mount].update_config_item / config.xxx"""
                class _Config:
                    pick_up_current = 0.1
                    pick_up_distance = 10.0
                    def __getattr__(self, name):
                        return 0.0
                config = _Config()
                def update_config_item(self, key, val):
                    pass
                def __getattr__(self, name):
                    return lambda *a, **k: None

            class _AttachedInstruments(dict):
                def __missing__(self, key):
                    return _DummyInstrument()

            hardware = type("Hardware", (), {
                "is_simulator": True,
                "set_lights": lambda s, rails=None, button=None, **kw: None,
                "_attached_instruments": _AttachedInstruments(),
            })()
            return type("HwManager", (), {"hardware": hardware})()

        def is_simulating(self):
            return True

        def define_liquid(self, name=None, description=None, display_color=None):
            # P4：将人写名称回记到 recorder，供下游 detailed_action_json /
            # well_to_varname 命名链消费。Liquid 对象保留 name/description/
            # display_color 三个属性，下游 well.load_liquid(liquid) 会再读取 name。
            if name:
                recorder.record_define_liquid(name, description, display_color)
            return type("Liquid", (), {
                "name": name,
                "description": description,
                "display_color": display_color,
            })()

        @property
        def fixed_trash(self):
            return loaded_labwares.get(12) or _make_trash_labware(protocol_dir)

    return MockContext(), get_values


def run_protocol_with_mock(protocol_path: Path, protocol_dir: Path) -> List[Dict]:
    """执行协议并返回记录的操作列表"""
    fields_path = protocol_dir / "fields.json"
    fields = []
    if fields_path.exists():
        with open(fields_path, "r", encoding="utf-8") as f:
            fields = json.load(f)

    recorder = ProtocolRecorder()
    ctx, get_values = build_mock_ctx(protocol_dir, fields, recorder)

    # 读取并执行协议
    with open(protocol_path, "r", encoding="utf-8") as f:
        code = f.read()

    # Mock opentrons 模块，避免 import 真实包
    class _Point:
        def __init__(self, x=0, y=0, z=0, **kwargs):
            self.x = kwargs.get("x", x)
            self.y = kwargs.get("y", y)
            self.z = kwargs.get("z", z)

    class _Mount:
        LEFT = "left"
        RIGHT = "right"

    class _Location:
        def __init__(self, point, labware=None):
            self.point = point
            self.labware = labware

    class _ProtocolContext:
        pass  # 占位，run(ctx) 时传入我们的 MockContext

    import sys
    from types import ModuleType
    _labware_mod = ModuleType("labware")
    _labware_mod.OutOfTipsError = type("OutOfTipsError", (Exception,), {})
    _labware_mod.Well = MockWell
    _labware_mod.Labware = MockLabware
    _protocol_api_mod = ModuleType("protocol_api")
    _protocol_api_mod.__path__ = []  # make it a package for submodule imports
    _protocol_api_mod.labware = _labware_mod
    _protocol_api_mod.ProtocolContext = _ProtocolContext
    _protocol_api_mod.InstrumentContext = type("InstrumentContext", (), {})
    opentrons_mod = ModuleType("opentrons")
    opentrons_mod.__path__ = []
    opentrons_mod.protocol_api = _protocol_api_mod
    opentrons_mod.protocols = ModuleType("protocols")
    opentrons_mod.types = ModuleType("types")
    opentrons_mod.types.Point = _Point
    opentrons_mod.types.Mount = _Mount
    opentrons_mod.types.Location = _Location
    _contexts_mod = ModuleType("contexts")
    _contexts_mod.InstrumentContext = type("InstrumentContext", (), {})
    _protocol_api_mod.contexts = _contexts_mod

    sys.modules["opentrons"] = opentrons_mod
    sys.modules["opentrons.protocol_api"] = _protocol_api_mod
    sys.modules["opentrons.protocol_api.labware"] = _labware_mod
    sys.modules["opentrons.protocol_api.contexts"] = _contexts_mod
    sys.modules["opentrons.types"] = opentrons_mod.types
    sys.modules["opentrons.protocols"] = opentrons_mod.protocols
    sys.modules["labware"] = _labware_mod  # fallback for "from labware import Well"

    globals_dict = {
        "get_values": get_values,
        "protocol_api": opentrons_mod.protocol_api,
        "Point": _Point,
        "math": __import__("math"),
        "ctx": ctx,
    }

    # 执行 run(ctx)，抑制协议内的 print
    import io
    exec_globals = {"__name__": "__main__", "get_values": get_values, "ctx": ctx, "math": __import__("math")}
    exec_globals["protocol_api"] = opentrons_mod.protocol_api
    exec_globals["Point"] = _Point
    old_stdout, sys.stdout = sys.stdout, io.StringIO()
    try:
        exec(code, exec_globals)
        # P5 — 抓取协议顶层的 metadata 字典（exec 后留在 exec_globals 命名空间里）
        recorder.record_protocol_metadata(exec_globals.get("metadata"))
        run_fn = exec_globals.get("run")
        if run_fn:
            run_fn(ctx)
    finally:
        sys.stdout = old_stdout

    # P5 — 把 metadata + mock 层 liquid_locations 落盘到
    # detailed_action_json/<name>.json，保留旧 modified_code.py 注入产物
    # 的 event_logs / 既有 liquid_locations 字段不动。
    try:
        detailed_dir = Path(__file__).parent / "detailed_action_json"
        detailed_path = detailed_dir / f"{protocol_dir.name}.json"
        recorder.dump_detailed_action_json(detailed_path)
    except Exception as e:
        # 落盘失败不影响 steps 输出，仅打印 warning
        print(f"  [warn] 写入 detailed_action_json 失败: {e}")

    return recorder.actions


def actions_to_phases(actions: List[Dict]) -> List[List[Dict]]:
    """将操作列表按 pick_tip 分组为 phases，并处理 blow_out"""
    phases = []
    current = []

    for a in actions:
        if a.get("action") == "pick_tip":
            if current:
                phases.append(current)
            current = [a]
        elif a.get("action") == "dispense" and a.get("vol") == -1:
            # blow_out 不输出，跳过
            continue
        else:
            current.append(a)

    if current:
        phases.append(current)

    return phases


def process_protocol_from_python(protocol_dir: Path, output_dir: Path, verbose: bool = True, prefer_static: bool = False) -> List[List[Dict]]:
    """
    从 protocol 目录的 Python 文件生成 steps。
    优先 mock 执行（完整循环、label、delay 等），失败时再尝试静态解析。
    """
    py_files = list(protocol_dir.glob("*.ot2.apiv2.py"))
    if not py_files:
        if verbose:
            print(f"  跳过 {protocol_dir.name}: 无 .ot2.apiv2.py 文件")
        return []

    phases: List[List[Dict]] = []
    name = protocol_dir.name
    if verbose:
        print(f"  转换 {name}...")

    # 优先 mock：完整执行循环、使用 labware label、捕获 delay 等，输出与参考一致
    try:
        protocol_path = py_files[0]
        actions = run_protocol_with_mock(protocol_path, protocol_dir)
        phases = actions_to_phases(actions)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / f"{name}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(phases, f, indent=4, ensure_ascii=False)
        n_actions = sum(len(p) for p in phases)
        if verbose:
            print(f"  mock 执行 steps: {out_path} ({len(phases)} phases, {n_actions} actions)")
        return phases
    except Exception as e:
        if verbose:
            print(f"  mock 失败: {e}，尝试静态解析...")

    # mock 失败时使用静态解析
    try:
        from protocol_static_parser import process_protocol_static
        result = process_protocol_static(protocol_dir, output_dir, verbose=verbose)
        if result:
            n_actions = sum(len(p) for p in result)
            if verbose:
                print(f"  静态解析 steps: {output_dir / f'{name}.json'} ({len(result)} phases, {n_actions} actions)")
            return result
    except Exception as e:
        if verbose:
            print(f"  静态解析失败: {e}")
    raise RuntimeError(f"协议 {name} mock 与静态解析均失败")


def batch_process_original(output_dir: Path = None, error_log: Path = None, prefer_static: bool = True):
    """批量处理 original 目录下所有协议。优先静态解析，减少 mock 执行失败。"""
    base = Path(__file__).parent
    original_dir = base / "original"
    steps_dir = output_dir or (base / "steps")
    error_log = error_log or (base / "log" / "error_converting.txt")

    if not original_dir.exists():
        print(f"original 目录不存在: {original_dir}")
        return

    error_log.parent.mkdir(parents=True, exist_ok=True)
    succeeded, failed = 0, 0
    dirs = [d for d in original_dir.iterdir() if d.is_dir()]
    for idx, proto_dir in enumerate(sorted(dirs), 1):
        py_files = list(proto_dir.glob("*.ot2.apiv2.py"))
        if not py_files:
            continue
        print(f"[{idx}/{len(dirs)}] {proto_dir.name}", end=" ", flush=True)
        try:
            process_protocol_from_python(proto_dir, steps_dir, verbose=False, prefer_static=prefer_static)
            succeeded += 1
            print("OK")
        except Exception as e:
            failed += 1
            print(f"FAIL: {e}")
            with open(error_log, "a", encoding="utf-8") as f:
                f.write(f"{proto_dir.name}: {e}\n")

    print(f"完成: 成功 {succeeded}, 失败 {failed} (见 {error_log})")


if __name__ == "__main__":
    base = Path(__file__).parent
    if len(sys.argv) > 1:
        name = sys.argv[1]
        proto_dir = base / "original" / name
        if proto_dir.exists():
            steps_dir = base / "steps"
            process_protocol_from_python(proto_dir, steps_dir)
        else:
            print(f"协议目录不存在: {proto_dir}")
    else:
        batch_process_original()
