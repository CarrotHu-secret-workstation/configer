"""YAML 适配器（规范 §4.4 Y-1..Y-9，画像 §5.3 YC-1..YC-7）。

实现库：ruamel.yaml——**只用其 parser/composer 产出的节点树**（ScalarNode /
MappingNode / SequenceNode，携带精确的 start_mark/end_mark 字符指针、style、
anchor）。**不使用 ruamel 的 dump**：实测（ruamel 0.19.1）round-trip dump 无法
逐字节保真金样本（丢行尾空格 ``step_cnt: 50 ``、``True``→``true``、纯空白
缩进行被归一化），故 save 采用对基线文本的**指针手术**（§7.1 外科手术式替换
的 ruamel 等价实现）：仅替换值 token 的字符区间，其余字节原样——T1 恒等往返
（含全部行尾空格）因此天然成立，被编辑行的行尾空格/行尾注释同样原样保留。

类型语义按 YAML 1.2 core 自行解析（:mod:`configer.adapters.yaml_scalar`），
不采信 ruamel 的 1.1 系隐式解析（``off``/``1_000``/timestamp 等歧义，Y-2）。
注释画像（YC-1 行尾说明、YC-2 组描述、C-3 警告）直接按行号从原文提取，不依赖
ruamel 的 ca 挂载（实测前导注释块会被并进前一键的行尾 token，不可靠）。

字节层约定（与 Python 适配器统一）：

- ``load(path)``：读原始字节 → ``doc.original_bytes`` = 原始字节、
  ``content_hash`` = compute_hash(原始字节)；``detect_encoding`` 失败 → 返回带
  E-ENCODING 诊断的 doc（不抛异常）；``strip_bom`` 后解析（只见 BOM-less
  文本）；``detect_mixed_eols`` → W-EOL-MIXED warning。
- ``save(doc, edits)``：对 ``doc.original_bytes``（剥 BOM 后的文本）做手术，
  返回 **BOM-less** 完整字节；未知 path → 抛异常。
- ``load`` 返回 ``(doc, diagnostics)``，``doc.diagnostics`` 与返回列表同一份。

本适配器登记的裁量扩展：

1. ``readonly_reason='non_plain_scalar'``（Y-9 登记值）覆盖一切"字面量无法按
   §7.2 确定性重放"的标量：块标量（``|``/``>``）、``.nan``/``.inf``、null
   叶子（``key:`` 空值，不属 v1 四种标量类型）、异体数值（``0x10``/``1e3``
   等 :func:`~configer.model.is_exotic_numeric`，§3.2）、显式 tag 标量
   （``!!str 5``，span 含 tag 前缀）。注意：``1_000``/``2021-01-01`` 在
   YAML 1.2 core 下解析为 **str**（非数值字面量），§3.2 异体数值只读规则
   不适用，按可编辑 str 处理（Y-2 类型以 1.2 core 为准的直接推论）。
2. 空 mapping（``{}``）→ 只读 ``container`` 条目（不发 I-SEQ-READONLY，该码
   专属序列）；序列值（Y-6）→ 只读 ``container`` + I-SEQ-READONLY，不递归。
3. ``bool_case``：token ∈ {``true``, ``false``} → ``'lower'``，其余大小写变体
   （``True``/``TRUE`` 等）→ ``'capital'``（Y-8：写回各回各家）。
4. 同一 path 多条 EditOp → 后者生效；edits 值类型与条目类型不符 → ValueError；
   编辑只读条目 → ValueError；locator span 校验失败（基线被换而未重新 load，
   程序错误）→ RuntimeError。
5. ``quote='none'`` 的新值裸写不合法（§7.2：含 ``: ``、前导指示符、按 1.2
   core 解析为非 str、空串、含换行等）→ 回退双引号；单引号风格的新值含
   控制字符/换行 → 回退双引号。
6. 根级散落标量的伪分组 ``（根级）`` 以 GroupInfo 形式按首次出现位置插入
   ``doc.groups``（§3.6 只规定条目归属；入列便于 UI 侧栏统一渲染）。
7. 键含 ``.`` → warning 诊断，扩展码 ``W-DOT-KEY``（§3.7 表无对应码；§4.4
   风险表要求"允许加载但给 warning"；写回永远走 locator，安全性不受影响）。
8. 点分路径冲突（如键 ``a.b`` 与嵌套 ``a: {b: …}`` 并存，违反 I-1）→ 后出现
   的条目不收录并给 ``W-DOT-KEY`` warning（病态输入，金样本无）。
9. ``detect``：扩展名命中 → True（内容嗅探**确认**由 registry 流程负责，
   确认失败 → E-PARSE 不换适配器，§4.2）；未命中 → 嗅探 head：UTF-8 解码
   （容错 replace）且可 compose 为**单文档 mapping** 才 True，任何异常 → False。

验收基准（§11 A-2）：config.yaml 金样本 138 标量叶子 + 12 组 + ``（根级）``
伪组；恒等往返 T1 字节一致（§7.3，含行尾空格）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..bytesio import detect_encoding, detect_mixed_eols, strip_bom
from ..model import (
    CODE_E_DUP_KEY,
    CODE_E_PARSE,
    CODE_E_PARSE_MULTIDOC,
    CODE_I_ANCHOR_READONLY,
    CODE_I_ENUM_DROPPED,
    CODE_I_SEQ_READONLY,
    CODE_W_EOL_MIXED,
    PROV_COMMENT,
    PROV_NONE,
    READONLY_ANCHOR,
    READONLY_CONTAINER,
    READONLY_NON_PLAIN_SCALAR,
    SEV_ERROR,
    SEV_INFO,
    SEV_WARNING,
    ConfigDoc,
    ConfigItem,
    Diagnostic,
    EditOp,
    GroupInfo,
    LiteralStyle,
    Section,
    classify_float_form,
    is_exotic_numeric,
)
from .yaml_scalar import (
    has_warning_marker,
    infer_enum_candidates,
    is_special_float_token,
    render_literal,
    resolve_core,
)

__all__ = ["YamlAdapter"]

ROOT_GROUP = "（根级）"
CODE_W_DOT_KEY = "W-DOT-KEY"  # 登记扩展码（模块 docstring 裁量 7/8）


@dataclass
class _YamlLocator:
    """写回定位信息（适配器私有，§3.1 locator：核心与 UI 不得解释）。

    BOM-less 原文中的值 token 字符区间 ``[start, end)`` 与 load 期原文
    ``raw``；save 手术前校验 ``text[start:end] == raw``（裁量 4）。
    """

    start: int
    end: int
    raw: str


class _WalkState:
    """一次 load 遍历的累积状态。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = text.splitlines(keepends=True)
        self.items: list[ConfigItem] = []
        self.groups: list[GroupInfo] = []
        self.diagnostics: list[Diagnostic] = []
        self.seen_nodes: set[int] = set()  # 别名检测：重复出现的集合节点 id
        self.paths_seen: set[str] = set()
        self.root_group_inserted = False

    def ensure_root_group(self) -> None:
        """首个根级散落条目出现时，把 ``（根级）`` 伪组插入当前位置（裁量 6）。"""
        if not self.root_group_inserted:
            self.groups.append(GroupInfo(name=ROOT_GROUP, description=None))
            self.root_group_inserted = True


