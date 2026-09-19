"""Python 适配器（规范 §4.3，画像 §5.2）——第二阶段实现。

实现库：libcst（保留格式的 CST）。适用范围：模块顶层（缩进级别 0）的赋值
语句。可编辑性判定规则 R-1..R-7（§4.3，按序执行）；注释画像 P-1..P-10
（§5.2：作用/影响/建议分段、``# ====`` 组头、``# ---`` 子组头、
``建议: [a, b]`` 范围推测、``休眠配置:`` 标记、``!!`` 警告标记等）。

验收基准（§11 A-1）：param.py 金样本 101 可编辑条目 + 14 只读条目 +
13 组 + 7 子组；恒等往返 T1 字节一致（§7.3）。

字节层约定（与 YAML 适配器统一）：

- ``load(path)``：读原始字节 → ``doc.original_bytes`` = 原始字节、
  ``content_hash`` = sha256(原始字节)；``detect_encoding`` 失败 → 返回带
  ``E-ENCODING`` 诊断的 doc（不抛异常）；BOM 剥离后解析；行尾混杂给
  ``W-EOL-MIXED``（换行逐行尽力保留）。
- ``save(doc, edits)``：返回 **BOM-less** 完整字节；未知 path → 抛异常。
- ``load`` 返回 ``(doc, diagnostics)``，``doc.diagnostics`` 与返回列表同一份内容。

只读原因登记（§3.5）：本适配器只使用 ``'non_literal'``（R-4 表达式 /
带前缀字符串 / 异体数值字面量）与 ``'container'``（R-5 元组等），无扩展值。

当前实现进度（chunk A+B+C，适配器完工）：R-1/R-2（含注解冲突
``W-ANN-CONFLICT``，适配器扩展码，v1 码表无对应项，已登记）/R-3 可编辑条目
（含 type/value/literal_style）、R-4/R-5 只读条目、R-6 重复赋值
``E-DUP-ASSIGN``（以首个为准）、异体数值只读、P-1/P-2 注释分段、P-3 文档
描述、P-4 分组、P-5 子组、P-6 范围推测（闸门 (a)/(b)，裁量追加 ``∈``）、
P-7 休眠、P-8/C-3 警告、P-9 只读不推测、P-10 不解析"保持 x"、
W-INFER-CONFLICT、save 恒等（T1）、save(edits) 外科手术替换（§7.1）+
字面量风格重放（§7.2 python 全部行，含 str 转义）。

写回裁量登记（chunk C）：

- **module 引用**挂在 doc 私有动态属性 ``_py_module``（与条目
  ``annotation_text`` 同机制；不入序列化面）：0 条目文件（§4.2 空 .py）
  没有 locator 可供反查，save(doc, []) 恒等返回原字节（BOM-less）；
- **E-ENCODING / E-PARSE / 伪造 doc**（无 module）→ save 抛 ValueError
  （即使空 edits）：加载未成功的 doc 不可写回，须重新 load（与 yaml
  适配器"以异常告终"对齐，但异常类型更干净——yaml 侧为 UnicodeDecodeError）；
- **未知 path** → ValueError（程序错误，§3.8/§4.1，与 yaml 一致）；
  **只读条目收到 EditOp** → ValueError（与 yaml 一致）；**值类型与条目
  类型不符** → ValueError（提交期硬校验在核心 §8.2，adapter 防御；
  float 条目收 int 新值合法——必落 float 字面量；非有限 float（inf/nan）
  拒绝，v1 无重放规则）；
- **同一 path 多条 EditOp** → 后者生效（与 yaml 对齐）；多条 edits 一次
  save 全部生效；同行多赋值（``A = 1; B = 2``，locator 带 small_index）
  的多条 edits 依序叠加；
- **fixed(p) 舍入**：新值小数位超出 p → Python ``%.*f`` 默认舍入
  （round-half-even 对精确二进制值；规范未明说，测试锁定：0.125→``0.12``、
  0.375→``0.38``、2.675→``2.67``、1.85→fixed(1)→``1.9``）；
- **str 转义**（python 单行字符串标准转义）：``\\``→``\\\\``、同风格引号→
  ``\\'``/``\\"``（异种引号原样保留）、``\\n``/``\\r``/``\\t``、其余控制字符→
  ``\\xNN``；quote='none'/None 防御 → 双引号（python 无裸字符串，R-3 条目
  不会出现）；
- **一元负号是字面量的一部分**：以 UnaryOperation(Minus) 重放（与 load 期
  拆解对称），``-6.5``→-7→``-7.0``；
- **R-6 重复赋值**：edit 只改首个赋值（locator 指向首个），第二处原样；
- locator 失效（目标不匹配 / raw_literal 与 load 期不一致）→ RuntimeError
  提示重新 load（§7.6，与 yaml 的基线校验对齐）。

画像裁量登记（chunk B）：
- 只读条目同样填 description/dormant/warning（标记与说明非推测；P-9 仅禁
  范围/枚举推测）；
- P-2 相邻无标签行合并为同一前导段；缩进续行保留缩进以 '\\n' 并入上一段；
  标签后文本 trim；
- 标记原文（dormant/warning_reason_text）= 该行 strip 后全文（含 ``#``）；
- P-6 多命中取第一个未被闸门排除者；unit 按 ``]`` 后到句读符（。；;,、）
  机械截取（如 NET_STEP 得到 ``米且 ≤ 2×NET_RADIUS``）；闸门 (a) 追加
  ``∈``（归属描述，见 _RANGE_GATE_CHARS）；``休眠配置：`` 全角冒号兼容；
- R-2 注解文本携带于条目私有动态属性 ``annotation_text``（仅展示，不入
  序列化面）；``float`` 注解 + int 字面量不算冲突（数值塔）；容器/未知
  注解不判定；
- R-6 首个赋值未产出条目时，dup 诊断 path=None（§3.7 path 不变量）。
"""

from __future__ import annotations

import ast as pyast
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import libcst as cst

