"""校验引擎测试（规范 §8、§7.5 门控、§3.4 provenance、§10 U-6 数字输入边界）。"""

from __future__ import annotations

import pytest

from configer.core.validation import (
    GateResult,
    ParseResult,
    check_load_conflicts,
    gate_edit,
    parse_bool_input,
    parse_text_input,
)
from configer.model import (
    CODE_W_INFER_CONFLICT,
    PROV_DECLARED,
    PROV_INFERRED,
    SEV_WARNING,
    ConfigDoc,
    ConfigItem,
    EnumCandidate,
    RangeConstraint,
)


def make_item(type_, **kw) -> ConfigItem:
    return ConfigItem(path=kw.pop("path", "x"), group="g", type=type_, **kw)


# ---------------------------------------------------------------------------
# parse_text_input：int（§8.2 表第一行 + U-6）
# ---------------------------------------------------------------------------

class TestParseInt:
    @pytest.mark.parametrize(
        "text", ["3.0", "1.5", '"3"', "'3'", "1e3", "1E3", "0x10", "1_0", " 3", "3 ", "三"]
    )
    def test_rejects_non_decimal(self, text):
        r = parse_text_input(make_item("int"), text)
        assert not r.ok
        assert not r.intermediate
        assert r.error  # 面向用户的中文说明

    @pytest.mark.parametrize("text,expected", [("42", 42), ("-42", -42), ("+7", 7), ("0", 0)])
    def test_accepts_decimal(self, text, expected):
        r = parse_text_input(make_item("int"), text)
        assert r.ok
        assert r.value == expected
        assert isinstance(r.value, int) and not isinstance(r.value, bool)

    @pytest.mark.parametrize("text", ["", "-", "+", ".", "-.", "+."])
    def test_intermediate(self, text):
        r = parse_text_input(make_item("int"), text)
        assert not r.ok
        assert r.intermediate  # UI 不提交、不红标


# ---------------------------------------------------------------------------
# parse_text_input：float（科学计数法、整数值文本）
# ---------------------------------------------------------------------------

class TestParseFloat:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1.5e3", 1500.0),
            ("2", 2.0),
            ("1.5", 1.5),
            ("-6.5", -6.5),
            (".5", 0.5),
            ("100.", 100.0),
            ("-2E-3", -0.002),
        ],
    )
    def test_accepts(self, text, expected):
        r = parse_text_input(make_item("float"), text)
        assert r.ok
        assert isinstance(r.value, float)
        assert r.value == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["inf", "-inf", "nan", "1_0", "0x10", '"1.5"', "1.5.2", "e3"])
    def test_rejects(self, text):
        r = parse_text_input(make_item("float"), text)
        assert not r.ok
        assert not r.intermediate

    @pytest.mark.parametrize("text", ["", "-", "."])
    def test_intermediate(self, text):
        r = parse_text_input(make_item("float"), text)
        assert not r.ok and r.intermediate


# ---------------------------------------------------------------------------
# parse_text_input：str / bool / None
# ---------------------------------------------------------------------------

class TestParseOtherTypes:
    @pytest.mark.parametrize("text", ["", "abc", "123", "a|b"])
    def test_str_accepts_anything(self, text):
        r = parse_text_input(make_item("str"), text)
        assert r.ok and r.value == text

    def test_bool_text_not_applicable(self):
        r = parse_text_input(make_item("bool"), "true")
        assert not r.ok and not r.intermediate and r.error

    def test_type_none_rejected(self):
        r = parse_text_input(make_item(None), "1")
        assert not r.ok and not r.intermediate

    def test_parse_bool_input_passthrough(self):
        item = make_item("bool")
        assert parse_bool_input(item, True) == ParseResult(ok=True, value=True)
        assert parse_bool_input(item, False).value is False

    def test_parse_bool_input_wrong_type(self):
        r = parse_bool_input(make_item("int"), True)
        assert not r.ok


# ---------------------------------------------------------------------------
# gate_edit：类型匹配（硬），含 bool 是 int 子类陷阱
# ---------------------------------------------------------------------------

