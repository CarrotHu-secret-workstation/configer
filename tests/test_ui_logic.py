"""G1 UI 纯函数层测试（logic.py，无头，不 import nicegui）。

覆盖：readonly 中文映射（U-4）、短名/类型标识（U-3）、分组树构建（U-3）、
搜索过滤与命中高亮（U-3）、范围/枚举展示（U-4）、徽标状态计算（U-5/U-8）、
诊断展示模型（U-10）。
"""

from __future__ import annotations

from pathlib import Path

from configer.model import (
    PROV_DECLARED,
    PROV_INFERRED,
    ConfigDoc,
    ConfigItem,
    Diagnostic,
    EnumCandidate,
    GroupInfo,
    RangeConstraint,
    Section,
)
from configer.ui import logic


def mk_item(path: str, group: str = "g", subgroup: str | None = None,
            type: str | None = "int", desc: tuple = (), **kw) -> ConfigItem:
    return ConfigItem(
        path=path, group=group, subgroup=subgroup, type=type,
        value=1, raw_literal="1",
        description=[Section(label=l, text=t) for l, t in desc],
        **kw,
    )


# ---------------------------------------------------------------------------
# readonly 中文映射（U-4，§3.5）
# ---------------------------------------------------------------------------


def test_readonly_reason_zh_known():
    assert logic.readonly_reason_zh("non_literal") == "表达式赋值"
    assert logic.readonly_reason_zh("container") == "容器（元组/序列）"
    assert logic.readonly_reason_zh("anchor") == "锚点子树"
    assert logic.readonly_reason_zh("non_plain_scalar") == "非普通标量"


def test_readonly_reason_zh_extension_passthrough_and_none():
    # §3.5：适配器可扩展，扩展值原样透传展示
    assert logic.readonly_reason_zh("custom_x") == "custom_x"
    assert logic.readonly_reason_zh(None) is None


# ---------------------------------------------------------------------------
# 短名与类型标识（U-3）
# ---------------------------------------------------------------------------


def test_short_name():
    assert logic.short_name("rl_brain.ball_vel.alpha") == "alpha"
    assert logic.short_name("KICK_POWER_MIN") == "KICK_POWER_MIN"
    assert logic.short_name("a.b") == "b"


def test_type_tag():
    assert logic.type_tag(mk_item("x", type="int")) == "int"
    assert logic.type_tag(mk_item("x", type="bool")) == "bool"
    # type=None 且只读（如 tuple 表）→ '只读'
    assert logic.type_tag(mk_item("x", type=None, readonly=True)) == "只读"
    assert logic.type_tag(mk_item("x", type=None)) is None


# ---------------------------------------------------------------------------
# 分组树（U-3）
# ---------------------------------------------------------------------------


def test_build_group_tree_order_and_subgroups():
    doc = ConfigDoc(
        format="python", path=Path("p.py"),
        items=[
            mk_item("A", group="组1"),
            mk_item("B", group="组1", subgroup="子a"),
            mk_item("C", group="组1", subgroup="子a"),
            mk_item("D", group="组1", subgroup="子b"),
            mk_item("E", group="组2"),
        ],
        groups=[GroupInfo(name="组2", description="d2"),
                GroupInfo(name="组1", description="d1")],
    )
    tree = logic.build_group_tree(doc)
    # 组顺序按 doc.groups 声明序（组2 在前），描述透传
    assert [g.name for g in tree] == ["组2", "组1"]
    assert tree[0].description == "d2"
    g1 = tree[1]
    # 组内子组按条目出现顺序：None 直属在前，其后 子a、子b
    assert [sg.name for sg in g1.subgroups] == [None, "子a", "子b"]
    assert [it.path for it in g1.subgroups[1].items] == ["B", "C"]
    assert g1.item_count == 4
    assert [it.path for it in g1.all_items()] == ["A", "B", "C", "D"]


def test_build_group_tree_undeclared_group_appended():
    doc = ConfigDoc(
        format="yaml", path=Path("c.yaml"),
        items=[mk_item("x", group="声明组"), mk_item("y", group="野组")],
        groups=[GroupInfo(name="声明组", description=None)],
    )
    tree = logic.build_group_tree(doc)
    assert [g.name for g in tree] == ["声明组", "野组"]
    assert tree[1].description is None


# ---------------------------------------------------------------------------
# 搜索过滤与高亮（U-3）
# ---------------------------------------------------------------------------