from ..bytesio import detect_encoding, detect_mixed_eols, strip_bom
from ..model import (
    CODE_E_DUP_ASSIGN,
    CODE_E_ENCODING,
    CODE_E_PARSE,
    CODE_W_EOL_MIXED,
    CODE_W_INFER_CONFLICT,
    PROV_COMMENT,
    PROV_INFERRED,
    READONLY_CONTAINER,
    READONLY_NON_LITERAL,
    SEV_ERROR,
    SEV_WARNING,
    ConfigDoc,
    ConfigItem,
    Diagnostic,
    EditOp,
    GroupInfo,
    LiteralStyle,
    RangeConstraint,
    Section,
    classify_float_form,
    is_exotic_numeric,
)

__all__ = ["PythonAdapter"]

UNGROUPED = "（未分组）"  # §3.6 python 伪分组

# P-4：`#` + ≥10 个 `=` 的行（组头分隔线）
_GROUP_SEP_RE = re.compile(r"^#\s*={10,}\s*$")
# P-5：以 `# ---` 开头的行（子组头）
_SUBGROUP_RE = re.compile(r"^#\s*---")
# P-2/R-4：段标签（全角冒号同样接受）
_LABEL_RE = re.compile(r"^(作用|影响|建议)\s*[:：]")
# P-4：组描述段标签
_OVERVIEW_RE = re.compile(r"^本节总览\s*[:：]\s*")
# P-7：休眠标记行（规范只列半角冒号；全角同样接受，裁量已登记）
_DORMANT_RE = re.compile(r"^休眠配置\s*[:：]")
# P-8/C-3：≥2 个连续 `!`，前后不是字母或数字
_WARN_BANG_RE = re.compile(r"(?<![0-9A-Za-z])!{2,}(?![0-9A-Za-z])")
# P-6：范围模式 `[ <num> , <num> ]`（num = -?\d+(\.\d+)?，允许空白）
_RANGE_RE = re.compile(r"\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]")
# P-6 闸门 (a)：`[` 前紧邻（忽略空白）字符属于增量算符则排除。
# 裁量追加 `∈`：`x∈[0, 7]` 为集合归属描述而非建议范围（param.py L405 续行；
# 不排除会给 X_OUR_KICKOFF_TARGET 产出 [0,7] 并触发第 3 条 W-INFER-CONFLICT，
# 违反 §11 A-8 恰 2 条的验收基准）。
_RANGE_GATE_CHARS = "+-*×/∈"
# P-6：unit 截取终止符（`]` 之后到下一句读符）
_UNIT_STOP = "。；;,、"
# R-2：注解基名 → 字面量类型（仅标量注解参与冲突判定；tuple[...] 等不判定）
_ANN_SCALAR_TYPES = {"int": "int", "float": "float", "str": "str", "bool": "bool"}
# R-2 注解冲突诊断码（v1 码表 §3.7 无对应码，适配器扩展，已登记）
CODE_W_ANN_CONFLICT = "W-ANN-CONFLICT"


@dataclass
class _PyLocator:
    """写回定位信息（适配器私有，§3.1）。

    所有条目共享同一个 ``module``（load 时解析出的 libcst Module）；
    ``body_index`` 为该赋值所在顶层 SimpleStatementLine 在 ``module.body``
    中的下标；``small_index`` 为赋值小语句在该行 ``stmt.body`` 中的下标
    （`A = 1; B = 2` 同行多赋值防御）；``target`` 为目标变量名（save 时
    防御性校验）。R-6 重复赋值时 locator 指向**首个**赋值（以首个为准）。
    """

    module: cst.Module
    body_index: int
    target: str
    small_index: int = 0


@dataclass
class _GroupSpan:
    """P-4 组头的行号区间与组描述。"""

    name: str
    start: int          # 首个 `====` 分隔线行号（1-based，含）
    end: int            # 下一组首个分隔线行号（不含）；末组为行数+1
    header_end: int     # 结束组头的第二个 `====` 行号
    description: str | None = None
    overview_span: tuple[int, int] | None = None  # 总览段行号区间（1-based，含两端）


@dataclass
class _SubgroupMark:
    """P-5 子组头。"""

    title: str
    lineno: int
    group_index: int    # 所属 _GroupSpan 在 groups 列表中的下标；-1 = 无组


