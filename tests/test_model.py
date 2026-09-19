"""configer.model 单元测试（规范 §3.1/§3.2/§3.6/§3.9）。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from configer.core.checks import check_doc_invariants
from configer.model import (
    CODE_E_ENCODING,
    PROV_COMMENT,
    PROV_INFERRED,
    READONLY_CONTAINER,
    READONLY_NON_LITERAL,
    SEV_ERROR,
    ConfigDoc,
    ConfigItem,
    Diagnostic,
    EditOp,
    EnumCandidate,
    GroupInfo,
    LiteralStyle,
    RangeConstraint,
    Section,
    classify_float_form,
    compute_hash,
    is_exotic_numeric,
)


# ---------------------------------------------------------------------------
# §3.2 classify_float_form：机械判定规则，示例取自规范原文
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("100.", "trailing_dot"),      # §3.2 / §4.4 风险表（step_interval）
        ("0.90", ("fixed", 2)),        # §3.2 示例：'0.90'→2（robot_height）
        ("1.60", ("fixed", 2)),        # §3.2 示例（SETPIECE_DISTANCE_FROM_BALL）
        ("1.0", ("fixed", 1)),         # §3.2 示例：'1.0'→1（KICK_POWER_MIN）
        ("10000.0", ("fixed", 1)),     # §3.2 示例（X_STRAIGHT_M）
        ("0.15", "plain"),             # §3.2 示例
        ("-6.5", "plain"),             # 负 float（GUARD_HOME_X）
        ("-70.0", ("fixed", 1)),       # 负 float 尾零（CENTRE_KICKOFF_ANGLE_END_DEG）
        ("2.05", "plain"),             # 小数末位非 0
    ],
)
def test_classify_float_form(literal: str, expected) -> None:
    assert classify_float_form(literal) == expected


# ---------------------------------------------------------------------------
# §3.2 is_exotic_numeric：下划线/指数/十六进制 → 只读
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("literal", ["1_000", "1e3", "0x10", "1E3", "0XFF", "-1.5e-3", "1_0.0_1"])
def test_is_exotic_numeric_true(literal: str) -> None:
    assert is_exotic_numeric(literal) is True


@pytest.mark.parametrize("literal", ["100.", "-6.5", "0.90", "10000", "1.0", "2300", "0.15"])
def test_is_exotic_numeric_false(literal: str) -> None:
    assert is_exotic_numeric(literal) is False


# ---------------------------------------------------------------------------
# §3.1/§3.6 数据结构构造
# ---------------------------------------------------------------------------

def test_configitem_construction_and_defaults() -> None:
    item = ConfigItem(
        path="KICK_POWER_MIN",
        group="踢球力度",
        type="float",
        value=1.0,
        raw_literal="1.0",
        literal_style=LiteralStyle(float_form=("fixed", 1)),
        description=[
            Section(label="作用", text="kick() 力度下限夹取(无量纲)。"),
            Section(label="建议", text="[1.0, 2.0]。"),
        ],
        description_provenance=PROV_COMMENT,
        range=RangeConstraint(min=1.0, max=2.0, unit=None, provenance=PROV_INFERRED),
    )
    # 默认值
    assert item.subgroup is None
    assert item.enum_candidates == []
    assert item.readonly is False
    assert item.readonly_reason is None
    assert item.dormant is False
    assert item.warning is False
    assert item.warning_reason_text is None
    assert item.dormant_reason_text is None
    assert item.locator is None
    # 显式字段
    assert item.literal_style.float_form == ("fixed", 1)
    assert item.literal_style.bool_case is None
    assert item.literal_style.quote is None
    assert item.literal_style.int_form == "decimal"
    assert item.range is not None and item.range.min == 1.0 and item.range.provenance == PROV_INFERRED
    assert [s.label for s in item.description] == ["作用", "建议"]


def test_configitem_readonly_container() -> None:
    item = ConfigItem(
        path="KICK_POWER_BY_X",
        group="踢球力度",
        type=None,
        value=None,
        raw_literal="(...力度表全文...)",
        readonly=True,
        readonly_reason=READONLY_CONTAINER,
    )
    assert item.readonly and item.readonly_reason == "container"
    # readonly_reason 枚举常量与规范 §3.5/§4.4 一致
    assert READONLY_NON_LITERAL == "non_literal"
    assert READONLY_CONTAINER == "container"


def test_enumcandidate_fields() -> None:
    cand = EnumCandidate(value="kid_size", label=None, provenance=PROV_INFERRED)
    assert cand.value == "kid_size" and cand.provenance == "inferred"


def test_range_single_bound_is_legal() -> None:
    r = RangeConstraint(min=1.0, max=None, unit="米", provenance=PROV_INFERRED)
    assert r.min == 1.0 and r.max is None
    r2 = RangeConstraint(min=None, max=10.0, provenance="declared")
    assert r2.min is None and r2.max == 10.0


def test_groupinfo_and_editop() -> None:
    g = GroupInfo(name="rl_brain", description="RL Brain 说明块")
    assert g.name == "rl_brain" and g.description is not None
    op = EditOp(path="rl_brain.ball_vel.alpha", new_value=0.7)
    assert op.path == "rl_brain.ball_vel.alpha" and op.new_value == 0.7


def test_diagnostic_fields_and_constants() -> None:
    d = Diagnostic(severity=SEV_ERROR, code=CODE_E_ENCODING, message="非 UTF-8", path=None)
    assert d.severity == "error" and d.code == "E-ENCODING" and d.path is None
    assert SEV_ERROR == "error"


# ---------------------------------------------------------------------------
# §3.6 compute_hash / ConfigDoc.refresh_hash
# ---------------------------------------------------------------------------

def test_compute_hash_known_bytes() -> None:
    assert compute_hash(b"abc") == hashlib.sha256(b"abc").hexdigest()
    # 固定已知值（防实现被换成别的算法）
    assert compute_hash(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert compute_hash(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_configdoc_construction_and_refresh_hash() -> None:
    raw = b"KICK_POWER_MIN = 1.0\n"
    doc = ConfigDoc(format="python", path=Path("/tmp/param.py"), original_bytes=raw)
    assert doc.items == [] and doc.groups == [] and doc.diagnostics == []
    assert doc.description is None and doc.content_hash == ""
    h = doc.refresh_hash()
    assert h == compute_hash(raw) == doc.content_hash
    # 提交后基线更新（§7.5）：original_bytes 变 → refresh_hash 跟随
    doc.original_bytes = raw + b"X_OUR_KICKOFF_TARGET = 10000\n"
    doc.refresh_hash()
    assert doc.content_hash == compute_hash(doc.original_bytes) != h


# ---------------------------------------------------------------------------
# §3.9 I-1 / I-2（check_doc_invariants）
# ---------------------------------------------------------------------------

def _doc_with_paths(*paths: str) -> ConfigDoc:
    return ConfigDoc(
        format="yaml",
        path=Path("/tmp/config.yaml"),
        items=[ConfigItem(path=p, group="（根级）") for p in paths],
    )


def test_invariant_i1_unique_paths_pass() -> None:
    doc = _doc_with_paths("a.b", "a.c", "d")
    assert check_doc_invariants(doc) == []


def test_invariant_i1_duplicate_path_reported() -> None:
    doc = _doc_with_paths("a.b", "a.c", "a.b")
    violations = check_doc_invariants(doc)
    assert len(violations) == 1
    assert "I-1" in violations[0]
    assert "a.b" in violations[0]


def test_invariant_i1_multiple_duplicates_all_reported() -> None:
    doc = _doc_with_paths("x", "x", "x", "y", "y")
    violations = check_doc_invariants(doc)
    assert len(violations) == 2
    assert any("x" in v for v in violations)
    assert any("y" in v for v in violations)


def test_invariant_i2_skipped_without_callback() -> None:
    doc = ConfigDoc(
        format="python",
        path=Path("/tmp/p.py"),
        items=[ConfigItem(path="A", group="g", type="float", value=999.0, raw_literal="1.0")],
    )
    # value 与 raw_literal 明显不一致，但未注入 parse_literal → I-2 跳过
    assert check_doc_invariants(doc) == []


def test_invariant_i2_with_callback() -> None:
    def parse_literal(raw: str, type_: str | None):
        assert type_ is not None
        if type_ == "bool":
            return raw == "True"
        return {"int": int, "float": float, "str": str}[type_](raw)

    ok = ConfigDoc(
        format="python",
        path=Path("/tmp/p.py"),
        items=[
            ConfigItem(path="A", group="g", type="float", value=1.0, raw_literal="1.0"),
            ConfigItem(path="B", group="g", type="int", value=10000, raw_literal="10000"),
            # type=None 只读条目（tuple 表）不参与 I-2
            ConfigItem(path="T", group="g", type=None, value=None,
                       raw_literal="(0.0, 1.85)", readonly=True,
                       readonly_reason=READONLY_CONTAINER),
        ],
    )
    assert check_doc_invariants(ok, parse_literal=parse_literal) == []

    bad = ConfigDoc(
        format="python",
        path=Path("/tmp/p.py"),
        items=[ConfigItem(path="A", group="g", type="float", value=2.0, raw_literal="1.0")],
    )
    violations = check_doc_invariants(bad, parse_literal=parse_literal)
    assert len(violations) == 1 and "I-2" in violations[0] and "A" in violations[0]


def test_invariant_i2_strict_type_bool_vs_int() -> None:
    # True == 1 在 Python 中成立；严格判定必须识破 bool/int 混淆
    def parse_literal(raw: str, type_: str | None):
        return raw == "True"  # bool 解析

    doc = ConfigDoc(
        format="yaml",
        path=Path("/tmp/c.yaml"),
        items=[ConfigItem(path="flag", group="（根级）", type="bool", value=1, raw_literal="True")],
    )
    violations = check_doc_invariants(doc, parse_literal=parse_literal)
    assert len(violations) == 1 and "I-2" in violations[0]
