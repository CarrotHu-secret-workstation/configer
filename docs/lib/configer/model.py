"""configer 核心语义模型（规范 docs/spec.md v0.4 §3，normative）。

本模块只定义数据结构与纯函数 helper，不做任何格式解析、不做 I/O——
「核心不得解析任何具体格式」（§2 层间职责边界 2）。

稳定接口（§2）：ConfigDoc / ConfigItem / Diagnostic / EditOp 及其字段语义。
变更本模块公共语义必须升规范版本号。

适配器工程师对接要点：
- load() 产出 ConfigDoc（含 items/groups/diagnostics/original_bytes/content_hash）；
- save() 消费 EditOp 列表；
- raw_literal + literal_style 是保真写回的锚点（§3.2、§7.2）；
- locator 为适配器私有不透明字段，核心与 UI 不得解释（§3.1）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

__all__ = [
    # provenance（§3.1、§3.4、§0.3 术语表）
    "PROV_DECLARED",
    "PROV_INFERRED",
    "PROV_COMMENT",
    "PROV_NONE",
    # readonly_reason（§3.5、§4.4 Y-9）
    "READONLY_NON_LITERAL",
    "READONLY_CONTAINER",
    "READONLY_ANCHOR",
    "READONLY_NON_PLAIN_SCALAR",
    # 诊断 severity（§3.7）
    "SEV_ERROR",
    "SEV_WARNING",
    "SEV_INFO",
    # 诊断码（§3.7 v1 诊断码表）
    "CODE_E_FORMAT",
    "CODE_E_PARSE",
    "CODE_E_PARSE_MULTIDOC",
    "CODE_E_ENCODING",
    "CODE_E_DUP_KEY",
    "CODE_E_DUP_ASSIGN",
    "CODE_W_INFER_CONFLICT",
    "CODE_W_EOL_MIXED",
    "CODE_I_ENUM_DROPPED",
    "CODE_I_SEQ_READONLY",
    "CODE_I_ANCHOR_READONLY",
    # 数据结构
    "FloatForm",
    "LiteralStyle",
    "classify_float_form",
    "is_exotic_numeric",
    "Section",
    "EnumCandidate",
    "RangeConstraint",
    "ConfigItem",
    "GroupInfo",
    "Diagnostic",
    "ConfigDoc",
    "EditOp",
    "compute_hash",
]

# ---------------------------------------------------------------------------
# provenance 常量（§0.3、§3.4）：declared（显式 @ 标注）/ inferred（注释推测）
# / comment（注释原文）/ none。
# ---------------------------------------------------------------------------
PROV_DECLARED = "declared"
PROV_INFERRED = "inferred"
PROV_COMMENT = "comment"
PROV_NONE = "none"

# ---------------------------------------------------------------------------
# readonly_reason 枚举（§3.5）。适配器可扩展，扩展值必须在适配器文档中登记；
# 'non_plain_scalar' 即 YAML 适配器登记的扩展值（§4.4 Y-9）。
# ---------------------------------------------------------------------------
READONLY_NON_LITERAL = "non_literal"          # 值是表达式，如 math.radians(15)（§4.3 R-4）
READONLY_CONTAINER = "container"              # 值为元组/序列/表（§4.3 R-5、§4.4 Y-6）
READONLY_ANCHOR = "anchor"                    # yaml 锚点/别名子树（§4.4 Y-6）
READONLY_NON_PLAIN_SCALAR = "non_plain_scalar"  # yaml 块标量与 .nan/.inf 等（§4.4 Y-9）

# ---------------------------------------------------------------------------
# Diagnostic severity（§3.7）
# ---------------------------------------------------------------------------
SEV_ERROR = "error"
SEV_WARNING = "warning"
SEV_INFO = "info"

# ---------------------------------------------------------------------------
# v1 诊断码表（§3.7）。severity 归属见规范表格：E-* error、W-* warning、I-* info。
# ---------------------------------------------------------------------------
CODE_E_FORMAT = "E-FORMAT"                  # 无法识别文件格式
CODE_E_PARSE = "E-PARSE"                    # 识别成功但解析失败
CODE_E_PARSE_MULTIDOC = "E-PARSE-MULTIDOC"  # yaml 多文档
CODE_E_ENCODING = "E-ENCODING"              # 非 UTF-8 编码
CODE_E_DUP_KEY = "E-DUP-KEY"                # yaml 重复键
CODE_E_DUP_ASSIGN = "E-DUP-ASSIGN"          # python 同名重复赋值
CODE_W_INFER_CONFLICT = "W-INFER-CONFLICT"  # 当前值违反 inferred 约束（加载期）
CODE_W_EOL_MIXED = "W-EOL-MIXED"            # 行尾风格混杂
CODE_I_ENUM_DROPPED = "I-ENUM-DROPPED"      # 枚举候选段无法解析被丢弃
CODE_I_SEQ_READONLY = "I-SEQ-READONLY"      # yaml 序列值只读
CODE_I_ANCHOR_READONLY = "I-ANCHOR-READONLY"  # yaml 锚点子树只读


# ---------------------------------------------------------------------------
# §3.2 字面量风格
# ---------------------------------------------------------------------------

# float_form 的无歧义表示：'plain' | 'trailing_dot' | ('fixed', p)，p 为固定
# 小数位数（正整数）。例：'0.15'→'plain'；'100.'→'trailing_dot'；
# '0.90'→('fixed', 2)；'1.0'→('fixed', 1)。
FloatForm = Literal["plain", "trailing_dot"] | tuple[Literal["fixed"], int]


def classify_float_form(literal: str) -> FloatForm:
    """§3.2 float_form 机械判定规则（保证 M1 测试可复现）。

    规则（对既有十进制 float 字面量逐条执行）：
    - 字面量以 ``.`` 结尾 → ``'trailing_dot'``（如 ``100.``）；
    - 小数部分末位为 ``0`` → ``('fixed', p)``，p = 小数位数
      （如 ``0.90``→2、``1.0``→1、``1.60``→2、``10000.0``→1）；
    - 其余 → ``'plain'``（如 ``0.15``、``-6.5``）。

    入参应为纯十进制 float 字面量文本（可带一元正负号）。含下划线/指数/
    十六进制的异体字面量在 v1 必须只读（见 :func:`is_exotic_numeric`），
    不依赖本函数。
    """
    if literal.endswith("."):
        return "trailing_dot"
    if "." in literal:
        frac = literal.split(".", 1)[1]
        if frac and frac[-1] == "0":
            return ("fixed", len(frac))
    return "plain"


def is_exotic_numeric(literal: str) -> bool:
    """§3.2：数值字面量含下划线（``1_000``）、指数（``1e3``）、十六进制
    （``0x10``）之一者，v1 **必须**按只读处理（此规则保证风格重放的确定性）。

    - ``'1_000'`` / ``'1e3'`` / ``'0x10'`` → True
    - ``'100.'`` / ``'-6.5'`` / ``'0.90'`` / ``'10000'`` → False

    裁量说明：仅对**数值**字面量调用（str 字面量内容含 'e' 等与本函数无关）；
    规范只点名十六进制，本实现把 ``0b`` / ``0o`` 前缀一并保守视为异体
    （同为非十进制基数，风格重放同样不确定），属登记在案的从严扩展。
    """
    core = literal.strip().lstrip("+-")
    if "_" in core:
        return True
    prefix = core[:2].lower()
    if prefix in ("0x", "0b", "0o"):
        return True
    # 十进制字面量中的 e/E 只可能是指数记法
    return "e" in core.lower()


@dataclass
class LiteralStyle:
    """字面量风格的结构化记录（§3.2），落盘时按 §7.2 重放。

    v1 统一字段（适配器可私有扩展，扩展须登记）：

    - ``float_form``：``'plain' | 'trailing_dot' | ('fixed', p)``，仅 float 条目；
    - ``bool_case``：``'lower'``（``true``）/ ``'capital'``（``True``），yaml bool（§4.4 Y-8）；
    - ``quote``：``'none' | 'single' | 'double'``，str 条目（python / yaml）；
    - ``int_form``：v1 恒 ``'decimal'``（int 只有十进制一种形式）。

    不适用的字段保持 None（如 int 条目的 float_form / bool_case / quote）。
    """

    float_form: FloatForm | None = None
    bool_case: Literal["lower", "capital"] | None = None
    quote: Literal["none", "single", "double"] | None = None
    int_form: Literal["decimal"] = "decimal"


# ---------------------------------------------------------------------------
# §3.1 ConfigItem 及其构件
# ---------------------------------------------------------------------------

@dataclass
class Section:
    """说明段（§3.1、§3.3）。

    python：作用/影响/建议 分段 + 前导段（label=None，P-2）；
    yaml：行尾注释全文为单段（label=None，YC-1）。
    """

    label: str | None
    text: str


@dataclass
class EnumCandidate:
    """枚举候选（§3.1、§3.4）。

    - ``value``：按目标格式类型语义解析后的候选值（§5.3 第 3 步）；
    - ``label``：展示用说明文本（如 ``off=关 (默认)`` 的 ``关 (默认)``），可无；
    - ``provenance``：``'declared'``（@enum，§6）或 ``'inferred'``（注释推测，§5.3）。
      declared 与 inferred 不得并存于同一条目（§3.4）。
    """

    value: Any
    label: str | None = None
    provenance: str = PROV_NONE


@dataclass
class RangeConstraint:
    """范围约束（§3.1、§3.4、§8.2）。每条目 0 或 1 个。

    min/max 均可为 None（**单边界约束合法**，空侧不设限；@min/@max 为独立
    可选键，§6）；unit 仅展示（如 ``米``、``秒``、``米/秒``），不参与校验；
    provenance ∈ {declared, inferred}，决定校验级别（§8.2）：declared 违规
    拦截不落盘，inferred 违规仅黄色警告。
    """

    min: float | None = None
    max: float | None = None
    unit: str | None = None
    provenance: str = PROV_NONE


@dataclass
class ConfigItem:
    """语义模型基本单元：一个可配置项（§3.1，字段语义 normative）。

    必填：``path``、``group``；其余字段有默认值，适配器按规范填充。

    - ``path``：唯一路径（I-1）。yaml 点分嵌套路径（包装链剥离后起算，Y-4），
      如 ``rl_brain.ball_vel.alpha``；python 为模块级变量名。
    - ``group``：分组名。yaml 分组键 / python ``# ====`` 节标题；散落条目归入
      伪分组 ``（根级）``（yaml）或 ``（未分组）``（python）（§3.6）。
    - ``subgroup``：python ``# --- 标题 ---``（P-5）；yaml v1 恒 None。
    - ``type``：``'bool'|'int'|'float'|'str'|None``；None 表示只读且类型未知
      （如 tuple 表）。判定优先级见 §8.1。
    - ``value``：当前值，**必须**与 ``raw_literal`` 解析结果一致（I-2）。
    - ``raw_literal``：原始字面量 token 的逐字符文本（如 ``100.``、``"adult_size"``）。
    - ``literal_style``：风格记录（§3.2），§7.2 重放依据。
    - ``description``：有序说明段列表；``description_provenance`` ∈
      {'comment', 'declared'}（§3.1）。说明只展示、不校验（§3.3）。
    - ``enum_candidates`` / ``range``：约束，各带 provenance（§3.4）。
    - ``readonly`` / ``readonly_reason``：只读标志与原因（§3.5 枚举常量
      READONLY_*；适配器扩展值须登记）。
    - ``dormant`` / ``warning``：休眠与警告标记（§3.5）；v0.4 起 dormant
      允许编辑并实时落盘，UI 持续黄标（U-8）。
    - ``warning_reason_text`` / ``dormant_reason_text``：标记注释**原文**，
      UI 悬停/详情面板必须展示的依据（§3.5）。
    - ``locator``：写回定位信息，**适配器私有**（行号、CST 节点引用等）。
      核心与 UI 不得解释、复制或持久化；跨进程边界允许置空（此时该 doc
      不可再提交，必须重新 load）（§3.1）。
    """

    path: str
    group: str
    subgroup: str | None = None
    type: Literal["bool", "int", "float", "str"] | None = None
    value: bool | int | float | str | None = None
    raw_literal: str = ""
    literal_style: LiteralStyle = field(default_factory=LiteralStyle)
    description: list[Section] = field(default_factory=list)
    description_provenance: Literal["declared", "inferred", "comment", "none"] = PROV_NONE
    enum_candidates: list[EnumCandidate] = field(default_factory=list)
    range: RangeConstraint | None = None
    readonly: bool = False
    readonly_reason: str | None = None
    dormant: bool = False
    warning: bool = False
    warning_reason_text: str | None = None
    dormant_reason_text: str | None = None
    locator: Any = None


@dataclass
class GroupInfo:
    """分组结构项（§3.6 groups 字段元素）。

    - yaml：包装链剥离后第一层映射的键；组描述来自组键上方独立注释块（YC-2）；
    - python：``# ====`` 节；组描述来自节内「本节总览:」段（P-4）。
    """

    name: str
    description: str | None = None


# ---------------------------------------------------------------------------
# §3.7 Diagnostic
# ---------------------------------------------------------------------------

@dataclass
class Diagnostic:
    """诊断（§3.7）。message 为面向用户的中文说明，须包含可定位信息。

    不变量：``path`` 非空时必须指向已存在的条目。severity 为 warning 的
    加载期诊断**不阻塞**文件进入编辑（U-10）。
    """

    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    path: str | None = None


# ---------------------------------------------------------------------------
# §3.6 ConfigDoc
# ---------------------------------------------------------------------------

def compute_hash(data: bytes) -> str:
    """``content_hash`` 定义（§3.6、§7.6）：字节的 sha256 hexdigest。"""
    return hashlib.sha256(data).hexdigest()


@dataclass
class ConfigDoc:
    """一份已打开文件的语义模型（§3.6）。每文件一个 doc（§3.10）。

    - ``format``：适配器注册名（``'python'`` / ``'yaml'``，§4.2）；
    - ``path``：源文件绝对路径；
    - ``description``：文档级描述。python 为模块 docstring（P-3）；yaml 为 None；
    - ``items``：按文件出现顺序排列；
    - ``groups``：分组结构，按出现顺序；
    - ``diagnostics``：加载期诊断（§3.7）；
    - ``original_bytes``：会话基线字节——load 时为原始文件字节，每次成功提交后
      更新为已落盘字节（§7.5、§7.6）；
    - ``content_hash``：``original_bytes`` 的 sha256（提交前基线校验，§7.6）。
      适配器 load() 负责填充 original_bytes / content_hash（§4.1）；基线更新后
      调用 :meth:`refresh_hash` 保持一致。
    """

    format: str
    path: Path
    description: str | None = None
    items: list[ConfigItem] = field(default_factory=list)
    groups: list[GroupInfo] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    original_bytes: bytes = b""
    content_hash: str = ""

    def refresh_hash(self) -> str:
        """按当前 ``original_bytes`` 重算并写入 ``content_hash``，返回新哈希。

        用于 load 填充与每次成功提交后的基线更新（§7.5、§7.6）。
        """
        self.content_hash = compute_hash(self.original_bytes)
        return self.content_hash


# ---------------------------------------------------------------------------
# §3.8 EditOp
# ---------------------------------------------------------------------------

@dataclass
class EditOp:
    """一次编辑操作（§3.8）。

    - 只带新**值**，不带字面量文本；字面量文本由适配器按 ``literal_style``
      生成（§7.2）；
    - 一次提交 = ``list[EditOp]``（实时落盘模型下通常为单条）；
    - 引用不存在的 ``path`` 属于程序错误：适配器 save() 抛异常（§4.1），
      核心必须拒绝，不产生 Diagnostic。
    """

    path: str
    new_value: bool | int | float | str