class PythonAdapter:
    """§4.1 Adapter 接口的 Python 实现（§4.3）。"""

    name = "python"
    extensions: tuple[str, ...] = (".py",)

    # ------------------------------------------------------------------
    # detect（§4.2）
    # ------------------------------------------------------------------
    def detect(self, path: Path, head: bytes) -> bool:
        """扩展名 .py 优先；内容嗅探 = 可被 libcst 解析为模块。不得抛异常。"""
        try:
            if path.suffix == ".py":
                return True
            data, _has_bom = strip_bom(head)
            text = data.decode("utf-8")
            cst.parse_module(text)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # load（§4.3）
    # ------------------------------------------------------------------
    def load(self, path: Path) -> tuple[ConfigDoc, list[Diagnostic]]:
        raw = Path(path).read_bytes()
        doc = ConfigDoc(format=self.name, path=Path(path).resolve())
        doc.original_bytes = raw
        doc.refresh_hash()

        diag = detect_encoding(raw)
        if diag is not None:
            doc.diagnostics.append(diag)
            return doc, doc.diagnostics

        data, _has_bom = strip_bom(raw)
        if detect_mixed_eols(data):
            doc.diagnostics.append(
                Diagnostic(
                    severity=SEV_WARNING,
                    code=CODE_W_EOL_MIXED,
                    message="文件行尾风格混杂（LF 与 CRLF 并存），逐行保真为尽力而为",
                )
            )

        text = data.decode("utf-8")
        try:
            module = cst.parse_module(text)
        except Exception as exc:  # cst.ParserError 等
            doc.diagnostics.append(
                Diagnostic(
                    severity=SEV_ERROR,
                    code=CODE_E_PARSE,
                    message=f"Python 解析失败：{exc}",
                )
            )
            return doc, doc.diagnostics

        # 模块引用挂在 doc 私有动态属性（与 annotation_text 同机制，裁量）：
        # 0 条目文件（§4.2 空 .py）没有 locator 可供 save 反查 module。
        doc._py_module = module
        self._collect(doc, module, text)
        return doc, doc.diagnostics

    # ------------------------------------------------------------------
    # save（§7.1/§7.2）
    # ------------------------------------------------------------------
    def save(self, doc: ConfigDoc, edits: Sequence[EditOp]) -> bytes:
        """外科手术式写回：只替换目标赋值的值节点，其余字节原样（§7.1）。

        - 空 edits → 恒等返回基线字节（T1；0 条目空文件 → 原字节 BOM-less）；
        - 未知 path → ValueError（程序错误，§3.8/§4.1）；
        - 只读条目收到 EditOp → ValueError（与 yaml 适配器一致，裁量）；
        - 值类型与条目类型不符 → ValueError（提交门控在核心 §8.2，此处防御）；
        - doc 加载未成功（E-ENCODING / E-PARSE / 伪造 doc，无 module）→
          ValueError（即使空 edits，裁量：与 yaml 一样以异常告终）；
        - 同一 path 多条 EditOp → 后者生效（与 yaml 对齐，裁量）；
        - 多条 edits 一次 save 全部生效；
        - 返回 BOM-less 完整字节，写盘（原子写 §7.4）由核心负责。
        """
        if doc.format != self.name:
            raise ValueError(
                f"doc.format={doc.format!r} 不是本适配器（'python'）的产物"
            )
        module = self._module_of(doc)
        if not edits:
            return module.bytes

        by_path = {it.path: it for it in doc.items}
        ops: dict[str, EditOp] = {}
        for op in edits:
            if op.path not in by_path:
                raise ValueError(
                    f"EditOp 引用未知 path {op.path!r}（{doc.path}），"
                    "属程序错误（§3.8、§4.1）"
                )
            ops[op.path] = op  # 同 path 多条 → 后者生效（裁量，与 yaml 对齐）

        body = list(module.body)
        for path, op in ops.items():
            item = by_path[path]
            loc = item.locator
            if item.readonly or not isinstance(loc, _PyLocator):
                raise ValueError(
                    f"条目 {path!r} 为只读（readonly_reason="
                    f"{item.readonly_reason!r}），不可编辑（§3.5、§4.3 R-7）"
                )
            if loc.module is not module:
                raise RuntimeError(
                    f"条目 {path!r} 的 locator 指向其他 module（doc 被拼装/复制？），"
                    "须重新 load（§7.6）"
                )
            node = _render_value_node(item, op.new_value)  # 类型不符 → ValueError
            # 校验用原始树（不随本轮替换变化），替换作用于 body 副本——
            # 同行多赋值（`A = 1; B = 2`）的多条 edits 依序叠加。
            orig_stmt = module.body[loc.body_index]
            cur_stmt = body[loc.body_index]
            body[loc.body_index] = _replace_stmt_value(
                module, orig_stmt, cur_stmt, loc, item, node
            )
        return module.with_changes(body=body).bytes

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _module_of(doc: ConfigDoc) -> cst.Module:
        mod = getattr(doc, "_py_module", None)  # load 时挂载（私有动态属性）
        if isinstance(mod, cst.Module):
            return mod
        for item in doc.items:  # 兼容兜底：从条目 locator 反查
            loc = item.locator
            if isinstance(loc, _PyLocator):
                return loc.module
        raise ValueError(
            "doc 不是 PythonAdapter.load() 成功解析的产物（E-ENCODING / "
            "解析失败 / locator 缺失），不可写回；须重新 load（§7.6）"
        )

    def _collect(self, doc: ConfigDoc, module: cst.Module, text: str) -> None:
        """模块顶层赋值 → 条目（R-1..R-5/R-7）+ P-3 描述 + P-4/P-5 分组。"""
        lines = text.splitlines()

        # P-3：模块 docstring → ConfigDoc.description
        try:
            doc.description = pyast.get_docstring(pyast.parse(text))
        except SyntaxError:  # 理论上不可达（libcst 已解析成功）
            doc.description = None

        # P-4/P-5：结构行扫描（组头、子组头、组描述）
        groups = _scan_groups(lines)
        subgroups = _scan_subgroups(lines, groups)
        for g in groups:
            doc.groups.append(GroupInfo(name=g.name, description=g.description))

        # 条目注释块上行扫描的附加硬边界：组头后的「本节总览:」段行号
        boundaries = frozenset(
            ln
            for g in groups
            if g.overview_span is not None
            for ln in range(g.overview_span[0], g.overview_span[1] + 1)
        )

        # 各小语句（Assign/AnnAssign 本体）的精确起始行号。
        # 注意：不能用 SimpleStatementLine 的位置——其 leading_lines（含组头
        # 注释）会并入语句起点，导致行号归属错位。
        positions = cst.metadata.MetadataWrapper(
            module, unsafe_skip_copy=True
        ).resolve(cst.metadata.PositionProvider)

        ungrouped_used = False
        seen: dict[str, bool] = {}  # 顶层赋值目标名 → 是否已产出条目（R-6）
        for index, stmt in enumerate(module.body):
            if not isinstance(stmt, cst.SimpleStatementLine):
                continue
            for small_index, small in enumerate(stmt.body):
                target, value = _assign_target_value(small)
                if target is None or value is None:
                    continue
                lineno = positions[small].start.line
                if target in seen:
                    # R-6：同名重复赋值 → E-DUP-ASSIGN error，以首个为准
                    # （第二个不生成条目，保 I-1 路径唯一）。path 仅在首个
                    # 赋值确有产出条目时填写（诊断 path 不变量，§3.7）。
                    doc.diagnostics.append(
                        Diagnostic(
                            severity=SEV_ERROR,
                            code=CODE_E_DUP_ASSIGN,
                            message=(
                                f"顶层变量 {target} 重复赋值（第 {lineno} 行），"
                                f"以首个赋值为准，本行被忽略"
                            ),
                            path=target if seen[target] else None,
                        )
                    )
                    continue
                group, subgroup = _locate(groups, subgroups, lineno)
                loc = _PyLocator(
                    module=module, body_index=index, target=target,
                    small_index=small_index,
                )
                item = self._make_item(
                    module, lines, target, value, group, subgroup, loc, lineno,
                    boundaries,
                )
                seen[target] = item is not None
                if item is not None:
                    if group == UNGROUPED:
                        ungrouped_used = True
                    doc.items.append(item)
                    # R-2：AnnAssign 注解仅展示（携带方式裁量：ConfigItem
                    # 动态私有属性 annotation_text）；注解与字面量类型冲突
                    # → warning 诊断，type 仍按字面量（§8.1）。
                    ann = _annotation_text(module, small)
                    if ann is not None:
                        item.annotation_text = ann
                        if _ann_type_conflict(ann, item.type):
                            doc.diagnostics.append(
                                Diagnostic(
                                    severity=SEV_WARNING,
                                    code=CODE_W_ANN_CONFLICT,
                                    message=(
                                        f"变量 {target}（第 {lineno} 行）类型注解 "
                                        f"`{ann}` 与右值字面量类型 {item.type} 冲突，"
                                        f"type 仍按字面量判定"
                                    ),
                                    path=target,
                                )
                            )
                    # W-INFER-CONFLICT：现值违反 inferred 约束（加载期黄色
                    # 警告，不阻塞提交；§5.2 冲突正例、§8.2）
                    doc.diagnostics.extend(_infer_conflict_diags(item))

        # 伪分组（未分组）：仅当确有散落条目时进入 groups（按出现顺序在最前）
        if ungrouped_used:
            doc.groups.insert(0, GroupInfo(name=UNGROUPED, description=None))

    # ------------------------------------------------------------------
    def _make_item(
        self,
        module: cst.Module,
        lines: list[str],
        target: str,
        value: cst.BaseExpression,
        group: str,
        subgroup: str | None,
        loc: _PyLocator,
        lineno: int,
        boundaries: frozenset[int] = frozenset(),
    ) -> ConfigItem | None:
        """按 R-3/R-4/R-5（含异体数值、前缀字符串）产出条目或 None（不生成）。

        生成的条目（含只读）统一应用 §5.2 注释画像：P-1/P-2 说明分段、
        P-7 休眠、P-8 警告；P-6 范围推测仅可编辑条目（P-9）。
        """
        common: dict[str, Any] = dict(
            path=target, group=group, subgroup=subgroup, locator=loc
        )
        # P-1：条目注释块（只读路径的 R-4 门槛与画像共用同一次扫描）
        block = _item_comment_lines(lines, lineno, boundaries)

        item: ConfigItem | None
        # R-5：元组/列表/字典/集合字面量 → 只读 container（raw 保留全文）
        if isinstance(value, (cst.Tuple, cst.List, cst.Dict, cst.Set)):
            item = ConfigItem(
                raw_literal=module.code_for_node(value),
                readonly=True,
                readonly_reason=READONLY_CONTAINER,
                **common,
            )
        else:
            # R-3：标量字面量 → 可编辑条目（type/value/literal_style）
            scalar = _scalar_info(value)
            # §3.2：异体数值字面量（1_000 / 0x10 / 1e3 等）→ v1 必须只读
            exotic = None if scalar is not None else _exotic_numeric_info(value)
            if scalar is not None:
                typ, pyval, style, token = scalar
                item = ConfigItem(
                    type=typ, value=pyval, raw_literal=token, literal_style=style,
                    **common,
                )
            elif exotic is not None:
                typ, pyval, token = exotic
                item = ConfigItem(
                    type=typ, value=pyval, raw_literal=token,
                    readonly=True, readonly_reason=READONLY_NON_LITERAL,
                    **common,
                )
            elif _has_profile_labels(block):
                # R-4：其他表达式（含前缀字符串/隐式拼接/f-string）→ 带条目
                # 注释块（含 作用:/影响:/建议: 任一标签）则只读 non_literal
                item = ConfigItem(
                    raw_literal=module.code_for_node(value),
                    readonly=True,
                    readonly_reason=READONLY_NON_LITERAL,
                    **common,
                )
            else:
                item = None  # R-4：无条目注释块 → 不生成

        if item is None:
            return None
        _apply_profile(item, block)
        return item


