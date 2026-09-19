"""UI 纯函数层：**不 import nicegui**，全部可无头单元测试。

职责（对应规范条目）：
- 分组树构建（U-3）：:func:`build_group_tree`
- 搜索过滤与命中高亮（U-3）：:func:`item_matches` / :func:`filter_items` /
  :func:`highlight_parts`
- 短名与类型标识（U-3）：:func:`short_name` / :func:`type_tag`
- readonly 原因中文映射（U-4，§3.5）：:func:`readonly_reason_zh`
- 范围/枚举展示文本（U-4/U-5）：:func:`range_text` / :func:`enum_option_text`
- 徽标状态计算（U-5/U-8）：:func:`badge_kinds`
- 诊断 → 展示模型（U-10，§3.7）：:func:`diagnostics_level` /
  :func:`diagnostic_rows`
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..model import PROV_INFERRED, SEV_ERROR, SEV_WARNING

if TYPE_CHECKING:  # 仅类型检查期导入，运行期零依赖 UI 框架
    from ..model import ConfigDoc, ConfigItem, Diagnostic, EnumCandidate, RangeConstraint

__all__ = [
    "READONLY_REASON_ZH",
    "BADGE_TEXT",
    "GroupNode",
    "SubgroupNode",
    "readonly_reason_zh",
    "short_name",
    "type_tag",
    "build_group_tree",
    "item_matches",
    "filter_items",
    "highlight_parts",
    "range_text",
    "enum_option_text",
    "badge_kinds",
    "diagnostics_level",
    "diagnostic_rows",
]

# §3.5 readonly_reason 中文映射（任务书 U-4 指定；适配器扩展值原样透传）。
READONLY_REASON_ZH: dict[str, str] = {
    "non_literal": "表达式赋值",
    "container": "容器（元组/序列）",
    "anchor": "锚点子树",
    "non_plain_scalar": "非普通标量",
}

# U-8 展示文案（§3.5 v0.4 修订）。
DORMANT_HINT = "当前不生效（休眠配置）"

# 徽标 kind → 简体中文文案（U-5"推测"字样 / U-8 状态标志）。
BADGE_TEXT: dict[str, str] = {
    "readonly": "只读",
    "dormant": "休眠",
    "warning": "警告",
    "inferred_range": "推测范围",
    "inferred_enum": "推测候选",
}


def readonly_reason_zh(reason: str | None) -> str | None:
    """readonly_reason → 中文展示；未知扩展值原样返回（§3.5 允许适配器扩展）。"""
    if reason is None:
        return None
    return READONLY_REASON_ZH.get(reason, reason)


def short_name(item_path: str) -> str:
    """条目短名（U-3）：yaml 点分路径取末段；python 变量名原样。"""
    return item_path.rsplit(".", 1)[-1] if "." in item_path else item_path


def type_tag(item: "ConfigItem") -> str | None:
    """类型标识（U-3）：int/float/bool/str；type=None（只读未知型）→ '只读'。"""
    if item.type is not None:
        return item.type
    if item.readonly:
        return "只读"
    return None


# ---------------------------------------------------------------------------
# 分组树（U-3）
# ---------------------------------------------------------------------------


@dataclass
class SubgroupNode:
    """子分组节点；``name=None`` 表示该组内无子分组的直属条目。"""

    name: str | None
    items: list[Any] = field(default_factory=list)  # list[ConfigItem]


@dataclass
class GroupNode:
    name: str
    description: str | None = None
    subgroups: list[SubgroupNode] = field(default_factory=list)

    @property
    def item_count(self) -> int:
        return sum(len(sg.items) for sg in self.subgroups)

    def all_items(self) -> list[Any]:
        out: list[Any] = []
        for sg in self.subgroups:
            out.extend(sg.items)
        return out


def build_group_tree(doc: "ConfigDoc") -> list[GroupNode]:
    """doc.items → 组/子组树，保持文件出现顺序（U-3）。

    组顺序：优先 ``doc.groups`` 声明顺序（含描述），其后追加声明外组
    （按条目首次出现顺序，防御性）。组内子组按条目出现顺序；无子组条目
    归入 ``name=None`` 节点，且排在有名子组之前（与 P-5 节内直属条目在
    ``# ---`` 之前的惯例一致——按出现顺序保序即可）。
    """
    group_desc: dict[str, str | None] = {g.name: g.description for g in doc.groups}
    declared_order = [g.name for g in doc.groups]

    tree: dict[str, GroupNode] = {}
    order: list[str] = []
    sub_index: dict[tuple[str, str | None], SubgroupNode] = {}

    for item in doc.items:
        gname = item.group
        if gname not in tree:
            tree[gname] = GroupNode(name=gname, description=group_desc.get(gname))
            order.append(gname)
        key = (gname, item.subgroup)
        if key not in sub_index:
            node = SubgroupNode(name=item.subgroup)
            sub_index[key] = node
            tree[gname].subgroups.append(node)
        sub_index[key].items.append(item)

    ordered = [tree[n] for n in declared_order if n in tree]
    ordered += [tree[n] for n in order if n not in set(declared_order)]
    return ordered


# ---------------------------------------------------------------------------
# 搜索过滤与高亮（U-3）
# ---------------------------------------------------------------------------


def item_matches(item: "ConfigItem", query: str) -> str | None:
    """子串匹配（大小写不敏感）：返回命中域 'path'/'description'，未命中 None。

    说明文本 = 全部 Section 的 label 与 text（U-3"path 与说明文本"）。
    """
    q = query.strip().lower()
    if not q:
        return None
    if q in item.path.lower():
        return "path"
    for sec in item.description:
        if q in sec.text.lower() or (sec.label and q in sec.label.lower()):
            return "description"
    return None


def filter_items(items: list["ConfigItem"], query: str) -> list["ConfigItem"]:
    """过滤（保序）；空查询 → 原样返回。"""
    if not query.strip():
        return list(items)
    return [it for it in items if item_matches(it, query) is not None]


def highlight_parts(text: str, query: str) -> list[tuple[str, bool]]:
    """把 text 切成 [(片段, 是否命中)]，命中大小写不敏感；空查询 → 单段。

    UI 层据此渲染 ``<mark>``；纯函数便于无头断言。
    """
    q = query.strip()
    if not q:
        return [(text, False)]
    parts: list[tuple[str, bool]] = []
    lower_text, lower_q = text.lower(), q.lower()
    i = 0
    while i < len(text):
        j = lower_text.find(lower_q, i)
        if j < 0:
            parts.append((text[i:], False))
            break
        if j > i:
            parts.append((text[i:j], False))
        parts.append((text[j : j + len(q)], True))
        i = j + len(q)
    return parts


# ---------------------------------------------------------------------------
# 约束展示（U-4/U-5）
# ---------------------------------------------------------------------------


def _fmt_num(v: float | int) -> str:
    """范围端点展示：整数值去掉 .0（纯展示裁量）。"""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def range_text(rng: "RangeConstraint | None") -> str | None:
    """范围约束展示文本（含 unit；单边界合法，§3.4）。None → None。"""
    if rng is None:
        return None
    if rng.min is not None and rng.max is not None:
        text = f"[{_fmt_num(rng.min)}, {_fmt_num(rng.max)}]"
    elif rng.min is not None:
        text = f"≥ {_fmt_num(rng.min)}"
    elif rng.max is not None:
        text = f"≤ {_fmt_num(rng.max)}"
    else:
        text = "（无边界）"
    if rng.unit:
        text = f"{text} {rng.unit}"
    return text


def enum_option_text(cand: "EnumCandidate") -> str:
    """枚举候选展示文本：value +（label）。"""
    text = str(cand.value)
    if cand.label:
        text = f"{text}（{cand.label}）"
    return text


# ---------------------------------------------------------------------------
# 徽标状态计算（U-5/U-8）
# ---------------------------------------------------------------------------


def badge_kinds(item: "ConfigItem") -> list[str]:
    """条目徽标 kind 列表（顺序稳定：readonly > dormant > warning > 推测）。

    U-5：inferred 的 range/enum **一律**带"推测"徽标；U-8：三类状态标志。
    """
    kinds: list[str] = []
    if item.readonly:
        kinds.append("readonly")
    if item.dormant:
        kinds.append("dormant")
    if item.warning:
        kinds.append("warning")
    if item.range is not None and item.range.provenance == PROV_INFERRED:
        kinds.append("inferred_range")
    if any(c.provenance == PROV_INFERRED for c in item.enum_candidates):
        kinds.append("inferred_enum")
    return kinds


# ---------------------------------------------------------------------------
# 诊断展示模型（U-10）
# ---------------------------------------------------------------------------


def diagnostics_level(diags: list["Diagnostic"]) -> str:
    """诊断集 → 侧栏指示级别：'error' | 'warning' | 'ok'（info 不着色，§3.7）。"""
    if any(d.severity == SEV_ERROR for d in diags):
        return "error"
    if any(d.severity == SEV_WARNING for d in diags):
        return "warning"
    return "ok"


def diagnostic_rows(diags: list["Diagnostic"]) -> list[tuple[str, str, str, str | None]]:
    """诊断 → 展示行 (severity, code, message, path)，保持原顺序（U-10）。"""
    return [(d.severity, d.code, d.message, d.path) for d in diags]