class TestGateTypes:
    def test_int_item_rejects_bool_subclass_trap(self):
        # Python bool 是 int 子类：True 不得被当作 int 1 接受
        r = gate_edit(make_item("int"), True)
        assert r.level == "block" and r.reason
        assert gate_edit(make_item("int"), False).level == "block"

    def test_float_item_rejects_bool(self):
        assert gate_edit(make_item("float"), True).level == "block"

    def test_int_item_rejects_float_and_str(self):
        assert gate_edit(make_item("int"), 3.0).level == "block"
        assert gate_edit(make_item("int"), 1.5).level == "block"
        assert gate_edit(make_item("int"), "3").level == "block"

    def test_float_item_accepts_int_normalized(self):
        r = gate_edit(make_item("float"), 2)
        assert r.level == "ok"
        assert isinstance(r.value, float) and r.value == 2.0

    def test_float_item_accepts_float(self):
        assert gate_edit(make_item("float"), 1.5).level == "ok"

    def test_bool_item_only_true_bool(self):
        assert gate_edit(make_item("bool"), True).level == "ok"
        assert gate_edit(make_item("bool"), 1).level == "block"
        assert gate_edit(make_item("bool"), "true").level == "block"

    def test_str_item_accepts_any_str(self):
        assert gate_edit(make_item("str"), "").level == "ok"
        assert gate_edit(make_item("str"), "任意").level == "ok"
        assert gate_edit(make_item("str"), 3).level == "block"

    def test_type_none_blocked(self):
        assert gate_edit(make_item(None), 1).level == "block"


# ---------------------------------------------------------------------------
# gate_edit：readonly / dormant（§7.5、§3.5）
# ---------------------------------------------------------------------------

class TestGateFlags:
    def test_readonly_blocked(self):
        r = gate_edit(make_item("int", readonly=True, readonly_reason="container"), 1)
        assert r.level == "block"

    def test_dormant_not_a_gate_condition(self):
        # dormant 允许编辑照常落盘（§7.5、U-8），gate 不因 dormant 降级
        item = make_item("int", dormant=True, value=5)
        assert gate_edit(item, 6).level == "ok"

    def test_dormant_does_not_soften_declared_block(self):
        item = make_item(
            "int",
            dormant=True,
            range=RangeConstraint(min=0, max=10, provenance=PROV_DECLARED),
        )
        assert gate_edit(item, 99).level == "block"


# ---------------------------------------------------------------------------
# gate_edit：range（单边界，§3.1/§8.2）
# ---------------------------------------------------------------------------

class TestGateRange:
    def test_declared_min_only(self):
        item = make_item("int", range=RangeConstraint(min=0, provenance=PROV_DECLARED))
        assert gate_edit(item, -1).level == "block"
        assert gate_edit(item, 0).level == "ok"
        assert gate_edit(item, 10**9).level == "ok"  # 空侧不设限

    def test_declared_max_only(self):
        item = make_item("float", range=RangeConstraint(max=1.0, provenance=PROV_DECLARED))
        assert gate_edit(item, 1.5).level == "block"
        assert gate_edit(item, 1.0).level == "ok"  # 边界含等号
        assert gate_edit(item, -999.0).level == "ok"

    def test_declared_block_reason_has_declared_hook(self):
        item = make_item("int", range=RangeConstraint(min=1, max=10, provenance=PROV_DECLARED))
        r = gate_edit(item, 0)
        assert r.level == "block"
        assert "declared" in r.reason  # UI 拼 @ 标注原文的钩子
        assert r.provenance == PROV_DECLARED

    def test_inferred_range_violation_warns_not_blocks(self):
        item = make_item("int", range=RangeConstraint(min=1, max=10, provenance=PROV_INFERRED))
        r = gate_edit(item, 99)
        assert r.level == "warn"  # I-6：推测永不拦截
        assert r.provenance == PROV_INFERRED
        assert r.value == 99  # 照常提交

    def test_unit_in_reason(self):
        item = make_item(
            "float", range=RangeConstraint(min=0.0, max=5.0, unit="米", provenance=PROV_DECLARED)
        )
        r = gate_edit(item, 9.0)
        assert r.level == "block" and "米" in r.reason


# ---------------------------------------------------------------------------
# gate_edit：enum（declared 限内 / inferred 仅建议，YC-6）
# ---------------------------------------------------------------------------

