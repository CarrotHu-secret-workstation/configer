"""Python 适配器 chunk B 验收：注释画像 P-1/P-2/P-6/P-7/P-8/P-9/P-10 +
W-INFER-CONFLICT + R-6 E-DUP-ASSIGN + R-2 AnnAssign 注解（规范 §5.2/§4.3/
§3.5/§3.7，验收基准 §11 A-3/A-7/A-8）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from configer.adapters.python_adapter import (
    CODE_W_ANN_CONFLICT,
    PythonAdapter,
    _infer_conflict_diags,
)
from configer.model import (
    CODE_E_DUP_ASSIGN,
    CODE_W_INFER_CONFLICT,
    PROV_INFERRED,
    SEV_ERROR,
    SEV_WARNING,
    ConfigItem,
    EnumCandidate,
    RangeConstraint,
)

ROOT = Path(__file__).resolve().parents[1]
PARAM = ROOT / "testdata" / "param.py"


@pytest.fixture(scope="module")
def gold():
    adapter = PythonAdapter()
    doc, diags = adapter.load(PARAM)
    by_path = {it.path: it for it in doc.items}
    return adapter, doc, diags, by_path


def _load_synthetic(tmp_path: Path, text: str, name: str = "syn.py"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    adapter = PythonAdapter()
    doc, diags = adapter.load(p)
    return adapter, doc, diags, {it.path: it for it in doc.items}


# ======================================================================
# P-2 说明分段（金样本）
# ======================================================================

def test_p2_kick_power_min_three_sections(gold):
    _a, _doc, _diags, by = gold
    it = by["KICK_POWER_MIN"]
    assert [s.label for s in it.description] == ["作用", "影响", "建议"]
    assert it.description_provenance == "comment"
    assert it.description[0].text == (
        "kick() 力度下限夹取(无量纲),低于该值的力度被抬高到该值。"
    )
    adv = it.description[2].text
    # 缩进续行并入建议段：'\n' 连接，且含"越界抛 ValueError;"证据
    assert adv.startswith("[1.0, 2.0]。SDK SoccerKickManager.update_command")
    assert "\n" in adv
    assert "越界抛 ValueError;" in adv
    assert "故取 1.0 对齐 SDK 下限,此修正不改变当前行为。" in adv


def test_p2_leading_section_golden(gold):
    """param.py L341 `# 开球` 短注释与下方 作用/影响/建议 同块 → 前导段。"""
    _a, _doc, _diags, by = gold
    it = by["KICKOFF_FRONT_MARGIN"]
    assert [s.label for s in it.description] == [None, "作用", "影响", "建议"]
    assert it.description[0].text == "开球"


def test_p2_readonly_description_discretion(gold):
    """裁量：只读条目同样填 description（P-9 只禁推测，不禁说明段）。"""
    _a, _doc, _diags, by = gold
    it = by["KICK_POWER_BY_X"]
    assert it.readonly
    assert [s.label for s in it.description] == ["作用", "影响", "建议", None]
    assert it.description_provenance == "comment"
    # 无标签的"表格式:"等行合并为同一个 label=None 段
    assert "表格式:" in it.description[3].text
    assert "\n" in it.description[3].text


def test_p2_unlabelled_after_label_golden(gold):
    """Y_OUR_KICKOFF_TARGET：`!!` 行 → 前导段；`实测效果：` 行 → 尾部 None 段。"""
    _a, _doc, _diags, by = gold
    it = by["Y_OUR_KICKOFF_TARGET"]
    assert [s.label for s in it.description] == [None, "作用", "影响", "建议", None]
    assert it.description[0].text.startswith("# !!".lstrip("# "))  # "!! 哨兵伪方向…"
    assert it.description[-1].text == "实测效果：左门柱2300～750中间～右门柱"


# ======================================================================
# A-3 休眠 / 警告（金样本）
# ======================================================================

CENTRE_KICKOFF_8 = [
    "CENTRE_KICKOFF_ANGLE_START_DEG",
    "CENTRE_KICKOFF_ANGLE_END_DEG",
    "CENTRE_KICKOFF_ANGLE_STEP_DEG",
    "CENTRE_KICKOFF_CORRIDOR_HALF_WIDTH_M",
    "CENTRE_KICKOFF_OPP_X_MIN_M",
    "CENTRE_KICKOFF_OPP_X_MAX_M",
    "CENTRE_KICKOFF_RAY_LENGTH_M",
    "CENTRE_KICKOFF_BALL_DRIFT_M",
]

WARNING_3 = ["X_STRAIGHT_M", "X_OUR_KICKOFF_TARGET", "Y_OUR_KICKOFF_TARGET"]


def test_a3_dormant_exactly_8(gold):
    _a, doc, _diags, _by = gold
    dormant = [it.path for it in doc.items if it.dormant]
    assert set(dormant) == set(CENTRE_KICKOFF_8)
    assert len(dormant) == 8
    for it in doc.items:
        if it.dormant:
            assert it.dormant_reason_text.startswith("# 休眠配置:")
            assert "不生效" in it.dormant_reason_text
        else:
            assert it.dormant_reason_text is None


def test_a3_warning_exactly_3(gold):
    _a, doc, _diags, by = gold
    warned = [it.path for it in doc.items if it.warning]
    assert set(warned) == set(WARNING_3)
    assert len(warned) == 3
    assert by["X_STRAIGHT_M"].warning_reason_text == "# !! 哨兵值,非场地坐标 !!"
    for n in ("X_OUR_KICKOFF_TARGET", "Y_OUR_KICKOFF_TARGET"):
        assert by[n].warning_reason_text.startswith(
            "# !! 哨兵伪方向向量分量,不是场地坐标 !!"
        )


# ======================================================================
# A-7 范围推测（金样本）
# ======================================================================

def test_a7_range_positives(gold):
    _a, _doc, _diags, by = gold
    expected = {
        "KICK_POWER_MIN": (1.0, 2.0, None),
        "ARRIVE_DIST": (0.05, 0.30, "米"),
        "MAX_LINEAR": (1.0, 2.0, "米/秒"),
        "GOALIE_MAX_Y_OFFSET": (0.5, 1.3, "米"),
        "FALLEN_COST": (5.0, 50.0, None),
    }
    for name, (lo, hi, unit) in expected.items():
        rc = by[name].range
        assert rc is not None, name
        assert (rc.min, rc.max, rc.unit) == (lo, hi, unit), name
        assert rc.provenance == PROV_INFERRED, name


def test_a7_range_negatives(gold):
    """闸门 (a) 增量式与闸门 (b) ± 对称带不得产生范围。"""
    _a, _doc, _diags, by = gold
    assert by["KICK_EXIT_M"].range is None          # KICK_ENTER_M + [0.2, 0.6]
    assert by["KICK_ENTER_OUR_KICKOFF_M"] is not None
    assert by["KICK_EXIT_OUR_KICKOFF_M"].range is None  # 同为 `+ [...]` 增量式
    assert by["CENTRE_KICKOFF_ANGLE_STEP_DEG"].range is None  # ±[1, 5]
    # ∈ 闸门（裁量）：X_OUR_KICKOFF_TARGET 建议段续行含 x∈[0, 7]、y∈[-4.5, 4.5]
    assert by["X_OUR_KICKOFF_TARGET"].range is None


def test_a7_range_total(gold):
    """实测总数锁定（2026-09-08 核对值；param.py 演进时须同步修订）。"""
    _a, doc, _diags, _by = gold
    assert sum(1 for it in doc.items if it.range is not None) == 85


def test_p6_unit_extraction_golden(gold):
    _a, _doc, _diags, by = gold
    assert by["GLOBAL_GRID_RESOLUTION_M"].range.unit == "米/格"
    assert by["LINEAR_GAIN"].range.unit == "1/秒"
    assert by["MAX_ANGULAR"].range.unit == "弧度/秒"
    # `[2.5, 5.0],须 ≥ SDK 下限 1.0。` → `]` 后立即句读符 `,` → unit=None
    assert by["THROW_IN_KICK_POWER_LONG"].range.unit is None
    # `[5°, 20°]` 非 num 模式且 PLAN_STEP 只读（P-9 双重排除）
    assert by["PLAN_STEP"].range is None


def test_p9_readonly_no_constraints(gold):
    """P-9：只读条目（含注释带 power∈[1.0, 10.0] 的 KICK_POWER_BY_X）不推测。"""
    _a, doc, _diags, by = gold
    for it in doc.items:
        if it.readonly:
            assert it.range is None, it.path
            assert it.enum_candidates == [], it.path
    assert "power∈[1.0, 10.0]" in by["KICK_POWER_BY_X"].description[2].text


# ======================================================================
# A-8 / P-10（金样本）
# ======================================================================

def test_a8_conflicts_exactly_2(gold):
    _a, doc, diags, _by = gold
    conflicts = [d for d in diags if d.code == CODE_W_INFER_CONFLICT]
    assert len(conflicts) == 2
    assert {d.path for d in conflicts} == {
        "THROW_IN_ADVANCE_MIN_M",
        "CORNER_KICK_POWER",
    }
    for d in conflicts:
        assert d.severity == SEV_WARNING
        # path 不变量：指向已存在条目
        assert any(it.path == d.path for it in doc.items)
    msg = {d.path: d.message for d in conflicts}
    assert "-2" in msg["THROW_IN_ADVANCE_MIN_M"] and "[1.5, 3]" in msg[
        "THROW_IN_ADVANCE_MIN_M"
    ]
    assert "1.6" in msg["CORNER_KICK_POWER"] and "[3, 8]" in msg["CORNER_KICK_POWER"]


def test_a8_p10_kick_enter_clean(gold):
    """P-10/A-8 负向：KICK_ENTER_M（1.4，注释'保持 1.2'）不得产生任何诊断。"""
    _a, _doc, diags, by = gold
    assert not [d for d in diags if d.path == "KICK_ENTER_M"]
    it = by["KICK_ENTER_M"]
    assert (it.range.min, it.range.max) == (1.0, 2.0)  # 建议: [1.0, 2.0] 米
    assert it.value == 1.4  # "保持 1.2" 未被解析为约束/默认值
    assert not it.warning and not it.dormant


def test_golden_diag_totals(gold):
    """金样本加载期诊断恰为 2 条 W-INFER-CONFLICT：无 E-DUP-ASSIGN（param.py
    无重复赋值）、无 W-ANN-CONFLICT（3 个 tuple[...] 注解元组表不得误报）。"""
    _a, _doc, diags, _by = gold
    assert len(diags) == 2
    assert all(d.code == CODE_W_INFER_CONFLICT for d in diags)


def test_r2_annotation_display_golden(gold):
    """裁量登记：注解文本携带于条目私有动态属性 annotation_text（仅展示）。"""
    _a, _doc, _diags, by = gold
    for n in ("KICK_POWER_BY_X", "KICK_POWER_BY_X_NARROW", "KICK_POWER_BY_X_WIDE"):
        ann = getattr(by[n], "annotation_text", None)
        assert ann is not None and ann.startswith("tuple["), n
    # 普通 Assign 条目无注解属性
    assert getattr(by["KICK_POWER_MIN"], "annotation_text", None) is None


# ======================================================================
# P-2 合成
# ======================================================================

def test_p2_fullwidth_colon_synthetic(tmp_path):
    _a, _d, _diags, by = _load_synthetic(tmp_path, (
        "# 作用：全角冒号同样接受。\n"
        "# 建议：[1.0, 2.0] 米。\n"
        "FULL_WIDTH = 1.5\n"
    ))
    it = by["FULL_WIDTH"]
    assert [s.label for s in it.description] == ["作用", "建议"]
    assert it.description[0].text == "全角冒号同样接受。"
    assert (it.range.min, it.range.max, it.range.unit) == (1.0, 2.0, "米")


def test_p2_indent_merge_synthetic(tmp_path):
    """≥2 空格缩进续行并入上一段；仅 1 空格 → 非续行（无标签行开新段）。"""
    _a, _d, _diags, by = _load_synthetic(tmp_path, (
        "# 作用: 首行\n"
        "#   缩进续行(3 空格#后→2 空格内容)\n"
        "# 单空格行\n"
        "IND = 1\n"
    ))
    it = by["IND"]
    assert [s.label for s in it.description] == ["作用", None]
    assert it.description[0].text == "首行\n  缩进续行(3 空格#后→2 空格内容)"
    assert it.description[1].text == "单空格行"


def test_p2_leading_multi_synthetic(tmp_path):
    """相邻无标签行并入同一前导段（label=None，'\\n' 连接）。"""
    _a, _d, _diags, by = _load_synthetic(tmp_path, (
        "# 前导第一行\n"
        "# 前导第二行\n"
        "# 作用: 正文\n"
        "LEAD = 2\n"
    ))
    it = by["LEAD"]
    assert [s.label for s in it.description] == [None, "作用"]
    assert it.description[0].text == "前导第一行\n前导第二行"


def test_p2_blank_line_breaks_block_synthetic(tmp_path):
    """P-1：注释与赋值之间有空行 → 上方注释不归属该条目。"""
    _a, _d, _diags, by = _load_synthetic(tmp_path, (
        "# 作用: 悬空注释\n"
        "\n"
        "DETACHED = 3\n"
    ))
    assert by["DETACHED"].description == []
    assert by["DETACHED"].description_provenance == "none"


def test_overview_not_misattributed_synthetic(tmp_path):
    """组头后总览段与首条目注释无空行分隔时，总览不得误归首条目（硬边界）。"""
    _a, doc, _diags, by = _load_synthetic(tmp_path, (
        "# ==========\n"
        "# 组名\n"
        "# ==========\n"
        "# 本节总览: 组描述文本\n"
        "# 作用: 条目自己的说明\n"
        "FIRST = 1\n"
    ))
    assert doc.groups[0].name == "组名"
    assert doc.groups[0].description == "组描述文本"
    it = by["FIRST"]
    assert [s.label for s in it.description] == ["作用"]
    assert it.description[0].text == "条目自己的说明"


# ======================================================================
# P-6 合成（闸门 / unit / 多命中）
# ======================================================================

def test_p6_gate_a_synthetic(tmp_path):
    """闸门 (a)：`[` 前紧邻（忽略空白）+ - * × / → 增量表达式，不产生范围。"""
    src = []
    for i, op in enumerate(["+", "-", "*", "×", "/"]):
        src.append(f"# 建议: BASE {op} [0.2, 0.6] 米。\nGATE_A{i} = 1.0\n")
    src.append("# 建议: BASE+[1, 2]。\nGATE_A5 = 1.0\n")  # 无空白紧邻
    _a, _d, _diags, by = _load_synthetic(tmp_path, "".join(src))
    for i in range(6):
        assert by[f"GATE_A{i}"].range is None, i


def test_p6_gate_b_synthetic(tmp_path):
    _a, _d, _diags, by = _load_synthetic(tmp_path, (
        "# 建议: ±[1, 5] 度。\nGATE_B = -1.0\n"
        "# 建议: ± [2, 6] 度。\nGATE_B2 = -3.0\n"  # ± 与 [ 间有空白
    ))
    assert by["GATE_B"].range is None
    assert by["GATE_B2"].range is None


def test_p6_membership_gate_synthetic(tmp_path):
    """∈ 闸门（裁量追加）：归属描述不产生范围。"""
    _a, _d, _diags, by = _load_synthetic(tmp_path, (
        "# 建议: 沿用哨兵用法;改真实场内点须同时改\n"
        "#   (x∈[0, 7]、y∈[-4.5, 4.5])。切勿单独修改。\n"
        "SENTINEL = 10000\n"
    ))
    assert by["SENTINEL"].range is None


def test_p6_first_hit_synthetic(tmp_path):
    """多命中取第一个；被闸门排除的命中继续向后找。"""
    _a, _d, _diags, by = _load_synthetic(tmp_path, (
        "# 建议: [1, 2];后文还有 [3, 4]。\nFIRST_HIT = 1.5\n"
        "# 建议: ±[1, 2] 度、[3.0, 4.0] 米。\nAFTER_GATE = 3.5\n"
    ))
    rc = by["FIRST_HIT"].range
    assert (rc.min, rc.max, rc.unit) == (1.0, 2.0, None)
    rc = by["AFTER_GATE"].range
    assert (rc.min, rc.max, rc.unit) == (3.0, 4.0, "米")


def test_p6_units_synthetic(tmp_path):
    _a, _d, _diags, by = _load_synthetic(tmp_path, (
        "# 建议: [1.0, 2.0] 米/秒。\nU1 = 1.5\n"
        "# 建议: [0.05, 0.20] 米/格。\nU2 = 0.1\n"
        "# 建议: [0.8, 2.5] 1/秒。\nU3 = 1.5\n"
        "# 建议: [5, 50]。\nU4 = 20.0\n"
        "# 建议: [1, 3] 米,注意上限。\nU5 = 2\n"
        "# 建议: [1, 3] 秒；后半句。\nU6 = 2\n"
    ))
    assert by["U1"].range.unit == "米/秒"
    assert by["U2"].range.unit == "米/格"
    assert by["U3"].range.unit == "1/秒"
    assert by["U4"].range.unit is None
    assert by["U5"].range.unit == "米"   # 半角逗号截断
    assert by["U6"].range.unit == "秒"   # 全角分号截断


def test_p6_negative_numbers_synthetic(tmp_path):
    _a, _d, _diags, by = _load_synthetic(tmp_path, (
        "# 建议: [-6.9, -6.0] 米。\nNEG = -6.5\n"
    ))
    rc = by["NEG"].range
    assert (rc.min, rc.max) == (-6.9, -6.0)
    assert rc.provenance == PROV_INFERRED


def test_p6_only_advice_section_synthetic(tmp_path):
    """范围只在「建议」段搜索：作用/影响/前导段中的 [a, b] 不产生约束。"""
    _a, _d, _diags, by = _load_synthetic(tmp_path, (
        "# 作用: 表查询区间 [0, 9] 说明。\n"
        "# 影响: 与 [1, 2] 相关。\n"
        "NOT_ADVICE = 5\n"
        "# 作用: x。\n"
        "# 建议: [1.5, 3.0] 米。\nADVICE = 2.0\n"
    ))
    assert by["NOT_ADVICE"].range is None
    assert (by["ADVICE"].range.min, by["ADVICE"].range.max) == (1.5, 3.0)


# ======================================================================
# P-7 / P-8 合成
# ======================================================================

def test_p7_dormant_synthetic(tmp_path):
    _a, _d, _diags, by = _load_synthetic(tmp_path, (
        "# 休眠配置: 当前不生效,调用点已注释。\n"
        "# 作用: 角度。\nDORM = -25.0\n"
        "# 休眠配置：全角冒号（裁量兼容）。\nDORM2 = 1\n"
        "# 非休眠配置: 行首不是该标签。\nNOT_DORM = 2\n"
        "# 【本小节均为休眠配置】括号开头不算。\nNOT_DORM2 = 3\n"
    ))
    assert by["DORM"].dormant
    assert by["DORM"].dormant_reason_text == "# 休眠配置: 当前不生效,调用点已注释。"
    assert by["DORM2"].dormant  # 全角冒号（裁量）
    assert not by["NOT_DORM"].dormant and not by["NOT_DORM2"].dormant


def test_p8_warning_synthetic(tmp_path):
    _a, _d, _diags, by = _load_synthetic(tmp_path, (
        "# !! 哨兵值 !!\nW2 = 10000\n"
        "# !!!! 正式比赛必须改回 !!!!\nW4 = False\n"
        "# 单! 不算警告。\nW_NONE1 = 1\n"
        "# a!!b 前后是字母数字不算。\nW_NONE2 = 2\n"
    ))
    assert by["W2"].warning and by["W2"].warning_reason_text == "# !! 哨兵值 !!"
    assert by["W4"].warning
    assert by["W4"].warning_reason_text == "# !!!! 正式比赛必须改回 !!!!"
    assert not by["W_NONE1"].warning
    assert not by["W_NONE2"].warning


def test_p7_p8_apply_to_readonly_synthetic(tmp_path):
    """裁量登记：P-7/P-8 是标记而非推测，对只读条目同样生效（C-3 通用规则）。"""
    _a, _d, _diags, by = _load_synthetic(tmp_path, (
        "# !! 哨兵表 !!\n"
        "# 休眠配置: 暂不生效。\n"
        "RO_TUPLE = (1.0, 2.0)\n"
    ))
    it = by["RO_TUPLE"]
    assert it.readonly
    assert it.warning and it.dormant
    assert it.range is None  # P-9 仍禁止推测


# ======================================================================
# P-10 合成
# ======================================================================

def test_p10_keep_not_parsed_synthetic(tmp_path):
    _a, _d, diags, by = _load_synthetic(tmp_path, (
        "# 作用: 进入距离。\n"
        "# 建议: 保持 1.2。\nKEEP_F = 1.4\n"
        "# 建议: 保持 True。\nKEEP_B = False\n"
    ))
    assert by["KEEP_F"].range is None
    assert by["KEEP_B"].range is None
    assert diags == []  # 不得产生任何冲突警告/诊断


# ======================================================================
# W-INFER-CONFLICT 合成（range + enum）
# ======================================================================

def test_conflict_range_synthetic(tmp_path):
    _a, _d, diags, _by = _load_synthetic(tmp_path, (
        "# 建议: [1.5, 3.0] 米。\nCONF = -2.0\n"
        "# 建议: [1.0, 2.0] 米。\nOKIN = 1.4\n"
    ))
    assert len(diags) == 1
    d = diags[0]
    assert (d.severity, d.code, d.path) == (
        SEV_WARNING, CODE_W_INFER_CONFLICT, "CONF",
    )
    assert "-2" in d.message and "[1.5, 3]" in d.message


def test_conflict_enum_helper():
    """inferred 枚举违规同理（python v1 不从注释推测枚举，直接测 helper）。"""
    item = ConfigItem(
        path="mode", group="g", type="str", value="c",
        enum_candidates=[
            EnumCandidate(value="a", provenance=PROV_INFERRED),
            EnumCandidate(value="b", provenance=PROV_INFERRED),
        ],
    )
    diags = _infer_conflict_diags(item)
    assert len(diags) == 1
    assert diags[0].code == CODE_W_INFER_CONFLICT and diags[0].path == "mode"
    assert diags[0].severity == SEV_WARNING
    # 值在候选内 → 无诊断；declared 候选不归本 helper（校验属 §8.2 提交期）
    item.value = "a"
    assert _infer_conflict_diags(item) == []
    declared = ConfigItem(
        path="d", group="g", value=9,
        enum_candidates=[EnumCandidate(value=1, provenance="declared")],
    )
    assert _infer_conflict_diags(declared) == []


def test_conflict_helper_one_sided_and_bool():
    """单边界约束只查有界一侧；bool 现值不参与数值范围判定。"""
    item = ConfigItem(
        path="x", group="g", type="float", value=0.5,
        range=RangeConstraint(min=1.0, max=None, provenance=PROV_INFERRED),
    )
    diags = _infer_conflict_diags(item)
    assert len(diags) == 1 and "≥1" in diags[0].message
    b = ConfigItem(
        path="b", group="g", type="bool", value=True,
        range=RangeConstraint(min=0.0, max=0.5, provenance=PROV_INFERRED),
    )
    assert _infer_conflict_diags(b) == []


# ======================================================================
# R-6 E-DUP-ASSIGN 合成
# ======================================================================

def test_r6_dup_assign_synthetic(tmp_path):
    _a, doc, diags, by = _load_synthetic(tmp_path, (
        "A = 1.0\n"
        "A = 2.0\n"                      # 标量重复 → 首个为准
        "B: tuple[float, float] = (1.0, 2.0)\n"
        "B = 5\n"                        # 混合形态：元组首赋 + 标量重复
        "C = foo()\n"
        "C = 3\n"                        # 首赋无条目（R-4 无注释块）→ path=None
        "D = 7\n"
    ))
    dups = [d for d in diags if d.code == CODE_E_DUP_ASSIGN]
    assert len(dups) == 3
    assert all(d.severity == SEV_ERROR for d in dups)
    # I-1：每个 path 只产出一个条目，以首个为准
    assert [it.path for it in doc.items] == ["A", "B", "D"]
    assert by["A"].value == 1.0 and by["A"].raw_literal == "1.0"
    assert by["B"].readonly and by["B"].readonly_reason == "container"
    by_code = {d.path: d for d in dups if d.path}
    assert set(by_code) == {"A", "B"}
    assert "第 2 行" in by_code["A"].message
    assert "第 4 行" in by_code["B"].message
    none_path = [d for d in dups if d.path is None]
    assert len(none_path) == 1 and "C" in none_path[0].message
    # 诊断 path 不变量：非空 path 必须指向已存在条目
    for d in dups:
        assert d.path is None or any(it.path == d.path for it in doc.items)


# ======================================================================
# R-2 AnnAssign 合成
# ======================================================================

def test_r2_ann_conflict_synthetic(tmp_path):
    _a, doc, diags, by = _load_synthetic(tmp_path, (
        "X: int = 1.5\n"                 # 冲突 → warning；type 仍按字面量
        "Y: float = 2\n"                 # 数值塔容忍（裁量）→ 无诊断
        "Z: str = 7\n"                    # 冲突
        "W: float = 1.5\n"               # 一致
        "V: tuple[int, ...] = (1, 2)\n"  # 容器注解不判定
        "U: int = 3\n"                   # 一致
    ))
    ann = [d for d in diags if d.code == CODE_W_ANN_CONFLICT]
    assert {d.path for d in ann} == {"X", "Z"}
    assert all(d.severity == SEV_WARNING for d in ann)
    # type 仍按字面量判定（§8.1）
    assert by["X"].type == "float" and by["X"].value == 1.5
    assert by["Z"].type == "int" and by["Z"].value == 7
    assert by["Y"].type == "int"
    # 注解文本仅展示（裁量：私有动态属性携带）
    assert by["X"].annotation_text == "int"
    assert by["V"].annotation_text == "tuple[int, ...]"
    assert by["V"].readonly and by["V"].range is None
    # 除注解冲突外无其他诊断
    assert [d for d in diags if d.code != CODE_W_ANN_CONFLICT] == []
    assert len(doc.items) == 6
