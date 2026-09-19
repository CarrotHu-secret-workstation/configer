"""YAML 适配器私有子模块：纯函数集合（无 I/O、不触碰 ConfigDoc）。

内容（规范条款）：

- YAML 1.2 core schema 标量解析（§4.4 Y-2：类型按 1.2 core 语义；
  **不采信 ruamel 的 1.1 系隐式解析**——``off``/``1_000``/timestamp 等歧义
  一律以 1.2 core 为准）；
- §7.2 字面量风格重放（float_form / bool_case / quote / int 十进制，
  含裸写安全回退与 str 转义）；
- §5.3 YC-3 枚举推测（``|`` 分段 + 五步清洗 + 三道丢弃闸门）；
- §5.1 C-3 警告标记（≥2 个连续 ``!``，前后非字母数字）。
"""

from __future__ import annotations

import re

from ..model import (
    PROV_INFERRED,
    EnumCandidate,
    LiteralStyle,
    is_exotic_numeric,
)

__all__ = [
    "resolve_core",
    "is_special_float_token",
    "render_literal",
    "bare_str_legal",
    "infer_enum_candidates",
    "has_warning_marker",
]

# ---------------------------------------------------------------------------
# YAML 1.2 core schema（spec 章节 10.3.2.1 的正则，锚定整 token）
# ---------------------------------------------------------------------------

_NULL_RE = re.compile(r"^(?:~|[nN]ull|NULL|)$")
_BOOL_RE = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
_INT_DEC_RE = re.compile(r"^[-+]?[0-9]+$")
_INT_OCT_RE = re.compile(r"^0o[0-7]+$")
_INT_HEX_RE = re.compile(r"^0x[0-9a-fA-F]+$")
_FLOAT_RE = re.compile(
    r"^[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?$"
)
_INF_RE = re.compile(r"^[-+]?\.(?:inf|Inf|INF)$")
_NAN_RE = re.compile(r"^\.(?:nan|NaN|NAN)$")


def resolve_core(token: str) -> tuple[str, object]:
    """按 YAML 1.2 core schema 解析 plain scalar token。

    返回 ``(类型名, 值)``，类型名 ∈ ``'null'|'bool'|'int'|'float'|'str'``。
    plain 标量在 core schema 下永不解析失败（兜底 str）；``'null'`` 视为
    v1 不可编辑类型（§3.1 type 仅四种标量）。
    """
    if _NULL_RE.match(token):
        return ("null", None)
    if _BOOL_RE.match(token):
        return ("bool", token.lower() == "true")
    if _INT_DEC_RE.match(token):
        return ("int", int(token, 10))
    if _INT_OCT_RE.match(token):
        return ("int", int(token[2:], 8))
    if _INT_HEX_RE.match(token):
        return ("int", int(token[2:], 16))
    if _NAN_RE.match(token):
        return ("float", float("nan"))
    if _INF_RE.match(token):
        return ("float", float("-inf") if token.startswith("-") else float("inf"))
    if _FLOAT_RE.match(token):
        return ("float", float(token))
    return ("str", token)


def is_special_float_token(token: str) -> bool:
    """``.nan`` / ``±.inf`` 等非普通浮点 token（§4.4 Y-9 → 只读）。"""
    return bool(_NAN_RE.match(token) or _INF_RE.match(token))


# ---------------------------------------------------------------------------
# §7.2 字面量风格重放
# ---------------------------------------------------------------------------

# 裸写（plain）安全回退判定用的前导指示符集合（YAML 1.2 §7.3.3 保守超集：
# 以这些字符开头的 str 新值一律回退双引号，语义不变、确定性优先）。
_BARE_LEADING_INDICATORS = set("-?:,[]{}#&*!|>%@`'\"\\\t ")