def test_item_matches_path_and_description():
    it = mk_item("game.player_id", desc=(("作用", "球员号码"), (None, "首发用")))
    assert logic.item_matches(it, "player") == "path"
    assert logic.item_matches(it, "PLAYER") == "path"       # 大小写不敏感
    assert logic.item_matches(it, "球员") == "description"
    assert logic.item_matches(it, "作用") == "description"   # label 也参与
    assert logic.item_matches(it, "不存在") is None
    assert logic.item_matches(it, "   ") is None             # 空查询不过滤


def test_filter_items_preserves_order():
    items = [mk_item("a.b"), mk_item("c.d"), mk_item("a.e")]
    assert [i.path for i in logic.filter_items(items, "a.")] == ["a.b", "a.e"]
    assert logic.filter_items(items, "") == items            # 原样
    assert logic.filter_items(items, "zz") == []


def test_highlight_parts():
    assert logic.highlight_parts("player_id", "player") == [("player", True), ("_id", False)]
    assert logic.highlight_parts("Player_ID", "player") == [("Player", True), ("_ID", False)]
    assert logic.highlight_parts("abc", "x") == [("abc", False)]
    assert logic.highlight_parts("abc", "") == [("abc", False)]
    # 多次命中 + 相邻命中
    assert logic.highlight_parts("aXbXc", "X") == [
        ("a", False), ("X", True), ("b", False), ("X", True), ("c", False)]
    assert logic.highlight_parts("XX", "X") == [("X", True), ("X", True)]


# ---------------------------------------------------------------------------
# 范围/枚举展示（U-4/U-5）
# ---------------------------------------------------------------------------


def test_range_text():
    assert logic.range_text(None) is None
    assert logic.range_text(RangeConstraint(min=1.0, max=2.0)) == "[1, 2]"
    assert logic.range_text(RangeConstraint(min=0.05, max=0.30)) == "[0.05, 0.3]"
    assert logic.range_text(RangeConstraint(min=5, max=50, unit="牛")) == "[5, 50] 牛"
    assert logic.range_text(RangeConstraint(min=1.5)) == "≥ 1.5"
    assert logic.range_text(RangeConstraint(max=3, unit="米")) == "≤ 3 米"
    assert logic.range_text(RangeConstraint()) == "（无边界）"


def test_enum_option_text():
    assert logic.enum_option_text(EnumCandidate(value=1)) == "1"
    assert logic.enum_option_text(EnumCandidate(value="kid_size", label="儿童")) == "kid_size（儿童）"


# ---------------------------------------------------------------------------
# 徽标状态计算（U-5/U-8）
# ---------------------------------------------------------------------------


def test_badge_kinds_full_order():
    it = mk_item(
        "x", readonly=True, dormant=True, warning=True,
        range=RangeConstraint(min=0, max=1, provenance=PROV_INFERRED),
        enum_candidates=[EnumCandidate(value=1, provenance=PROV_INFERRED)],
    )
    assert logic.badge_kinds(it) == [
        "readonly", "dormant", "warning", "inferred_range", "inferred_enum"]


def test_badge_kinds_declared_no_inferred_badge():
    # C-1/I-7：只有 inferred 才带"推测"徽标
    it = mk_item(
        "x",
        range=RangeConstraint(min=0, max=1, provenance=PROV_DECLARED),
        enum_candidates=[EnumCandidate(value=1, provenance=PROV_DECLARED)],
    )
    assert logic.badge_kinds(it) == []
    assert logic.BADGE_TEXT["inferred_range"] == "推测范围"
    assert "推测" in logic.BADGE_TEXT["inferred_enum"]


def test_badge_kinds_plain_item_empty():
    assert logic.badge_kinds(mk_item("x")) == []


# ---------------------------------------------------------------------------
# 诊断展示模型（U-10）
# ---------------------------------------------------------------------------


def _d(sev: str, code: str = "C", msg: str = "m", path: str | None = None):
    return Diagnostic(severity=sev, code=code, message=msg, path=path)


def test_diagnostics_level():
    assert logic.diagnostics_level([]) == "ok"
    assert logic.diagnostics_level([_d("info")]) == "ok"        # info 不着色
    assert logic.diagnostics_level([_d("warning")]) == "warning"
    assert logic.diagnostics_level([_d("warning"), _d("error")]) == "error"


def test_diagnostic_rows():
    diags = [_d("error", "E-PARSE", "解析失败", "a.b"), _d("warning", "W-INFER-CONFLICT", "冲突")]
    assert logic.diagnostic_rows(diags) == [
        ("error", "E-PARSE", "解析失败", "a.b"),
        ("warning", "W-INFER-CONFLICT", "冲突", None),
    ]
