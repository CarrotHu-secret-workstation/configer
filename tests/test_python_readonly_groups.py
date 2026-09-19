"""Python 适配器 chunk A 验收：只读条目（R-4/R-5/异体数值）+ P-3 文档描述
+ P-4 分组 / P-5 子组（规范 §3.5/§3.6/§4.3/§5.2，验收基准 §11 A-1）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from configer.adapters.python_adapter import PythonAdapter
from configer.model import READONLY_CONTAINER, READONLY_NON_LITERAL

ROOT = Path(__file__).resolve().parents[1]
PARAM = ROOT / "testdata" / "param.py"

EXPECTED_GROUPS = [
    "踢球力度",
    "Player 走位控制",
    "Player 踢球 / 射门规划",
    "Player 技术动作参数：守门 / 支援",
    "Normal 阶段策略",
    "防守 / 回防(chaser 轨迹选人 + support 封堵 + guard 出击)",
    "开球 / 定位球策略",
    "站位 / 避让",
    "踢球目标几何",
    "障碍物几何参数",
    "全局路径规划器 (A* Grid Planner)",
    "局部路径规划器 (VFH Direction Scan)",
    "可视化",
]

NO_OVERVIEW_GROUPS = ["Normal 阶段策略", "开球 / 定位球策略", "站位 / 避让", "可视化"]

EXPECTED_READONLY_CONTAINER = [
    "KICK_POWER_BY_X",
    "KICK_POWER_BY_X_NARROW",
    "KICK_POWER_BY_X_WIDE",
    "READY_OUR_KICKOFF_SLOT1_XY",
    "READY_OUR_KICKOFF_SLOT2_XY",
    "READY_OUR_KICKOFF_SLOT3_XY",
    "READY_OPP_KICKOFF_SLOT1_XY",
    "READY_OPP_KICKOFF_SLOT2_XY",
    "READY_OPP_KICKOFF_SLOT3_XY",
    "CORNER_KICK_AIM_XY",
    "CORNER_KICK_SUPPORTER_XY",
    "THROW_IN_INWARD_AIM_XY",
]

EXPECTED_READONLY_NON_LITERAL = ["PLAN_STEP", "PLAN_MAX_OFFSET"]

EXPECTED_SUBGROUPS = [
    "守门员门线横向移动(封堵超远射门:按球速射线交点预判)",
    "READY 阶段站位（_act_ready 用）",
    "中场开球扫描(Centre Kickoff)",
    "对方定位球分层防守站位（防乌龙）",
    "定位球第一脚瞄准点：指向场内，避免把球直接踢出场外（底线/边线），白白浪费进攻。",
    "我方角球接应者站位",
    "我方边线球组织进攻",
]


@pytest.fixture(scope="module")
def gold():
    adapter = PythonAdapter()
    raw = PARAM.read_bytes()
    doc, diags = adapter.load(PARAM)
    return adapter, doc, diags, raw


def _by_path(doc, name):
    return next(i for i in doc.items if i.path == name)


# ----------------------------------------------------------------------
# A-1 计数
# ----------------------------------------------------------------------
def test_a1_counts(gold):
    _adapter, doc, diags, _raw = gold
    assert not [d for d in diags if d.severity == "error"]
    editable = [i for i in doc.items if not i.readonly]
    readonly = [i for i in doc.items if i.readonly]
    assert len(editable) == 101
    assert len(readonly) == 14
    assert len(doc.items) == 115


def test_a1_readonly_names_and_reasons(gold):
    _adapter, doc, _diags, _raw = gold
    ro = {i.path: i.readonly_reason for i in doc.items if i.readonly}
    assert len(ro) == 14
    for name in EXPECTED_READONLY_CONTAINER:
        assert ro[name] == READONLY_CONTAINER, name
    for name in EXPECTED_READONLY_NON_LITERAL:
        assert ro[name] == READONLY_NON_LITERAL, name


def test_a1_groups(gold):
    _adapter, doc, _diags, _raw = gold
    assert [g.name for g in doc.groups] == EXPECTED_GROUPS


def test_a1_subgroups(gold):
    _adapter, doc, _diags, _raw = gold
    subs = []
    for i in doc.items:
        if i.subgroup and i.subgroup not in subs:
            subs.append(i.subgroup)
    assert sorted(subs) == sorted(EXPECTED_SUBGROUPS)
    assert len(set(subs)) == 7


def test_a1_doc_description(gold):
    _adapter, doc, _diags, _raw = gold
    assert doc.description
    assert "集中调参入口" in doc.description


# ----------------------------------------------------------------------
# 组描述（本节总览）
# ----------------------------------------------------------------------
def test_group_descriptions(gold):
    _adapter, doc, _diags, _raw = gold
    by_name = {g.name: g.description for g in doc.groups}
    assert by_name["踢球力度"]
    assert by_name["踢球力度"].startswith("踢球力度统一经")
    assert by_name["踢球目标几何"]
    assert "plan_kick" in by_name["踢球目标几何"]
    # 多行总览拼接（防守 / 回防组 L283-284）
    assert "\n" in by_name["防守 / 回防(chaser 轨迹选人 + support 封堵 + guard 出击)"]
    for name in NO_OVERVIEW_GROUPS:
        assert by_name[name] is None, name


# ----------------------------------------------------------------------
# 条目归属与只读字段
# ----------------------------------------------------------------------
def test_item_group_attribution(gold):
    _adapter, doc, _diags, _raw = gold
    assert _by_path(doc, "KICK_POWER_MIN").group == "踢球力度"
    assert _by_path(doc, "KICK_POWER_MIN").subgroup is None
    assert _by_path(doc, "PLAN_STEP").group == "局部路径规划器 (VFH Direction Scan)"
    # 只读条目照常归属组/子组
    slot1 = _by_path(doc, "READY_OUR_KICKOFF_SLOT1_XY")
    assert slot1.group == "开球 / 定位球策略"
    assert slot1.subgroup == "READY 阶段站位（_act_ready 用）"
    centre = _by_path(doc, "CENTRE_KICKOFF_ANGLE_START_DEG")
    assert centre.group == "开球 / 定位球策略"
    assert centre.subgroup == "中场开球扫描(Centre Kickoff)"
    # 守门员门线子组内的条目全部属于 Player 技术动作参数组
    goalie_sub = "守门员门线横向移动(封堵超远射门:按球速射线交点预判)"
    in_sub = [i for i in doc.items if i.subgroup == goalie_sub]
    assert in_sub
    assert all(i.group == "Player 技术动作参数：守门 / 支援" for i in in_sub)


def test_container_items_fields(gold):
    _adapter, doc, _diags, _raw = gold
    for name in EXPECTED_READONLY_CONTAINER:
        it = _by_path(doc, name)
        assert it.readonly and it.readonly_reason == READONLY_CONTAINER
        assert it.type is None
        assert it.value is None
        assert it.locator is not None
        assert it.raw_literal  # 全文保留
    # 元组内空格逐字符保留（§4.3 风险表）
    assert _by_path(doc, "READY_OUR_KICKOFF_SLOT1_XY").raw_literal == "(-0.15, 1.4)"
    assert _by_path(doc, "READY_OUR_KICKOFF_SLOT3_XY").raw_literal == "(-4.0,-1.5)"
    # 多行 AnnAssign 力度表：全文逐字符
    table = _by_path(doc, "KICK_POWER_BY_X").raw_literal
    assert table.startswith("(") and table.endswith(")")
    assert "\n" in table


def test_non_literal_items_fields(gold):
    _adapter, doc, _diags, _raw = gold
    step = _by_path(doc, "PLAN_STEP")
    assert step.readonly and step.readonly_reason == READONLY_NON_LITERAL
    assert step.raw_literal == "math.radians(15)"
    assert step.type is None and step.value is None
    off = _by_path(doc, "PLAN_MAX_OFFSET")
    assert off.readonly and off.raw_literal == "math.radians(100)"


def test_p9_no_profile_on_readonly(gold):
    _adapter, doc, _diags, _raw = gold
    for it in doc.items:
        if it.readonly:
            assert it.range is None, it.path
            assert it.enum_candidates == [], it.path


def test_editable_scalar_fields(gold):
    _adapter, doc, _diags, _raw = gold
    it = _by_path(doc, "KICK_POWER_MIN")
    assert (it.type, it.value, it.raw_literal) == ("float", 1.0, "1.0")
    assert it.literal_style.float_form == ("fixed", 1)
    it = _by_path(doc, "X_OUR_KICKOFF_TARGET")
    assert (it.type, it.value, it.raw_literal) == ("int", 10000, "10000")
    it = _by_path(doc, "GUARD_FACE_BALL")
    assert (it.type, it.value, it.raw_literal) == ("bool", True, "True")
    it = _by_path(doc, "GUARD_HOME_X")
    assert (it.type, it.value, it.raw_literal) == ("float", -6.5, "-6.5")
    assert it.literal_style.float_form == "plain"


# ----------------------------------------------------------------------
# 恒等往返不回归（T1）
# ----------------------------------------------------------------------
def test_identity_roundtrip(gold):
    adapter, doc, _diags, raw = gold
    assert adapter.save(doc, []) == raw


# ----------------------------------------------------------------------
# 合成用例
# ----------------------------------------------------------------------
def _load_text(tmp_path: Path, text: str, name: str = "synth.py"):
    p = tmp_path / name
    p.write_bytes(text.encode("utf-8"))
    adapter = PythonAdapter()
    doc, diags = adapter.load(p)
    assert not [d for d in diags if d.severity == "error"]
    return adapter, doc


def test_synth_no_group_headers_is_ungrouped(tmp_path):
    _adapter, doc = _load_text(tmp_path, "A = 1\nB = 2.5\n")
    assert [i.path for i in doc.items] == ["A", "B"]
    assert all(i.group == "（未分组）" for i in doc.items)
    assert all(i.subgroup is None for i in doc.items)
    assert [g.name for g in doc.groups] == ["（未分组）"]


def test_synth_items_before_first_header(tmp_path):
    text = (
        "Z = 0\n"
        "\n"
        "# ==========\n"
        "# 组一\n"
        "# ==========\n"
        "A = 1\n"
    )
    _adapter, doc = _load_text(tmp_path, text)
    assert _by_path(doc, "Z").group == "（未分组）"
    assert _by_path(doc, "A").group == "组一"
    assert [g.name for g in doc.groups] == ["（未分组）", "组一"]


def test_synth_expression_without_comment_block_skipped(tmp_path):
    text = "import math\n\nX = math.radians(15)\nY = A + B\n"
    _adapter, doc = _load_text(tmp_path, text)
    assert doc.items == []
    assert doc.groups == []


def test_synth_expression_with_comment_block_readonly(tmp_path):
    text = (
        "import math\n"
        "\n"
        "# 作用: 扫描步长(弧度)。\n"
        "# 建议: 保持 math.radians(15)。\n"
        "X = math.radians(15)\n"
    )
    _adapter, doc = _load_text(tmp_path, text)
    assert len(doc.items) == 1
    it = doc.items[0]
    assert it.path == "X"
    assert it.readonly and it.readonly_reason == READONLY_NON_LITERAL
    assert it.raw_literal == "math.radians(15)"
    assert it.type is None and it.value is None


def test_synth_comment_block_without_labels_skipped(tmp_path):
    text = "import math\n\n# 随便一行说明,无标签。\nX = math.radians(15)\n"
    _adapter, doc = _load_text(tmp_path, text)
    assert doc.items == []


def test_synth_exotic_numeric_readonly(tmp_path):
    text = (
        "A = 1_000\n"
        "B = 0x10\n"
        "C = 1e3\n"
        "D = -2_500\n"
        "E = 100\n"
    )
    _adapter, doc = _load_text(tmp_path, text)
    by = {i.path: i for i in doc.items}
    assert set(by) == {"A", "B", "C", "D", "E"}
    for name in ("A", "B", "C", "D"):
        assert by[name].readonly, name
        assert by[name].readonly_reason == READONLY_NON_LITERAL, name
    assert (by["A"].type, by["A"].value, by["A"].raw_literal) == ("int", 1000, "1_000")
    assert (by["B"].type, by["B"].value) == ("int", 16)
    assert (by["C"].type, by["C"].value) == ("float", 1000.0)
    assert (by["D"].type, by["D"].value) == ("int", -2500)
    assert not by["E"].readonly
    assert (by["E"].type, by["E"].value) == ("int", 100)


def test_synth_prefixed_strings(tmp_path):
    text = (
        "# 作用: 前缀字符串示例。\n"
        'P = f"{A}_x"\n'
        'Q = r"raw"\n'
        'R = b"bytes"\n'
    )
    _adapter, doc = _load_text(tmp_path, text)
    assert [i.path for i in doc.items] == ["P"]
    it = doc.items[0]
    assert it.readonly and it.readonly_reason == READONLY_NON_LITERAL
    assert it.raw_literal == 'f"{A}_x"'


def test_synth_plain_string_editable(tmp_path):
    text = 'S = "abc"\nT = \'def\'\n'
    _adapter, doc = _load_text(tmp_path, text)
    s, t = doc.items
    assert (s.type, s.value, s.raw_literal) == ("str", "abc", '"abc"')
    assert s.literal_style.quote == "double"
    assert (t.type, t.value, t.raw_literal) == ("str", "def", "'def'")
    assert t.literal_style.quote == "single"
    assert not s.readonly and not t.readonly


def test_synth_container_readonly(tmp_path):
    text = (
        "T1 = (1, 2)\n"
        "T2: tuple[float, ...] = (\n"
        "    1.0,\n"
        "    2.0,\n"
        ")\n"
        "L = [1, 2]\n"
        "D = {'a': 1}\n"
        "S = {1, 2}\n"
    )
    _adapter, doc = _load_text(tmp_path, text)
    assert len(doc.items) == 5
    for it in doc.items:
        assert it.readonly and it.readonly_reason == READONLY_CONTAINER, it.path
        assert it.type is None and it.value is None
    by = {i.path: i for i in doc.items}
    assert by["T1"].raw_literal == "(1, 2)"
    assert by["T2"].raw_literal == "(\n    1.0,\n    2.0,\n)"
    assert by["L"].raw_literal == "[1, 2]"


def test_synth_subgroup_without_trailing_dashes(tmp_path):
    text = (
        "# ==========\n"
        "# 组一\n"
        "# ==========\n"
        "# --- 子组标题无结尾\n"
        "A = 1\n"
        "# --- 子组二 ---\n"
        "B = 2\n"
        "C = 3\n"
    )
    _adapter, doc = _load_text(tmp_path, text)
    by = {i.path: i for i in doc.items}
    assert by["A"].subgroup == "子组标题无结尾"
    assert by["B"].subgroup == "子组二"
    assert by["C"].subgroup == "子组二"
    assert all(i.group == "组一" for i in doc.items)


def test_synth_subgroup_header_not_in_comment_block(tmp_path):
    # 子组头行不并入条目注释块：仅子组头（无标签行）→ 表达式不生成条目
    text = (
        "import math\n"
        "\n"
        "# ==========\n"
        "# 组一\n"
        "# ==========\n"
        "# --- 标题 ---\n"
        "X = math.radians(1)\n"
    )
    _adapter, doc = _load_text(tmp_path, text)
    assert doc.items == []
    assert [g.name for g in doc.groups] == ["组一"]


def test_synth_group_overview_multiline(tmp_path):
    text = (
        "# ==========\n"
        "# 我的组\n"
        "# ==========\n"
        "# 本节总览:第一行\n"
        "# 第二行\n"
        "\n"
        "A = 1\n"
        "\n"
        "# ==========\n"
        "# 无总览组\n"
        "# ==========\n"
        "\n"
        "B = 2\n"
    )
    _adapter, doc = _load_text(tmp_path, text)
    assert [g.name for g in doc.groups] == ["我的组", "无总览组"]
    assert doc.groups[0].description == "第一行\n第二行"
    assert doc.groups[1].description is None
    assert _by_path(doc, "A").group == "我的组"
    assert _by_path(doc, "B").group == "无总览组"


def test_synth_empty_file(tmp_path):
    _adapter, doc = _load_text(tmp_path, "")
    assert doc.items == []
    assert doc.groups == []
    assert doc.description is None