# ----------------------------------------------------------------------
# 赋值目标提取
# ----------------------------------------------------------------------
def _assign_target_value(small: Any) -> tuple[str | None, Any]:
    """Assign/AnnAssign、单目标 Name → (目标名, RHS 节点)；否则 (None, None)。"""
    if isinstance(small, cst.Assign):
        if len(small.targets) != 1:
            return None, None
        tgt = small.targets[0].target
        if isinstance(tgt, cst.Name):
            return tgt.value, small.value
        return None, None
    if isinstance(small, cst.AnnAssign):
        if isinstance(small.target, cst.Name) and small.value is not None:
            return small.target.value, small.value
        return None, None
    return None, None


def _annotation_text(module: cst.Module, small: Any) -> str | None:
    """R-2：AnnAssign 的类型注解源码文本（仅展示）；非 AnnAssign → None。"""
    if isinstance(small, cst.AnnAssign) and small.annotation is not None:
        try:
            return module.code_for_node(small.annotation.annotation)
        except Exception:  # 防御：异常节点不阻断加载
            return None
    return None


def _ann_type_conflict(ann_text: str, typ: str | None) -> bool:
    """R-2：注解基名与 R-3 字面量类型是否冲突（如 ``X: int = 1.5``）。

    - 仅判定标量注解基名（int/float/str/bool）；``tuple[...]``/``list[...]``
      等容器与未知注解不判定（金样本 3 个 tuple[...] 元组表不得误报）；
    - 裁量：``float`` 注解 + int 字面量不算冲突（Python 数值塔惯例）；
    - typ 为 None（只读/类型未知）不判定。
    """
    if typ is None:
        return False
    m = re.match(r"[A-Za-z_][A-Za-z_0-9]*", ann_text.strip())
    if not m:
        return False
    expected = _ANN_SCALAR_TYPES.get(m.group(0))
    if expected is None:
        return False
    if expected == "float" and typ == "int":
        return False
    return expected != typ