def bare_str_legal(value: str) -> bool:
    """str 新值能否按 plain 裸写而仍解析为同一 str（§7.2 裸字符串安全回退）。

    不合法情形（任一）：空串；首尾空白；含控制字符/换行；含 ``: `` 或以
    ``:`` 结尾；含 `` #``（注释起点）；以指示符开头；按 1.2 core 解析为
    非 str（``true``/``123``/``1.5``/``null``/``~`` 等）。
    """
    if value == "":
        return False
    if value != value.strip():
        return False
    for ch in value:
        o = ord(ch)
        if o < 0x20 or o == 0x7F:
            return False
    if ": " in value or value.endswith(":"):
        return False
    if " #" in value:
        return False
    if value[0] in _BARE_LEADING_INDICATORS:
        return False
    kind, _ = resolve_core(value)
    return kind == "str"


def yaml_quote_double(value: str) -> str:
    """双引号风格转义（§7.2 str 转义重放：``"`` → ``\\"`` 等 YAML 标准转义）。"""
    out = ['"']
    for ch in value:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif o < 0x20 or o == 0x7F:
            out.append("\\x%02x" % o)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def yaml_quote_single(value: str) -> str:
    """单引号风格转义（YAML 单引号内唯一转义：``'`` → ``''``）。"""
    return "'" + value.replace("'", "''") + "'"


def _render_str(value: str, quote: str | None) -> str:
    if quote == "single":
        # 单引号无法表达换行/控制字符 → 回退双引号（登记裁量）
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            return yaml_quote_double(value)
        return yaml_quote_single(value)
    if quote == "double":
        return yaml_quote_double(value)
    # quote == 'none'（裸写）：不合法 → 回退双引号（§7.2）
    if quote == "none" or quote is None:
        if bare_str_legal(value):
            return value
        return yaml_quote_double(value)
    return yaml_quote_double(value)


def _shortest_float(f: float) -> str:
    """最短表示：Python repr 即最短 round-trip 十进制（'0.5'、'2.0'、'1920.5'）。"""
    return repr(f)


def _render_float(form, f: float) -> str:
    if form == "trailing_dot":
        # 整数值 → `N.`；非整数 → 最短表示（§7.2 T3：100.→1920 得 `1920.`；→0.5 得 `0.5`）
        if f.is_integer():
            return "%d." % int(f)
        return _shortest_float(f)
    if isinstance(form, tuple) and len(form) == 2 and form[0] == "fixed":
        # 恰好 p 位小数（0.90→1.05 得 `1.05`；→1.2 得 `1.20`）
        return "%.*f" % (int(form[1]), f)
    # plain：最短表示；float 条目收整数值必落 float 字面量（repr(2.0)=='2.0'）
    return _shortest_float(f)


def render_literal(item_type: str | None, style: LiteralStyle, new_value) -> str:
    """按条目类型与 literal_style 生成新字面量 token（§7.2 规则表全部行）。

    值类型与条目类型不符 → ValueError（提交门控在核心 §8.2，此处防御）。
    """
    if item_type == "bool":
        if not isinstance(new_value, bool):
            raise ValueError(f"bool 条目只接受真布尔，收到 {new_value!r}")
        if style.bool_case == "capital":
            return "True" if new_value else "False"
        return "true" if new_value else "false"
    if item_type == "int":
        if isinstance(new_value, bool) or not isinstance(new_value, int):
            raise ValueError(f"int 条目只接受整数，收到 {new_value!r}")
        return str(new_value)  # v1 恒十进制（§3.2 int_form='decimal'）
    if item_type == "float":
        if isinstance(new_value, bool) or not isinstance(new_value, (int, float)):
            raise ValueError(f"float 条目只接受数值，收到 {new_value!r}")
        return _render_float(style.float_form, float(new_value))
    if item_type == "str":
        if not isinstance(new_value, str):
            raise ValueError(f"str 条目只接受文本，收到 {new_value!r}")
        return _render_str(new_value, style.quote)
    raise ValueError(f"类型 {item_type!r} 的条目不可编辑（只读），无法重放字面量")