class YamlAdapter:
    """§4.1 Adapter 接口的 YAML 实现（§4.4、§5.3、§7.1/§7.2）。"""

    name = "yaml"
    extensions: tuple[str, ...] = (".yaml", ".yml")

    # -- detect（§4.2） ------------------------------------------------------

    def detect(self, path: Path, head: bytes) -> bool:
        """扩展名优先；否则嗅探=可解析为单文档 mapping。**不得抛异常**。"""
        try:
            if Path(path).suffix.lower() in self.extensions:
                return True
        except Exception:
            pass
        try:
            text = head.decode("utf-8", errors="replace")
            docs = list(self._new_yaml().compose_all(text))
            from ruamel.yaml.nodes import MappingNode

            return len(docs) == 1 and isinstance(docs[0], MappingNode)
        except Exception:
            return False

    # -- load（§4.4） --------------------------------------------------------

    def load(self, path: Path) -> tuple[ConfigDoc, list[Diagnostic]]:
        """按 Y-1..Y-9 与 YC 画像解析。任何可读输入都不抛异常。"""
        path = Path(path)
        raw = path.read_bytes()
        doc = ConfigDoc(format=self.name, path=path.resolve())
        doc.original_bytes = raw
        doc.refresh_hash()
        diagnostics: list[Diagnostic] = []
        doc.diagnostics = diagnostics

        enc = detect_encoding(raw)
        if enc is not None:
            diagnostics.append(enc)
            return doc, diagnostics

        text_bytes, _has_bom = strip_bom(raw)
        if detect_mixed_eols(text_bytes):
            diagnostics.append(
                Diagnostic(
                    severity=SEV_WARNING,
                    code=CODE_W_EOL_MIXED,
                    message=(
                        f"{path} 行尾风格混杂（LF 与 CRLF 并存），"
                        "逐行保真为尽力而为（§4.1）"
                    ),
                )
            )
        try:
            text = text_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:  # 防御：detect_encoding 已把关
            diagnostics.append(
                Diagnostic(
                    severity=SEV_ERROR,
                    code=CODE_E_PARSE,
                    message=f"YAML 文本解码失败：{exc}",
                )
            )
            return doc, diagnostics

        root = self._compose_single_mapping(text)
        if isinstance(root, Diagnostic):
            diagnostics.append(root)
            return doc, diagnostics

        state = _WalkState(text)
        state.diagnostics = diagnostics
        node = self._strip_wrappers(root)
        self._walk_mapping(node, [], state)
        doc.items = state.items
        doc.groups = state.groups
        return doc, diagnostics

    # -- save（§7.1/§7.2） ---------------------------------------------------

    def save(self, doc: ConfigDoc, edits: Sequence[EditOp]) -> bytes:
        """指针手术：仅替换被编辑值 token 的字符区间，其余字节原样。

        返回 BOM-less 完整字节；未知 path / 只读条目 / 类型不符 → 抛异常
        （程序错误，§3.8、§4.1）。edits 为空 → 恒等返回基线字节（T1）。
        """
        if doc.format != self.name:
            raise ValueError(f"doc.format={doc.format!r} 不是本适配器（'yaml'）的产物")
        text_bytes, _has_bom = strip_bom(doc.original_bytes)
        text = text_bytes.decode("utf-8")
        by_path = {it.path: it for it in doc.items}

        # 同一 path 多条 EditOp → 后者生效（裁量 4）
        ops: dict[str, EditOp] = {}
        for op in edits:
            if op.path not in by_path:
                raise ValueError(
                    f"EditOp 引用未知 path {op.path!r}（{doc.path}），属程序错误（§3.8）"
                )
            ops[op.path] = op

        replacements: list[tuple[int, int, str]] = []
        for op_path, op in ops.items():
            it = by_path[op_path]
            loc: _YamlLocator | None = it.locator
            if it.readonly or loc is None:
                raise ValueError(
                    f"条目 {op_path!r} 为只读（readonly_reason={it.readonly_reason!r}），"
                    "不可编辑（§3.5）"
                )
            if text[loc.start : loc.end] != loc.raw:
                raise RuntimeError(
                    f"条目 {op_path!r} 的 locator 已失效（基线字节与 load 期不一致，"
                    "须重新 load，§7.6）"
                )
            literal = render_literal(it.type, it.literal_style, op.new_value)
            replacements.append((loc.start, loc.end, literal))

        # 降序替换，避免指针漂移；不同 path 的 span 天然不相交
        replacements.sort(key=lambda r: r[0], reverse=True)
        for start, end, literal in replacements:
            text = text[:start] + literal + text[end:]
        return text.encode("utf-8")

    # -- 内部：compose 与结构检查（Y-1、§4.2） --------------------------------

    @staticmethod
    def _new_yaml():
        from ruamel.yaml import YAML

        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.width = 10**9  # 防御性：本适配器不 dump，仅统一配置
        return yaml

    def _compose_single_mapping(self, text: str):
        """compose_all + Y-1 结构检查；成功返回根 MappingNode，失败返回诊断。"""
        from ruamel.yaml.nodes import MappingNode

        try:
            docs = list(self._new_yaml().compose_all(text))
        except Exception as exc:  # ScannerError / ParserError / ReaderError…
            return Diagnostic(
                severity=SEV_ERROR,
                code=CODE_E_PARSE,
                message=f"YAML 解析失败（Y-1）：{_one_line(exc)}",
            )
        if len(docs) > 1:
            return Diagnostic(
                severity=SEV_ERROR,
                code=CODE_E_PARSE_MULTIDOC,
                message=(
                    f"YAML 多文档（{len(docs)} 个文档，`---` 分隔）不受支持，"
                    "v1 仅支持单文档 mapping（Y-1）"
                ),
            )
        if len(docs) == 0 or docs[0] is None:
            return Diagnostic(
                severity=SEV_ERROR,
                code=CODE_E_PARSE,
                message="YAML 文件为空（无可解析文档），顶层必须是 mapping（Y-1、§4.2）",
            )
        if not isinstance(docs[0], MappingNode):
            return Diagnostic(
                severity=SEV_ERROR,
                code=CODE_E_PARSE,
                message=(
                    f"YAML 顶层结构不是 mapping（实际为 {type(docs[0]).__name__}），"
                    "不受支持（Y-1）"
                ),
            )
        return docs[0]

    def _strip_wrappers(self, root):
        """Y-4 包装链剥离：恰一键且值为（无锚点）mapping → 下钻。"""
        from ruamel.yaml.nodes import MappingNode

        node = root
        while isinstance(node, MappingNode) and len(node.value) == 1:
            _key, value = node.value[0]
            if not isinstance(value, MappingNode) or getattr(value, "anchor", None):
                break
            if len(value.value) == 0:
                break  # 空 mapping 不作包装键（无内容可下钻）
            node = value
        return node

    # -- 内部：节点树遍历 ------------------------------------------------------

    def _walk_mapping(self, node, comps: list[str], state: _WalkState) -> None:
        from ruamel.yaml.nodes import MappingNode, ScalarNode, SequenceNode

        seen_keys: dict[str, str | None] = {}  # 本级键 → 首现条目 path（若有）
        for key_node, value_node in node.value:
            comp = self._key_component(key_node, state)
            key_line = getattr(key_node.start_mark, "line", None)

            # Y-7 重复键：error 诊断，条目取首现，文件原样保留
            if comp in seen_keys:
                first_path = ".".join(comps + [comp])
                state.diagnostics.append(
                    Diagnostic(
                        severity=SEV_ERROR,
                        code=CODE_E_DUP_KEY,
                        message=(
                            f"重复键 {comp!r}（第 "
                            f"{(key_line + 1) if key_line is not None else '?'} 行，"
                            f"路径 {first_path}），"
                            "条目取首次出现，文件原样保留（Y-7）"
                        ),
                        path=first_path if first_path in state.paths_seen else None,
                    )
                )
                continue
            seen_keys[comp] = True

            path_comps = comps + [comp]
            anchor = getattr(value_node, "anchor", None)
            is_alias = id(value_node) in state.seen_nodes
            if isinstance(value_node, (MappingNode, SequenceNode)):
                state.seen_nodes.add(id(value_node))

            # Y-6 锚点/别名子树 → 只读 anchor，不递归
            if anchor or is_alias:
                path = self._register_item_path(path_comps, state)
                if path is None:
                    continue
                self._finish_readonly_item(
                    path_comps,
                    path,
                    value_node,
                    state,
                    reason=READONLY_ANCHOR,
                    info_code=CODE_I_ANCHOR_READONLY,
                    info_message=(
                        f"{'别名' if is_alias and not anchor else '锚点'}子树"
                        f"（{anchor or '别名'}）按只读处理（Y-6，v1 不编辑）"
                    ),
                )
                continue

            if isinstance(value_node, MappingNode):
                if len(value_node.value) == 0:
                    # 空 mapping → 只读 container（裁量 2）
                    path = self._register_item_path(path_comps, state)
                    if path is None:
                        continue
                    self._finish_readonly_item(
                        path_comps, path, value_node, state,
                        reason=READONLY_CONTAINER,
                        info_code=None, info_message=None,
                    )
                    continue
                if len(path_comps) == 1:
                    # 分组 = 剥离后第一层映射的键（Y-4、§3.6）；YC-2 组描述
                    state.groups.append(
                        GroupInfo(
                            name=comp,
                            description=self._group_description(state, key_line),
                        )
                    )
                self._walk_mapping(value_node, path_comps, state)
                continue

            if isinstance(value_node, SequenceNode):
                # Y-6 序列值 → 只读 container + I-SEQ-READONLY，不递归
                path = self._register_item_path(path_comps, state)
                if path is None:
                    continue
                self._finish_readonly_item(
                    path_comps, path, value_node, state,
                    reason=READONLY_CONTAINER,
                    info_code=CODE_I_SEQ_READONLY,
                    info_message=f"序列值按只读处理（Y-6，v1 不编辑）：{path}",
                )
                continue

            # ScalarNode → 叶子条目（Y-2/Y-3/Y-5/Y-8/Y-9）
            self._add_scalar_item(path_comps, value_node, state)

    def _key_component(self, key_node, state: _WalkState) -> str:
        from ruamel.yaml.nodes import ScalarNode

        if isinstance(key_node, ScalarNode):
            return str(key_node.value)
        return self._span_text(key_node, state).strip() or "?"

    def _register_item_path(self, path_comps: list[str], state: _WalkState) -> str | None:
        """path 登记（I-1 唯一性防御，裁量 8）；冲突返回 None（不收录）。"""
        path = ".".join(path_comps)
        if path in state.paths_seen:
            state.diagnostics.append(
                Diagnostic(
                    severity=SEV_WARNING,
                    code=CODE_W_DOT_KEY,
                    message=(
                        f"点分路径冲突：{path!r}（键名含 `.` 或与既有路径重名），"
                        "后出现的条目不收录（I-1 路径唯一，裁量 8）"
                    ),
                )
            )
            return None
        state.paths_seen.add(path)
        return path

    # -- 内部：条目构造 --------------------------------------------------------

    def _span_text(self, node, state: _WalkState) -> str:
        return state.text[node.start_mark.pointer : node.end_mark.pointer]

    def _group_of(self, path_comps: list[str], state: _WalkState) -> str:
        if len(path_comps) > 1:
            return path_comps[0]
        state.ensure_root_group()
        return ROOT_GROUP

    def _finish_readonly_item(
        self,
        path_comps: list[str],
        path: str,
        value_node,
        state: _WalkState,
        reason: str,
        info_code: str | None,
        info_message: str | None,
    ) -> None:
        raw = self._span_text(value_node, state)
        it = ConfigItem(
            path=path,
            group=self._group_of(path_comps, state),
            type=None,
            value=None,
            raw_literal=raw,
            readonly=True,
            readonly_reason=reason,
        )
        self._attach_profile(it, value_node, state, editable=False)
        state.items.append(it)
        if info_code is not None:
            state.diagnostics.append(
                Diagnostic(
                    severity=SEV_INFO, code=info_code, message=info_message or "",
                    path=path,
                )
            )

    def _add_scalar_item(
        self, path_comps: list[str], node, state: _WalkState
    ) -> None:
        raw = self._span_text(node, state)
        style_ch = node.style  # None=plain, '"' , "'", '|', '>'
        path = self._register_item_path(path_comps, state)
        if path is None:
            return
        group = self._group_of(path_comps, state)

        # 键含 `.` → warning（Y-4 风险表；写回走 locator 不受影响，裁量 7）
        if any("." in c for c in path_comps):
            state.diagnostics.append(
                Diagnostic(
                    severity=SEV_WARNING,
                    code=CODE_W_DOT_KEY,
                    message=(
                        f"键名含 `.`（路径 {path}，第 {node.start_mark.line + 1} 行），"
                        "点分路径存在歧义；写回走 locator，不受影响（§4.4 风险表）"
                    ),
                    path=path,
                )
            )

        def readonly(reason: str) -> None:
            it = ConfigItem(
                path=path, group=group, type=None, value=None,
                raw_literal=raw, readonly=True, readonly_reason=reason,
            )
            self._attach_profile(it, node, state, editable=False)
            state.items.append(it)

        if style_ch in ("|", ">"):
            readonly(READONLY_NON_PLAIN_SCALAR)  # Y-9 块标量
            return
        if style_ch in ('"', "'"):
            # Y-2 带引号一律 str（如 "192.168.10.110:9876"）
            quote = "double" if style_ch == '"' else "single"
            self._finish_editable(
                path, group, node, state,
                type_="str", value=node.value, raw=raw,
                literal_style=LiteralStyle(quote=quote),
            )
            return
        # plain
        if raw.startswith("!"):
            readonly(READONLY_NON_PLAIN_SCALAR)  # 显式 tag（裁量 1）
            return
        kind, val = resolve_core(raw)
        if kind == "null":
            readonly(READONLY_NON_PLAIN_SCALAR)  # null 叶子（裁量 1）
            return
        if kind == "bool":
            bool_case = "lower" if raw in ("true", "false") else "capital"
            self._finish_editable(
                path, group, node, state,
                type_="bool", value=val, raw=raw,
                literal_style=LiteralStyle(bool_case=bool_case),
            )
            return
        if kind == "int":
            if is_exotic_numeric(raw):
                readonly(READONLY_NON_PLAIN_SCALAR)  # 0x10 等（§3.2，裁量 1）
                return
            self._finish_editable(
                path, group, node, state,
                type_="int", value=val, raw=raw,
                literal_style=LiteralStyle(),
            )
            return
        if kind == "float":
            if is_special_float_token(raw) or is_exotic_numeric(raw):
                readonly(READONLY_NON_PLAIN_SCALAR)  # Y-9 .nan/.inf；§3.2 1e3
                return
            self._finish_editable(
                path, group, node, state,
                type_="float", value=val, raw=raw,
                literal_style=LiteralStyle(float_form=classify_float_form(raw)),
            )
            return
        # str（plain）
        self._finish_editable(
            path, group, node, state,
            type_="str", value=val, raw=raw,
            literal_style=LiteralStyle(quote="none"),
        )

    def _finish_editable(
        self,
        path: str,
        group: str,
        node,
        state: _WalkState,
        type_: str,
        value: Any,
        raw: str,
        literal_style: LiteralStyle,
    ) -> None:
        it = ConfigItem(
            path=path,
            group=group,
            type=type_,
            value=value,
            raw_literal=raw,
            literal_style=literal_style,
            locator=_YamlLocator(
                start=node.start_mark.pointer,
                end=node.end_mark.pointer,
                raw=raw,
            ),
        )
        self._attach_profile(it, node, state, editable=True)
        state.items.append(it)

    # -- 内部：注释画像（YC-1/YC-2/YC-3/C-3，YC-7 dormant 恒 False） ----------

    def _attach_profile(
        self, it: ConfigItem, node, state: _WalkState, editable: bool
    ) -> None:
        """YC-1 行尾说明 → description；C-3 警告；YC-3 枚举推测（仅可编辑）。"""
        comment = self._trailing_comment(node, state)
        if comment:
            it.description = [Section(label=None, text=comment)]
            it.description_provenance = PROV_COMMENT
            # C-3：≥2 个连续 `!`（前后非字母数字）→ warning + 该行原文
            if has_warning_marker(comment):
                it.warning = True
                it.warning_reason_text = state.lines[
                    min(node.end_mark.line, len(state.lines) - 1)
                ].rstrip("\r\n").strip()
            # YC-3 枚举推测（readonly 条目不做推测，对齐 §5.2 P-9 精神）
            if editable and it.type is not None:
                candidates, dropped = infer_enum_candidates(comment, it.type)
                for seg, reason in dropped:
                    state.diagnostics.append(
                        Diagnostic(
                            severity=SEV_INFO,
                            code=CODE_I_ENUM_DROPPED,
                            message=(
                                f"注释枚举候选段 {seg!r} 被丢弃（{reason}），"
                                f"条目 {it.path}（YC-3）"
                            ),
                            path=it.path,
                        )
                    )
                if candidates:
                    it.enum_candidates = candidates
        # YC-7：yaml v1 无休眠标记，dormant 恒 False（默认值即 False）

    def _trailing_comment(self, node, state: _WalkState) -> str | None:
        """YC-1：值 token 所在行 `#` 后文本（trim）。

        单行 token：从 end_mark 之后找注释；跨行 token（块标量/多行 flow）：
        从 start_mark 所在行 token 起点之后找（如 ``key: | # 头注释``）。
        注释起点 `#` 必须位于行首或前一字符为空白（YAML 注释语法）。
        """
        start_line = node.start_mark.line
        end_line = getattr(node.end_mark, "line", start_line)
        if not (0 <= start_line < len(state.lines)):
            return None
        if start_line == end_line:
            rest = state.lines[start_line][node.end_mark.column :]
        else:
            rest = state.lines[start_line][node.start_mark.column :]
        idx = rest.find("#")
        while idx != -1 and not (idx > 0 and rest[idx - 1] in " \t"):
            idx = rest.find("#", idx + 1)
        if idx == -1:
            return None
        text = rest[idx + 1 :].split("\n", 1)[0].strip()
        return text or None

    def _group_description(self, state: _WalkState, key_line: int | None) -> str | None:
        """YC-2：分组键上方紧邻的连续 `#` 行块（中间不得有空行）→ 组描述。"""
        if key_line is None:
            return None
        block: list[str] = []
        i = key_line - 1
        while i >= 0:
            s = state.lines[i].strip()
            if not s.startswith("#"):
                break
            block.append(s)
            i -= 1
        if not block:
            return None
        block.reverse()
        out = []
        for ln in block:
            t = ln[1:]
            if t.startswith(" "):
                t = t[1:]  # 去掉 `#` 与一个可选空格（对齐 P-1 风格）
            out.append(t.rstrip())
        return "\n".join(out)


def _one_line(exc: Exception) -> str:
    return " ".join(str(exc).split())[:300]