def enum_item(prov, values=("rl", "classic")):
    return make_item(
        "str",
        enum_candidates=[EnumCandidate(value=v, provenance=prov) for v in values],
    )


class TestGateEnum:
    def test_declared_enum_limits_to_candidates(self):
        item = enum_item(PROV_DECLARED)
        assert gate_edit(item, "rl").level == "ok"
        r = gate_edit(item, "other")
        assert r.level == "block"
        assert "declared" in r.reason
        assert r.provenance == PROV_DECLARED

    def test_inferred_enum_outside_warns(self):
        item = enum_item(PROV_INFERRED)
        assert gate_edit(item, "rl").level == "ok"
        r = gate_edit(item, "自由输入")  # YC-6：允许列表外值
        assert r.level == "warn"
        assert r.provenance == PROV_INFERRED

    def test_declared_overrides_inferred_defensively(self):
        # §3.4：模型层保证不并存；若违规并存，按 declared 优先（硬）
        item = make_item(
            "str",
            enum_candidates=[
                EnumCandidate(value="a", provenance=PROV_DECLARED),
                EnumCandidate(value="b", provenance=PROV_INFERRED),
            ],
        )
        assert gate_edit(item, "c").level == "block"


# ---------------------------------------------------------------------------
# check_load_conflicts（§5.2 通用化、§3.7 W-INFER-CONFLICT）
# ---------------------------------------------------------------------------

class TestLoadConflicts:
    def _doc(self, *items) -> ConfigDoc:
        from pathlib import Path

        return ConfigDoc(format="yaml", path=Path("t.yaml"), items=list(items))

    def test_inferred_range_conflict_warning(self):
        item = make_item(
            "int",
            path="THROW_MIN",
            value=99,
            range=RangeConstraint(min=1, max=10, provenance=PROV_INFERRED),
        )
        diags = check_load_conflicts(self._doc(item))
        assert len(diags) == 1
        d = diags[0]
        assert d.severity == SEV_WARNING
        assert d.code == CODE_W_INFER_CONFLICT
        assert d.path == "THROW_MIN"

    def test_inferred_enum_conflict_warning(self):
        item = enum_item(PROV_INFERRED)
        item.path, item.value = "mode", "unexpected"
        diags = check_load_conflicts(self._doc(item))
        assert len(diags) == 1 and diags[0].code == CODE_W_INFER_CONFLICT

    def test_no_conflict_no_diagnostic(self):
        item = make_item("int", value=5, range=RangeConstraint(min=1, max=10, provenance=PROV_INFERRED))
        assert check_load_conflicts(self._doc(item)) == []

    def test_declared_conflict_out_of_scope(self):
        # 裁量：W-INFER-CONFLICT 语义专指推测冲突；declared 现值冲突不在此报告
        item = make_item("int", value=99, range=RangeConstraint(min=1, max=10, provenance=PROV_DECLARED))
        assert check_load_conflicts(self._doc(item)) == []

    def test_readonly_and_valueless_skipped(self):
        ro = make_item("int", value=99, readonly=True,
                       range=RangeConstraint(max=1, provenance=PROV_INFERRED))
        novalue = make_item("int", value=None,
                            range=RangeConstraint(max=1, provenance=PROV_INFERRED))
        assert check_load_conflicts(self._doc(ro, novalue)) == []


# ---------------------------------------------------------------------------
# 集成加分项：真实 YAML 适配器加载 testdata/config.yaml（只读使用）
# ---------------------------------------------------------------------------

def test_load_conflicts_on_real_yaml_doc():
    yaml_adapter = pytest.importorskip("configer.adapters.yaml_adapter")
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "testdata" / "config.yaml"
    if not path.exists():
        pytest.skip("testdata/config.yaml 不存在")
    doc, _adapter_diags = yaml_adapter.YamlAdapter().load(path)
    diags = check_load_conflicts(doc)
    # 金样本加载合法：不应产生 error；本 helper 输出全部为 warning 级别
    assert all(d.severity == SEV_WARNING and d.code == CODE_W_INFER_CONFLICT for d in diags)
    assert all(d.path in {i.path for i in doc.items} for d in diags)