# ---------------------------------------------------------------------------
# §5.3 YC-3 枚举推测
# ---------------------------------------------------------------------------

# CJK 判定（保守超集）：U+2E80 起覆盖 CJK 部首/汉字/假名/谚文/全角标点，
# 另有常见中文标点 U+3000-U+303F 已含；`·`(U+00B7)、`—`(U+2014) 等西文
# 标点不算 CJK（`——` 截断在 (i) 步已处理）。
def _has_cjk(s: str) -> bool:
    return any(ord(ch) >= 0x2E80 for ch in s)


def _clean_enum_segment(seg: str) -> tuple[str, str | None]:
    """YC-3 第 2 步五段清洗，返回 (候选文本, label|None)。"""
    s = seg
    # (i) 含 `——` 截去其后备注
    if "——" in s:
        s = s.split("——", 1)[0]
    # (ii) 紧邻左侧无空白的 `=`：token=候选，右侧为 label
    eq = -1
    for i, ch in enumerate(s):
        if ch == "=" and i > 0 and not s[i - 1].isspace():
            eq = i
            break
    if eq > 0:
        j = eq
        while j > 0 and not s[j - 1].isspace():
            j -= 1
        return s[j:eq], (s[eq + 1 :].strip() or None)
    # (iii) 剥离尾部成对 (…) 括注
    s = s.strip()
    if s.endswith(")"):
        depth = 0
        for i in range(len(s) - 1, -1, -1):
            if s[i] == ")":
                depth += 1
            elif s[i] == "(":
                depth -= 1
            if depth == 0:
                s = s[:i]
                break
    # (iv) 仍含 :/： 取最后一个冒号之后的部分
    idx = max(s.rfind(":"), s.rfind("："))
    if idx >= 0:
        s = s[idx + 1 :]
    # (v) trim
    return s.strip(), None


def infer_enum_candidates(
    comment: str, item_type: str | None
) -> tuple[list[EnumCandidate], list[tuple[str, str]]]:
    """YC-3 枚举推测。返回 (候选列表, 丢弃明细 [(段原文, 原因)])。

    触发：说明含 ≥2 个 ``|`` 分段。候选按 1.2 core 解析；三道丢弃闸门
    （解析失败 / 含空白或 CJK / 类型与条目不符）逐段给丢弃明细（调用方转
    I-ENUM-DROPPED）。剩余 ≥2 才产出候选（provenance='inferred'）。
    """
    segments = comment.split("|")
    if len(segments) < 2:
        return [], []
    candidates: list[EnumCandidate] = []
    dropped: list[tuple[str, str]] = []
    for seg in segments:
        cand, label = _clean_enum_segment(seg)
        kind, val = resolve_core(cand)
        if cand == "" or kind == "null":
            dropped.append((seg.strip(), "无法按 YAML 1.2 core 解析为标量"))
            continue
        if any(ch.isspace() for ch in cand) or _has_cjk(cand):
            dropped.append((seg.strip(), "候选含空白或 CJK 字符"))
            continue
        if item_type is None or kind != item_type:
            dropped.append(
                (
                    seg.strip(),
                    f"候选类型 {kind} 与条目类型 {item_type or '（只读/未知）'} 不符",
                )
            )
            continue
        candidates.append(
            EnumCandidate(value=val, label=label, provenance=PROV_INFERRED)
        )
    if len(candidates) < 2:
        return [], dropped
    return candidates, dropped


# ---------------------------------------------------------------------------
# §5.1 C-3 警告标记
# ---------------------------------------------------------------------------

_WARNING_RE = re.compile(r"(?<![0-9A-Za-z])!{2,}(?![0-9A-Za-z])")


def has_warning_marker(text: str) -> bool:
    """注释中出现 ≥2 个连续 ``!``（前后不是字母或数字）→ True（C-3）。"""
    return bool(_WARNING_RE.search(text))