# ----------------------------------------------------------------------
# §7.1 外科手术替换 + §7.2 字面量风格重放（python 相关行）
# ----------------------------------------------------------------------
def _replace_stmt_value(
    module: cst.Module,
    orig_stmt: Any,
    cur_stmt: Any,
    loc: _PyLocator,
    item: ConfigItem,
    node: cst.BaseExpression,
) -> cst.SimpleStatementLine:
    """把 ``cur_stmt``（可能已含同行其他小语句的替换）中 loc 指向的赋值
    的值节点换成 ``node``，其余（等号两侧空白/对齐、行尾注释、缩进、
    leading_lines、同行其他赋值）逐字节不动。

    ``orig_stmt`` 为原始树中的同一语句，用于防御性校验（目标名、
    raw_literal 与 load 期一致——locator 失效 → RuntimeError，§7.6）。
    """
    if not isinstance(orig_stmt, cst.SimpleStatementLine) or not isinstance(
        cur_stmt, cst.SimpleStatementLine
    ):
        raise RuntimeError(
            f"条目 {loc.target!r} 的 locator 已失效（非简单语句行），"
            "须重新 load（§7.6）"
        )
    if loc.body_index >= len(module.body):
        raise RuntimeError(f"条目 {loc.target!r} 的 locator 已失效，须重新 load")
    if loc.small_index >= len(orig_stmt.body) or loc.small_index >= len(cur_stmt.body):
        raise RuntimeError(f"条目 {loc.target!r} 的 locator 已失效，须重新 load")
    o_small = orig_stmt.body[loc.small_index]
    o_target, o_value = _assign_target_value(o_small)
    if o_target != loc.target or o_value is None:
        raise RuntimeError(
            f"条目 {loc.target!r} 的 locator 已失效（目标不匹配），"
            "须重新 load（§7.6）"
        )
    if module.code_for_node(o_value) != item.raw_literal:
        raise RuntimeError(
            f"条目 {loc.target!r} 的 locator 已失效（基线字节与 load 期不一致），"
            "须重新 load（§7.6）"
        )
    small = cur_stmt.body[loc.small_index]
    target, value = _assign_target_value(small)
    if target != loc.target or value is None:
        raise RuntimeError(f"条目 {loc.target!r} 的 locator 已失效，须重新 load")
    new_small = small.with_changes(value=node)
    new_body = list(cur_stmt.body)
    new_body[loc.small_index] = new_small
    return cur_stmt.with_changes(body=new_body)


def _render_value_node(item: ConfigItem, new_value: Any) -> cst.BaseExpression:
    """按条目 type 与 literal_style 生成新值 CST 节点（§7.2 python 相关行）。

    值类型与条目类型不符 → ValueError（提交期硬校验在核心 §8.2；
    adapter 层防御，与 yaml render_literal 语义对齐，裁量登记）：
    - float 条目收 int 新值 → 允许，落 float 字面量（fixed(2) 收 2 → ``2.00``）；
    - int 条目收 float/bool → 拒绝；bool 条目收非 bool → 拒绝；
      str 条目收非 str → 拒绝；
    - 非有限 float（inf/nan）→ 拒绝（v1 无对应字面量重放，防御）。
    """
    typ = item.type
    style = item.literal_style or LiteralStyle()
    if typ == "bool":
        if not isinstance(new_value, bool):
            raise ValueError(
                f"bool 条目 {item.path!r} 只接受真布尔，收到 {new_value!r}"
            )
        return cst.Name("True" if new_value else "False")  # python 恒 capital
    if typ == "int":
        if isinstance(new_value, bool) or not isinstance(new_value, int):
            raise ValueError(
                f"int 条目 {item.path!r} 只接受整数（十进制原样重放），"
                f"收到 {new_value!r}"
            )
        return _number_node(str(new_value))
    if typ == "float":
        if isinstance(new_value, bool) or not isinstance(new_value, (int, float)):
            raise ValueError(
                f"float 条目 {item.path!r} 只接受数值，收到 {new_value!r}"
            )
        f = float(new_value)
        if not math.isfinite(f):
            raise ValueError(
                f"float 条目 {item.path!r} 收到非有限值 {new_value!r}"
                "（inf/nan 无 v1 字面量重放规则，防御）"
            )
        return _number_node(_render_float_py(style.float_form, f))
    if typ == "str":
        if not isinstance(new_value, str):
            raise ValueError(
                f"str 条目 {item.path!r} 只接受文本，收到 {new_value!r}"
            )
        return cst.SimpleString(_render_str_py(new_value, style.quote))
    raise ValueError(
        f"条目 {item.path!r} type={typ!r} 不可编辑（只读），无法重放字面量"
    )


def _number_node(literal: str) -> cst.BaseExpression:
    """十进制数值字面量文本 → CST 节点；一元负号是字面量的一部分（§7.2），
    以 UnaryOperation(Minus) 重放（与 load 期 _scalar_info 的拆解对称）。"""
    if literal.startswith("-"):
        mag = literal[1:]
        inner = cst.Float(mag) if "." in mag else cst.Integer(mag)
        return cst.UnaryOperation(operator=cst.Minus(), expression=inner)
    return cst.Float(literal) if "." in literal else cst.Integer(literal)


