"""校验引擎（规范 §8）。

承载语义：
- §8.1 类型判定优先级：显式 @type（declared）> 格式自身类型 > None；格式
  自身类型不可被注释推翻；类型一经判定 v1 内不可变。类型判定本身发生在
  适配器 load() 期（填充 ``ConfigItem.type``），本模块消费判定结果。
- §8.2 值合法性与硬/软校验：类型匹配与 declared range/enum 为硬校验
  （拦截，不落盘，红标）；inferred range/enum 为软校验（黄色警告，照常
  提交落盘并持续黄标，I-6）。range 单边界合法，仅校验存在的一侧。
- 数字输入边界（v0.4 决议，§10 U-6）：不钳制；中间态（``-``、空串等）
  非法不提交；int 不接受 ``3.0`` / 带引号形式；float 接受科学计数法输入，
  落盘字面量仍按 §7.2 风格重放（适配器职责）。
- 提交期校验只针对本次 EditOp 集合，未编辑条目不参与（§7.5、§8.2）。

核心不得解析具体格式（§2）：本模块只对**已类型化**的值与 UI 文本输入做
通用标量判定，不接触任何文件格式语法。

公共 API（session 层 / UI 层对接）：
- :func:`parse_text_input`：文本控件输入 → 类型化值（含中间态区分）；
- :func:`parse_bool_input`：点击类 bool 控件直通；
- :func:`gate_edit`：对已类型化新值做三级门控（ok / warn / block）；
- :func:`check_load_conflicts`：加载期现值 vs inferred 约束的参考实现。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from ..model import (
    CODE_W_INFER_CONFLICT,
    PROV_DECLARED,
    PROV_INFERRED,
    SEV_WARNING,
    ConfigDoc,
    ConfigItem,
    Diagnostic,
)

__all__ = [
    "ParseResult",
    "GateResult",
    "parse_text_input",
    "parse_bool_input",
    "gate_edit",
    "check_load_conflicts",
]

# ---------------------------------------------------------------------------
# 文本解析（§8.2 数字输入边界、§10 U-6）
# ---------------------------------------------------------------------------

# int：十进制整数，可带一元符号。严格 ASCII（``\d`` 会放行非 ASCII 数字），
# 不接受空白、下划线、小数点、指数、引号包裹等一切变体。
_INT_RE = re.compile(r"^[+-]?[0-9]+$")

# float：十进制小数或科学计数法（``1.5``、``2``、``1.``、``.5``、``1.5e3``、
# ``-2E-3``）。显式拒绝 inf / nan / 十六进制 / 下划线（Python 的 float()
# 会接受其中部分形式，故先过正则再转换）。
_FLOAT_RE = re.compile(
    r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$"
)

# 中间态：用户仍在键入、既非法也不值得红标骚扰的前缀（v0.4 决议：中间态
# 视为非法、不提交；UI 不显示红标）。裁量：对 int 与 float 统一采用同一
# 集合——``.``/``-.`` 对 int 永远不可能补全为合法值，但键入过程中短暂出现
# 属正常编辑路径，按中间态静默处理比红标更符合 U-6 意图。
_INTERMEDIATE = frozenset({"", "-", "+", ".", "-.", "+."})


@dataclass
class ParseResult:
    """文本输入解析结果。

    - ``ok=True``：``value`` 为类型化值，可直接送 :func:`gate_edit`；
    - ``ok=False, intermediate=True``：**中间态**（空串、仅 ``-``、仅 ``.``
      等），UI 不提交也不红标（§10 U-6）；``error`` 仍给出说明供调试；
    - ``ok=False, intermediate=False``：确定性非法输入，UI 红标并展示
      ``error``（面向用户的中文说明）。
    """

    ok: bool
    value: bool | int | float | str | None = None
    error: str | None = None
    intermediate: bool = False


def parse_text_input(item: ConfigItem, text: str) -> ParseResult:
    """把 UI 文本输入解析为 ``item.type`` 对应的类型化值（§8.2）。

    - ``int``：仅十进制整数文本（可带符号）；``3.0``、``1.5``、``"3"``、
      ``1e3``、``0x10``、``1_0`` 一律拒绝；
    - ``float``：小数与科学计数法（``1.5e3`` → 1500.0）；整数值文本接受
      （``2`` → 2.0，值为 float；落盘字面量的浮点化由 §7.2 风格重放负责）；
    - ``str``：任意文本接受（含空串，如 ``own_color: ""``）；
    - ``bool``：文本输入不适用（bool 走点击控件，用 :func:`parse_bool_input`），
      返回确定性错误；
    - ``type=None``（只读容器等）：返回确定性错误。

    中间态（``''``、``'-'``、``'.'`` 及其带符号组合）对 int/float 返回
    ``ok=False, intermediate=True``——不提交、不红标。
    """
    t = item.type
    if t == "str":
        return ParseResult(ok=True, value=text)
    if t == "bool":
        return ParseResult(
            ok=False,
            error="布尔条目请通过开关/复选控件修改，不支持文本输入。",
        )
    if t is None:
        return ParseResult(
            ok=False, error="该条目类型未知（只读），不接受输入。"
        )
    # t in ("int", "float")
    if text in _INTERMEDIATE:
        return ParseResult(
            ok=False,
            error="输入不完整。",
            intermediate=True,
        )
    if t == "int":
        if not _INT_RE.match(text):
            return ParseResult(
                ok=False,
                error=f"「{text}」不是合法的整数：只接受十进制整数（可带正负号），"
                "不接受小数、科学计数法、引号或十六进制等形式。",
            )
        return ParseResult(ok=True, value=int(text))
    # float
    if not _FLOAT_RE.match(text):
        return ParseResult(
            ok=False,
            error=f"「{text}」不是合法的数字：接受小数或科学计数法（如 1.5、1.5e3），"
            "不接受 inf/nan、下划线或十六进制等形式。",
        )
    return ParseResult(ok=True, value=float(text))


def parse_bool_input(item: ConfigItem, checked: bool) -> ParseResult:
    """bool 条目的点击类控件直通（§7.5 第 3 步：值变化即提交，无防抖）。

    非 bool 条目调用属程序错误，返回确定性错误结果（不抛异常，UI 层可
    统一按 ParseResult 处理）。
    """
    if item.type != "bool":
        return ParseResult(
            ok=False, error="该条目不是布尔类型，不能使用开关控件。"
        )
    return ParseResult(ok=True, value=bool(checked))


# ---------------------------------------------------------------------------
# 提交门控（§7.5 第 2 步、§8.2 硬/软校验表）
# ---------------------------------------------------------------------------


@dataclass
class GateResult:
    """门控三级判定结果（§8.2）。

    - ``level='ok'``：照常提交；
    - ``level='warn'``：inferred 约束违规——黄色警告，**照常提交落盘**并
      持续黄标（I-6 推测永不拦截）；``provenance='inferred'``；
    - ``level='block'``：类型错误或 declared 约束违规——暂态不进入提交，
      控件红标并展示 ``reason``；declared 违规的 ``reason`` 含 "declared"
      字样作为钩子，UI 层据此拼接 @ 标注原文展示（§6、§7.5）。
    - ``value``：规范化后的值（float 条目收到 int 输入时为该值的 float
      形式；其余与入参相同）。session 层生成 EditOp 时应使用此字段。
    """

    level: Literal["ok", "warn", "block"]
    reason: str | None = None
    provenance: str | None = None
    value: Any = None


def _range_violation(item: ConfigItem, value: Any) -> str | None:
    """range 单边界校验（§3.1、§8.2）：只校验存在的一侧。返回违规描述或 None。"""
    r = item.range
    if r is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None  # 非数值不参与 range（防御性；类型门控已先拦截错型）
    if r.min is not None and value < r.min:
        unit = f" {r.unit}" if r.unit else ""
        return f"值 {value}{unit} 低于允许的最小值 {r.min}{unit}"
    if r.max is not None and value > r.max:
        unit = f" {r.unit}" if r.unit else ""
        return f"值 {value}{unit} 高于允许的最大值 {r.max}{unit}"
    return None


def _enum_violation(item: ConfigItem, value: Any) -> str | None:
    """enum 校验：值须在候选内。返回违规描述或 None。"""
    if not item.enum_candidates:
        return None
    if any(c.value == value for c in item.enum_candidates):
        return None
    shown = "、".join(repr(c.value) for c in item.enum_candidates)
    return f"值 {value!r} 不在允许的候选内（{shown}）"


def _enum_provenance(item: ConfigItem) -> str | None:
    """候选集 provenance（§3.4：同一字段 declared 覆盖 inferred，模型层
    保证不并存；此处防御性按 declared 优先）。"""
    if not item.enum_candidates:
        return None
    if any(c.provenance == PROV_DECLARED for c in item.enum_candidates):
        return PROV_DECLARED
    if any(c.provenance == PROV_INFERRED for c in item.enum_candidates):
        return PROV_INFERRED
    return None


def gate_edit(item: ConfigItem, new_value: bool | int | float | str) -> GateResult:
    """对已类型化的新值做提交门控（§7.5 第 2 步、§8.2 校验表）。

    判定顺序（首个命中即返回）：

    1. ``readonly`` 条目 → BLOCK（readonly 天然不进提交流程，此为防御性
       兜底，§7.5）；
    2. **类型匹配（硬）**：int 条目只接受真 int（Python 的 bool 是 int
       子类，显式排除）；bool 只接受真 bool；float 接受 float 与 int
       输入（int 规范化为 float，落盘字面量由 §7.2 风格重放保证浮点化）；
       str 接受任意 str；``type=None`` 不可编辑 → BLOCK；
    3. **declared range/enum（硬）** → BLOCK，reason 含 "declared" 字样；
    4. **inferred range/enum（软）** → WARN，照常提交（I-6）；
    5. 无约束 → OK。

    ``dormant`` **不是**拦截条件（§7.5：休眠条目允许编辑并实时落盘，UI
    只持续黄标 U-8），本函数对 dormant 条目不做任何降级。
    """
    # 1. readonly（防御性；§7.5：readonly 条目天然不进入提交流程）
    if item.readonly:
        return GateResult(
            level="block", reason="该条目为只读，不可编辑。", value=new_value
        )

    t = item.type
    # 2. 类型匹配（硬，§8.2 表第一行）
    if t is None:
        return GateResult(
            level="block", reason="该条目类型未知（只读容器），不可编辑。",
            value=new_value,
        )
    if t == "bool":
        if not isinstance(new_value, bool):
            return GateResult(
                level="block",
                reason=f"类型不匹配：该条目是布尔型，不接受 {type(new_value).__name__} 值。",
                value=new_value,
            )
    elif t == "int":
        # bool 是 int 子类，必须显式排除（True 不是合法 int 输入）
        if isinstance(new_value, bool) or not isinstance(new_value, int):
            return GateResult(
                level="block",
                reason=f"类型不匹配：该条目是整数型，不接受 {type(new_value).__name__} 值"
                "（不接受小数或布尔）。",
                value=new_value,
            )
    elif t == "float":
        if isinstance(new_value, bool):
            return GateResult(
                level="block",
                reason="类型不匹配：该条目是浮点型，不接受布尔值。",
                value=new_value,
            )
        if isinstance(new_value, int):
            new_value = float(new_value)  # §8.2：float 条目接受整数值输入
        elif not isinstance(new_value, float):
            return GateResult(
                level="block",
                reason=f"类型不匹配：该条目是浮点型，不接受 {type(new_value).__name__} 值。",
                value=new_value,
            )
    elif t == "str":
        if not isinstance(new_value, str):
            return GateResult(
                level="block",
                reason=f"类型不匹配：该条目是字符串型，不接受 {type(new_value).__name__} 值。",
                value=new_value,
            )

    # 3/4. 约束校验：declared 硬（BLOCK）、inferred 软（WARN）；同一字段
    # declared 覆盖 inferred（§3.4，模型层已保证不并存，此处防御性分列）。
    range_v = _range_violation(item, new_value)
    enum_v = _enum_violation(item, new_value)
    range_prov = item.range.provenance if item.range is not None else None
    enum_prov = _enum_provenance(item)

    for violation, prov, kind in (
        (range_v, range_prov, "范围"),
        (enum_v, enum_prov, "枚举"),
    ):
        if violation is None:
            continue
        if prov == PROV_DECLARED:
            # reason 含 "declared" 钩子：UI 层据此拼接 @ 标注原文（§6、§7.5）
            return GateResult(
                level="block",
                reason=f"违反 declared（显式标注）{kind}约束：{violation}。",
                provenance=PROV_DECLARED,
                value=new_value,
            )
        if prov == PROV_INFERRED:
            return GateResult(
                level="warn",
                reason=f"违反 inferred（注释推测）{kind}约束：{violation}。"
                "推测不作铁律，仍会照常落盘。",
                provenance=PROV_INFERRED,
                value=new_value,
            )

    # 5. 无约束通过（dormant 不参与门控，§7.5）
    return GateResult(level="ok", value=new_value)


# ---------------------------------------------------------------------------
# 加载期现值冲突检查（§5.2 冲突正例的通用化、§3.7 W-INFER-CONFLICT）
# ---------------------------------------------------------------------------


def check_load_conflicts(doc: ConfigDoc) -> list[Diagnostic]:
    """加载期现值 vs **inferred** 约束的冲突检查（W-INFER-CONFLICT）。

    遍历可编辑条目（跳过 readonly 与无值条目），现值违反 inferred
    range/enum → ``warning`` 诊断（不阻塞进入编辑，U-10；只做黄标展示，
    提交期不得对未编辑条目重复告阻，§7.5）。

    与适配器产出的关系：**以适配器 load() 填充的 diagnostics 为准**——
    适配器（如 python 适配器对 THROW_IN_ADVANCE_MIN_M 反例）可能自带此
    检查；本函数是校验引擎侧的参考实现/兜底，供核心与测试对任意 doc
    独立使用，不去重、不合并。

    裁量：declared 约束的现值冲突不在本函数范围——W-INFER-CONFLICT 语义
    专指推测冲突（§3.7 码表）；declared 冲突属文件自身违约，留给适配器
    按格式语义报告。
    """
    out: list[Diagnostic] = []
    for item in doc.items:
        if item.readonly or item.value is None:
            continue
        problems: list[str] = []
        if (
            item.range is not None
            and item.range.provenance == PROV_INFERRED
        ):
            v = _range_violation(item, item.value)
            if v:
                problems.append(f"范围推测：{v}")
        if _enum_provenance(item) == PROV_INFERRED:
            v = _enum_violation(item, item.value)
            if v:
                problems.append(f"枚举推测：{v}")
        if problems:
            out.append(
                Diagnostic(
                    severity=SEV_WARNING,
                    code=CODE_W_INFER_CONFLICT,
                    message=f"条目 {item.path} 当前值与注释推测的约束冲突（"
                    + "；".join(problems)
                    + "）。仅作黄标提示，不影响编辑与提交。",
                    path=item.path,
                )
            )
    return out