def _render_float_py(form: Any, f: float) -> str:
    """§7.2 浮点风格重放（python 行；与 yaml _render_float 规则一致）。

    - trailing_dot：新值为整数 → ``N.``；非整数 → 最短表示（repr）；
    - fixed(p)：恰好 p 位小数（不足补零；**超出按 Python ``%.*f`` 默认
      舍入 = round-half-even（对精确二进制值），规范未明说，裁量登记 +
      测试锁定**：0.125→fixed(2)→``0.12``、0.375→``0.38``、
      1.85→fixed(1)→``1.9``）；
    - plain：最短 round-trip 表示（repr 语义：0.15→``0.15``；整数值 float
      2.0→``2.0`` 而非 ``2.``）；
    - 数值语义：只改字符串表示不改数值（float 条目收 int 新值必落 float
      字面量：fixed(2) 收 2 → ``2.00``）；负号随字面量重放（-7 收进
      fixed(1) 条目 → ``-7.0``）。
    """
    if form == "trailing_dot":
        if f.is_integer():
            return "%d." % int(f)
        return repr(f)
    if isinstance(form, tuple) and len(form) == 2 and form[0] == "fixed":
        return "%.*f" % (int(form[1]), f)
    return repr(f)  # plain（及防御兜底）


def _render_str_py(value: str, quote: str | None) -> str:
    """§7.2 str 转义重放：按原 quote 风格（single/double）生成字面量。

    转义规则（python 单行字符串标准转义）：``\\`` → ``\\\\``；与本风格
    同种的引号 → ``\\'`` / ``\\"``（异种引号无需转义，原样保留）；换行 →
    ``\\n``、回车 → ``\\r``、制表 → ``\\t``；其余控制字符（<0x20、0x7F）→
    ``\\xNN``。python str 条目 quote 恒为 single/double（R-3）；
    'none'/None 防御 → 双引号（python 无裸字符串，裁量登记）。
    """
    q = "'" if quote == "single" else '"'
    out = [q]
    for ch in value:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == q:
            out.append("\\" + q)
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
    out.append(q)
    return "".join(out)


# ----------------------------------------------------------------------
# R-3 标量字面量判定
# ----------------------------------------------------------------------
def _scalar_info(
    value: cst.BaseExpression,
) -> tuple[str, Any, LiteralStyle, str] | None:
    """RHS 为 R-3 纯标量字面量 → (type, value, literal_style, raw_token)。

    异体数值（is_exotic_numeric）与带前缀字符串不在此列（分别走只读与
    R-4 路径）。
    """
    node = value
    sign = ""
    if isinstance(node, cst.UnaryOperation) and isinstance(node.operator, cst.Minus):
        sign = "-"
        node = node.expression
    if isinstance(node, (cst.Integer, cst.Float)):
        token = node.value
        if is_exotic_numeric(sign + token):
            return None
        try:
            pyval = pyast.literal_eval(sign + token)
        except (ValueError, SyntaxError):
            return None
        if isinstance(node, cst.Integer):
            return "int", pyval, LiteralStyle(), sign + token
        return (
            "float",
            pyval,
            LiteralStyle(float_form=classify_float_form(token)),
            sign + token,
        )
    if isinstance(node, cst.Name) and node.value in ("True", "False") and not sign:
        return "bool", node.value == "True", LiteralStyle(), node.value
    if isinstance(node, cst.SimpleString) and not sign:
        token = node.value
        if token[:1] not in ("'", '"'):
            return None  # r'' / b'' / f'' 等前缀字符串 → R-4 路径
        try:
            pyval = pyast.literal_eval(token)
        except (ValueError, SyntaxError):
            return None
        if not isinstance(pyval, str):
            return None  # b'' 等（防御，libcst 一般归为 SimpleString 前缀形式）
        quote = "single" if token[0] == "'" else "double"
        return "str", pyval, LiteralStyle(quote=quote), token
    return None


def _exotic_numeric_info(
    value: cst.BaseExpression,
) -> tuple[str, Any, str] | None:
    """RHS 为异体数值字面量（可带一元负号）→ (type, value, raw_token)。"""
    node = value
    sign = ""
    if isinstance(node, cst.UnaryOperation) and isinstance(node.operator, cst.Minus):
        sign = "-"
        node = node.expression
    if not isinstance(node, (cst.Integer, cst.Float)):
        return None
    token = sign + node.value
    if not is_exotic_numeric(token):
        return None
    try:
        pyval = pyast.literal_eval(token)
    except (ValueError, SyntaxError):
        return None
    typ = "int" if isinstance(node, cst.Integer) else "float"
    return typ, pyval, token


# ----------------------------------------------------------------------
# P-4 组头 / 组描述、P-5 子组头扫描（纯文本行级）
# ----------------------------------------------------------------------
def _strip_comment_hash(line: str) -> str:
    """注释行去掉行首 `#` 与一个可选空格（P-1/P-2 约定）。"""
    s = line.strip()
    if s.startswith("#"):
        s = s[1:]
        if s.startswith(" "):
            s = s[1:]
    return s


def _scan_groups(lines: list[str]) -> list[_GroupSpan]:
    """P-4：`====` 行开组头 → 首个非空 `#` 行为组名 → 再一个 `====` 行结束；
    组头后紧邻的「本节总览:」段为组描述。"""
    groups: list[_GroupSpan] = []
    n = len(lines)
    i = 0
    while i < n:
        if not _GROUP_SEP_RE.match(lines[i].strip()):
            i += 1
            continue
        start = i + 1  # 1-based 行号
        # 组名：其后第一行非空 `#` 行
        name = None
        name_idx = None
        j = i + 1
        while j < n:
            s = lines[j].strip()
            if not s.startswith("#") or _GROUP_SEP_RE.match(s):
                break
            content = _strip_comment_hash(s)
            if content:
                name = content
                name_idx = j
                break
            j += 1
        if name is None:
            i += 1  # 防御：无组名的孤立分隔线，不当作组头
            continue
        # 结束组头的第二个 `====` 行
        header_end = name_idx + 1  # 1-based；防御默认 = 组名行
        k = name_idx + 1
        while k < n:
            s = lines[k].strip()
            if _GROUP_SEP_RE.match(s):
                header_end = k + 1  # 1-based
                break
            if not s.startswith("#"):
                break
            k += 1
        overview, overview_span = _extract_overview(lines, header_end)
        groups.append(
            _GroupSpan(
                name=name,
                start=start,
                end=n + 1,  # 暂置文件尾，随后修正
                header_end=header_end,
                description=overview,
                overview_span=overview_span,
            )
        )
        i = k + 1 if k < n else n
    for prev, nxt in zip(groups, groups[1:]):
        prev.end = nxt.start
    return groups


def _extract_overview(
    lines: list[str], header_end: int
) -> tuple[str | None, tuple[int, int] | None]:
    """组头结束行后紧邻的「本节总览:」段（连续 `#` 行，至空行/非注释止）。

    返回 (文本, 行号区间)；区间为 1-based 含两端，供条目注释块上行扫描
    作为硬边界（防止组头后总览段在无空行分隔的合成文件中被误归首条目）。
    """
    idx = header_end  # 0-based：header_end(1-based) 的下一行
    if idx >= len(lines):
        return None, None
    s = lines[idx].strip()
    if not s.startswith("#"):
        return None, None
    m = _OVERVIEW_RE.match(_strip_comment_hash(s))
    if not m:
        return None, None
    parts = [_strip_comment_hash(s)[m.end():]]
    j = idx + 1
    while j < len(lines):
        t = lines[j].strip()
        if not t.startswith("#"):
            break
        if _GROUP_SEP_RE.match(t) or _SUBGROUP_RE.match(t):
            break
        content = _strip_comment_hash(t)
        # 裁量防御：无空行分隔的合成文件中，条目画像标记行（作用/影响/建议、
        # 休眠配置:、!!）视为条目注释块起点，总览段到此为止（param.py 总览
        # 恒以空行结束，不受影响）。
        if (
            _LABEL_RE.match(content)
            or _DORMANT_RE.search(content)
            or _WARN_BANG_RE.search(content)
        ):
            break
        parts.append(content)
        j += 1
    return "\n".join(parts), (idx + 1, j)  # 0-based idx..j-1 → 1-based idx+1..j


def _subgroup_title(line: str) -> str | None:
    """P-5：`# ---` 行 → 标题（去掉开头 `# ---` 与结尾 `---`，trim）。"""
    s = line.strip()
    if not _SUBGROUP_RE.match(s):
        return None
    rest = re.sub(r"^#\s*---\s*", "", s)
    if rest.endswith("---"):
        rest = rest[:-3]
    return rest.strip()


def _scan_subgroups(
    lines: list[str], groups: list[_GroupSpan]
) -> list[_SubgroupMark]:
    marks: list[_SubgroupMark] = []
    for idx, line in enumerate(lines):
        title = _subgroup_title(line)
        if title is None:
            continue
        lineno = idx + 1
        gi = -1
        for k, g in enumerate(groups):
            if g.start <= lineno < g.end:
                gi = k
                break
        marks.append(_SubgroupMark(title=title, lineno=lineno, group_index=gi))
    return marks


def _locate(
    groups: list[_GroupSpan], subgroups: list[_SubgroupMark], lineno: int
) -> tuple[str, str | None]:
    """条目行号 → (组名, 子组名|None)。首组之前 → 伪分组（未分组）。"""
    gi = -1
    for k, g in enumerate(groups):
        if g.start <= lineno < g.end:
            gi = k
            break
    if gi < 0:
        return UNGROUPED, None
    sub = None
    for s in subgroups:
        if s.group_index == gi and s.lineno < lineno:
            sub = s.title  # 取最近一个（列表按行号升序）
    return groups[gi].name, sub


# ----------------------------------------------------------------------
# P-1：条目注释块（紧邻、连续、无空行；结构行/总览段为硬边界）
# ----------------------------------------------------------------------
def _item_comment_lines(
    lines: list[str], lineno: int, boundaries: frozenset[int] = frozenset()
) -> list[str]:
    """赋值行（1-based）上方紧邻、连续的 `#` 行（不含结构行；遇空行/非注释止）。

    ``boundaries``：附加硬边界行号集合（1-based，如组头后「本节总览:」段），
    截断且不并入——防止无空行分隔时总览段被误归首条目。
    """
    out: list[str] = []
    idx = lineno - 2  # 0-based：赋值行的上一行
    while idx >= 0:
        if (idx + 1) in boundaries:
            break
        s = lines[idx].strip()
        if not s.startswith("#"):
            break
        if _GROUP_SEP_RE.match(s) or _SUBGROUP_RE.match(s):
            break  # 结构行不并入条目注释块（P-4/P-5）
        out.append(s)
        idx -= 1
    out.reverse()
    return out


def _has_profile_labels(comment_lines: Sequence[str]) -> bool:
    """R-4 门槛：注释块中含 作用:/影响:/建议: 任一标签行。"""
    for s in comment_lines:
        if _LABEL_RE.match(_strip_comment_hash(s).strip()):
            return True
    return False


# ----------------------------------------------------------------------
# P-2：说明分段（作用/影响/建议 + 前导段 + 缩进续行并入）
# ----------------------------------------------------------------------
def _split_sections(block: Sequence[str]) -> list[Section]:
    """注释块行（含 `#`）→ 有序 Section 列表（P-2）。

    - 行首（去 `#` 与一个可选空格后）为 ``作用:``/``影响:``/``建议:``
      （全角冒号兼容）→ 开启对应新段，text 取标签后文本（trim）；
    - 以 ≥2 空格缩进开头 → 并入上一段（多行以 '\\n' 连接，缩进保留，裁量）；
    - 其余无标签行 → 前导段（label=None）；相邻无标签行并入同一段（裁量：
      param.py 无标签行仅出现在块首，如 L341 ``# 开球``）。
    """
    sections: list[Section] = []
    for raw in block:
        content = _strip_comment_hash(raw)
        if content.startswith("  "):  # ≥2 空格缩进 → 续行并入上一段
            if sections:
                prev = sections[-1]
                prev.text = f"{prev.text}\n{content}" if prev.text else content
            else:  # 防御：块首即缩进行 → 视为前导段
                sections.append(Section(label=None, text=content.strip()))
            continue
        m = _LABEL_RE.match(content)
        if m:
            sections.append(Section(label=m.group(1), text=content[m.end():].strip()))
            continue
        # 无标签行：与相邻前导段合并，否则新开前导段
        if sections and sections[-1].label is None:
            prev = sections[-1]
            prev.text = f"{prev.text}\n{content}" if prev.text else content
        else:
            sections.append(Section(label=None, text=content))
    return sections


def _find_marker_line(block: Sequence[str], pattern: re.Pattern[str]) -> str | None:
    """块内首个匹配行 → 该行原文（strip 后，含 `#`；裁量登记）；无则 None。

    P-7 的 ``^休眠配置`` 锚定行首（search 等效 match）；P-8/C-3 的 ``!!``
    模式在行内任意处搜索。匹配对象为去 ``#`` 与一个可选空格后的内容。
    """
    for raw in block:
        if pattern.search(_strip_comment_hash(raw)):
            return raw.strip()
    return None


def _infer_range(sections: Sequence[Section]) -> RangeConstraint | None:
    """P-6：在「建议」段中搜索 `[ <num> , <num> ]` → inferred 范围约束。

    - 闸门 (a)：`[` 前紧邻（忽略空白）为 ``+ - * × /`` 之一 → 增量表达式，
      排除（如 ``KICK_ENTER_M + [0.2, 0.6] 米``）；裁量追加 ``∈``（见
      _RANGE_GATE_CHARS 注释）。
    - 闸门 (b)：``±`` 前缀 → 对称带，v1 不解析（如 ``±[1, 5] 度``）。
    - 多处命中取第一个未被排除的（裁量登记）。
    - unit：``]`` 之后到下一句读符（。；;,、）之间文本 trim；空 → None。
    - P-10：「保持 x.x」等评价文字不解析（本函数只认 `[a, b]` 模式，
      天然满足）。
    """
    for sec in sections:
        if sec.label != "建议":
            continue
        text = sec.text
        for m in _RANGE_RE.finditer(text):
            j = m.start() - 1
            while j >= 0 and text[j] in " \t\n":
                j -= 1
            # 注意 prev 可能为空串（命中在段首）：空串是任何字符串的子串，
            # 必须先判非空，否则段首 `[a, b]` 全被误排除。
            prev = text[j] if j >= 0 else ""
            if prev and (prev == "±" or prev in _RANGE_GATE_CHARS):
                continue  # 闸门 (a)/(b)：排除该命中，继续找后续
            rest = text[m.end():]
            k = 0
            while k < len(rest) and rest[k] not in _UNIT_STOP:
                k += 1
            unit = rest[:k].strip() or None
            return RangeConstraint(
                min=float(m.group(1)),
                max=float(m.group(2)),
                unit=unit,
                provenance=PROV_INFERRED,
            )
    return None


def _fmt_num(x: float) -> str:
    return f"{x:g}"


def _fmt_range(rc: RangeConstraint) -> str:
    if rc.min is not None and rc.max is not None:
        return f"[{_fmt_num(rc.min)}, {_fmt_num(rc.max)}]"
    if rc.min is not None:
        return f"≥{_fmt_num(rc.min)}"
    return f"≤{_fmt_num(rc.max)}"


def _infer_conflict_diags(item: ConfigItem) -> list[Diagnostic]:
    """现值违反 inferred 约束 → W-INFER-CONFLICT warning 诊断（C-2 不拦截）。

    - range：数值型现值（bool 除外）越界即告警；单边界约束只查有界一侧；
    - enum：inferred 候选非空且现值不在候选值中即告警（python v1 不从注释
      推测枚举，此分支为通用性预留，合成测试直接覆盖）。
    """
    out: list[Diagnostic] = []
    rc = item.range
    if (
        rc is not None
        and rc.provenance == PROV_INFERRED
        and isinstance(item.value, (int, float))
        and not isinstance(item.value, bool)
    ):
        if (rc.min is not None and item.value < rc.min) or (
            rc.max is not None and item.value > rc.max
        ):
            out.append(
                Diagnostic(
                    severity=SEV_WARNING,
                    code=CODE_W_INFER_CONFLICT,
                    message=(
                        f"当前值 {_fmt_num(item.value)} 不在推测范围 "
                        f"{_fmt_range(rc)} 内（推测约束，不阻塞提交，条目持续黄标）"
                    ),
                    path=item.path,
                )
            )
    inferred = [c for c in item.enum_candidates if c.provenance == PROV_INFERRED]
    if inferred and item.value not in [c.value for c in inferred]:
        cands = "、".join(repr(c.value) for c in inferred)
        out.append(
            Diagnostic(
                severity=SEV_WARNING,
                code=CODE_W_INFER_CONFLICT,
                message=(
                    f"当前值 {item.value!r} 不在推测枚举候选（{cands}）内"
                    f"（推测约束，不阻塞提交，条目持续黄标）"
                ),
                path=item.path,
            )
        )
    return out


def _apply_profile(item: ConfigItem, block: Sequence[str]) -> None:
    """把注释块画像应用到已生成的条目（可编辑与只读同待遇；P-6 范围推测
    除外，见 P-9）。只读条目填 description 为裁量决定（已登记）。

    - P-2：说明分段（description + description_provenance='comment'）；
    - P-7：``休眠配置:`` 行 → dormant=true + 原文；
    - P-8/C-3：≥2 连续 ``!``（前后非字母数字）→ warning=true + 该行原文。
    """
    if not block:
        return
    sections = _split_sections(block)
    if sections:
        item.description = sections
        item.description_provenance = PROV_COMMENT
    if not item.readonly:
        # P-6 范围推测；P-9：仅可编辑条目（只读条目文案中的 [a, b] 不产生约束）
        item.range = _infer_range(sections)
    dormant_line = _find_marker_line(block, _DORMANT_RE)
    if dormant_line is not None:
        item.dormant = True
        item.dormant_reason_text = dormant_line
    warning_line = _find_marker_line(block, _WARN_BANG_RE)
    if warning_line is not None:
        item.warning = True
        item.warning_reason_text = warning_line
