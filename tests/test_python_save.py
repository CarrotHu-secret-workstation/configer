"""Python 适配器 chunk C 验收：save(edits) 外科手术式写回（§7.1）+
字面量风格重放（§7.2 python 全部行，含 str 转义）+ T1–T4 测试矩阵（§7.3）
+ §11 A-5 预演 + 异常语义（未知 path / 只读 / 类型不符 / E-ENCODING）。

裁量锁定（登记于 python_adapter 模块 docstring）：
- fixed(p) 小数位超出 → Python ``%.*f`` 默认舍入（round-half-even 对精确
  二进制值）：0.125→fixed(2)→``0.12``、0.375→``0.38``、2.675→``2.67``、
  1.85→fixed(1)→``1.9``；
- 只读条目收到 EditOp → ValueError（与 yaml 对齐）；
- E-ENCODING / E-PARSE doc → save 抛 ValueError（即使空 edits）；
- 同 path 多条 EditOp → 后者生效；
- R-6 重复赋值 → edit 只改首个赋值；
- 空文件（0 条目）save([]) → 原字节（BOM-less）。
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

import pytest

from configer.adapters.python_adapter import PythonAdapter
from configer.model import CODE_E_ENCODING, CODE_E_PARSE, ConfigDoc, EditOp

ROOT = Path(__file__).resolve().parents[1]
PARAM = ROOT / "testdata" / "param.py"


@pytest.fixture(scope="module")
def gold():
    adapter = PythonAdapter()
    raw = PARAM.read_bytes()
    doc, diags = adapter.load(PARAM)
    return adapter, doc, raw, {it.path: it for it in doc.items}


def _load_synthetic(tmp_path: Path, src: bytes, name: str = "syn.py"):
    p = tmp_path / name
    p.write_bytes(src)
    adapter = PythonAdapter()
    doc, diags = adapter.load(p)
    return adapter, doc, diags, src


def _udiff_changed(orig: bytes, out: bytes) -> list[str]:
    """unified diff 中的变更行（'-'/'+'，不含文件头），§7.3 T2 判据。"""
    ol = orig.decode("utf-8").splitlines()
    nl = out.decode("utf-8").splitlines()
    return [
        line
        for line in difflib.unified_diff(ol, nl, lineterm="", n=0)
        if line[:1] in ("-", "+") and not line.startswith(("---", "+++"))
    ]


def _changed_line_indices(orig: bytes, out: bytes) -> list[int]:
    """逐字节行比对（T4）：变更行下标（0-based）；行数必须一致。"""
    ol = orig.splitlines(keepends=True)
    nl = out.splitlines(keepends=True)
    assert len(ol) == len(nl), "行数变化——违反 §7.1 外科手术替换"
    return [i for i, (x, y) in enumerate(zip(ol, nl)) if x != y]


def _assert_t2_t4(orig: bytes, out: bytes, path: str, literal: str) -> int:
    """T2：unified diff 恰好 1 个变更行且 '+' 行含新字面量；
    T4：除该行外每行与原文件逐字节一致。返回变更行下标（0-based）。"""
    changed = _udiff_changed(orig, out)
    assert len(changed) == 2, f"应恰好 1 个变更行（1 '-' + 1 '+'），实际 {changed}"
    minus, plus = changed
    assert plus.startswith("+" + path) or plus == "+" + path, plus
    assert re.search(r"(?<![\w.])" + re.escape(literal) + r"(?![\w.])", plus[1:]), (
        f"'+' 行不含新字面量 {literal!r}：{plus}"
    )
    idx = _changed_line_indices(orig, out)
    assert len(idx) == 1
    return idx[0]


def _line_of(raw: bytes, path: str) -> int:
    """金样本中赋值行下标（0-based）。"""
    pat = re.compile(r"^" + re.escape(path) + r"\b")
    for i, line in enumerate(raw.decode("utf-8").splitlines()):
        if pat.match(line):
            return i
    raise AssertionError(f"未找到 {path} 的赋值行")


# ======================================================================
# T1 恒等（§7.3；含合成疑难与空文件 / BOM / E-ENCODING 边界）
# ======================================================================

def test_t1_golden_identity(gold) -> None:
    adapter, doc, raw, _ = gold
    assert adapter.save(doc, []) == raw


_HARD_SRC = '''\
# ==== 分隔 ====
# 组名（全角）
# ==== 分隔 ====
# 本节总览: 全角（测试）

KICK_POWER_BY_X: tuple[float, ...] = (
    (-4.0,-1.5),
    (0.0, 1.0),
)  # 尾注释
# 作用: 测试条目
ALIGNED   = 1.0
PLAIN_STR = '中文值'  # 行尾注释
TRAILING = 2   
'''


def test_t1_synthetic_hard_identity(tmp_path: Path) -> None:
    """多行元组、AnnAssign、对齐空格、全角字符注释、尾随空格、行尾注释。"""
    adapter, doc, _, src = _load_synthetic(tmp_path, _HARD_SRC.encode("utf-8"))
    assert adapter.save(doc, []) == src


def test_t1_empty_file_returns_original_bytes(tmp_path: Path) -> None:
    """§4.2 空 .py：0 条目正常加载；save([]) → 原字节（chunk A 坑位修复：
    module 引用挂 doc._py_module，不再依赖 locator 反查）。"""
    adapter, doc, diags, _ = _load_synthetic(tmp_path, b"", "empty.py")
    assert doc.items == []
    assert diags == []
    assert adapter.save(doc, []) == b""


def test_t1_bom_file_save_is_bomless(tmp_path: Path) -> None:
    adapter, doc, _, _ = _load_synthetic(tmp_path, b"\xef\xbb\xbfA = 1.0\n", "bom.py")
    assert adapter.save(doc, []) == b"A = 1.0\n"
    out = adapter.save(doc, [EditOp("A", 2)])
    assert out == b"A = 2.0\n"  # BOM-less 约定对 edits 路径同样成立


def test_t1_mixed_eol_identity(tmp_path: Path) -> None:
    src = b"A = 1.0\r\nB = 2\r\nC = 3.0\n"
    adapter, doc, diags, _ = _load_synthetic(tmp_path, src, "eol.py")
    assert [d.code for d in diags] == ["W-EOL-MIXED"]
    assert adapter.save(doc, []) == src


def test_e_encoding_doc_save_raises(tmp_path: Path) -> None:
    """E-ENCODING doc → save 抛 ValueError（即使空 edits；裁量登记）。"""
    p = tmp_path / "bad.py"
    p.write_bytes(b'A = "\xff\xfe bad"\n')
    adapter = PythonAdapter()
    doc, diags = adapter.load(p)
    assert [d.code for d in diags] == [CODE_E_ENCODING]
    with pytest.raises(ValueError):
        adapter.save(doc, [])
    with pytest.raises(ValueError):
        adapter.save(doc, [EditOp("A", "x")])


def test_e_parse_doc_save_raises(tmp_path: Path) -> None:
    p = tmp_path / "pf.py"
    p.write_bytes(b"def broken(\n")
    adapter = PythonAdapter()
    doc, diags = adapter.load(p)
    assert [d.code for d in diags] == [CODE_E_PARSE]
    with pytest.raises(ValueError):
        adapter.save(doc, [])


def test_foreign_doc_save_raises(tmp_path: Path) -> None:
    """伪造 doc（format 不符 / 无 module）→ ValueError。"""
    adapter = PythonAdapter()
    doc = ConfigDoc(format="yaml", path=tmp_path / "x.py")
    with pytest.raises(ValueError):
        adapter.save(doc, [])
    doc2 = ConfigDoc(format="python", path=tmp_path / "x.py")
    with pytest.raises(ValueError):
        adapter.save(doc2, [])


# ======================================================================
# T2 单点手术 + T4 逐字节行比对（金样本参数化；§7.3）
# ======================================================================

# (path, 新值, 必须写回的字面量, §7.2 规则)
_T2_CASES = [
    ("SETPIECE_DISTANCE_FROM_BALL", 2, "2.00"),      # fixed(2) 收 int → 补零
    ("X_STRAIGHT_M", 20000, "20000.0"),              # fixed(1)
    ("X_OUR_KICKOFF_TARGET", 20000, "20000"),        # int 十进制原样
    ("GUARD_FACE_BALL", False, "False"),             # bool 恒 capital
    ("GUARD_HOME_X", -7, "-7.0"),                    # 负数：符号是字面量一部分
    ("CENTRE_KICKOFF_ANGLE_END_DEG", -75, "-75.0"),  # dormant 条目可编辑（A-3/U-8）
    ("FALLEN_COST", 21.0, "21.0"),                   # fixed(1)（A-4 实例）
]


@pytest.mark.parametrize("path,new_value,literal", _T2_CASES)
def test_t2_single_point_surgery(gold, path, new_value, literal) -> None:
    adapter, doc, raw, by_path = gold
    out = adapter.save(doc, [EditOp(path, new_value)])
    idx = _assert_t2_t4(raw, out, path, literal)
    # 变更行就是该条目的赋值行（其余全部行——含注释块/组头/对齐行——不动）
    assert idx == _line_of(raw, path)
    # （重载语义闭环统一在 test_roundtrip_reload 验证）


def test_t2_dormant_item_is_editable(gold) -> None:
    _, _, _, by_path = gold
    item = by_path["CENTRE_KICKOFF_ANGLE_END_DEG"]
    assert item.dormant is True  # A-3：休眠条目允许编辑并实时落盘（U-8）


def test_t2_str_synthetic(tmp_path: Path) -> None:
    """金样本无 str 常量（§4.3 风险表）：str 用合成文件覆盖 T2。"""
    src = b'GREETING = "say \\"hi\\""\nOTHER = 1\n'
    adapter, doc, _, raw = _load_synthetic(tmp_path, src, "s.py")
    out = adapter.save(doc, [EditOp("GREETING", 'he said "hi"')])
    idx = _assert_t2_t4(raw, out, "GREETING", r'"he said \"hi\""')
    assert out.splitlines()[idx] == b'GREETING = "he said \\"hi\\""'


def test_roundtrip_reload(gold, tmp_path: Path) -> None:
    """T2 各输出重新 load：value/raw_literal/literal_style 闭环正确。"""
    adapter, doc, raw, _ = gold
    edits = [EditOp(p, v) for p, v, _ in _T2_CASES]
    out = adapter.save(doc, edits)
    p = tmp_path / "param_out.py"
    p.write_bytes(out)
    doc2, _ = adapter.load(p)
    by2 = {it.path: it for it in doc2.items}
    for path, new_value, literal in _T2_CASES:
        it = by2[path]
        assert it.raw_literal == literal, path
        if isinstance(new_value, float):
            assert it.value == pytest.approx(new_value)
        else:
            assert it.value == new_value
        assert it.type == doc.items[[i.path for i in doc.items].index(path)].type


# ======================================================================
# T3 风格重放逐条（§7.2 规则表 python 行；字节级字面量断言）
# ======================================================================

def test_t3_fixed2_int_new_value(gold) -> None:
    """`1.60`（fixed(2)）收 int 2 → `2.00`（float 条目收 int 必落 float
    字面量，不得写成 `2`；数值语义 §7.2）。"""
    adapter, doc, raw, _ = gold
    out = adapter.save(doc, [EditOp("SETPIECE_DISTANCE_FROM_BALL", 2)])
    nl = out.splitlines(keepends=True)
    line = nl[_line_of(raw, "SETPIECE_DISTANCE_FROM_BALL")]
    assert line == b"SETPIECE_DISTANCE_FROM_BALL = 2.00\n"  # 禁止 `= 2`（int）


def test_t3_fixed1(gold) -> None:
    adapter, doc, raw, _ = gold
    out = adapter.save(doc, [EditOp("X_STRAIGHT_M", 20000)])
    assert b"X_STRAIGHT_M = 20000.0\n" in out  # 禁止 `20000.` / `20000`


def test_t3_negative_float(gold) -> None:
    """`-6.5` → -7 → `-7.0`：一元负号随字面量重放（§7.2 负数行）。"""
    adapter, doc, raw, _ = gold
    out = adapter.save(doc, [EditOp("GUARD_HOME_X", -7)])
    assert b"GUARD_HOME_X = -7.0\n" in out


def test_t3_int_decimal(gold) -> None:
    adapter, doc, raw, _ = gold
    out = adapter.save(doc, [EditOp("X_OUR_KICKOFF_TARGET", 20000)])
    assert b"X_OUR_KICKOFF_TARGET = 20000\n" in out


def test_t3_bool_capital(gold) -> None:
    """python bool 恒 capital：True→False 写 `False`（不得 `false`）。"""
    adapter, doc, raw, _ = gold
    out = adapter.save(doc, [EditOp("GUARD_FACE_BALL", False)])
    assert b"GUARD_FACE_BALL = False\n" in out
    assert b"false" not in out.splitlines()[_line_of(raw, "GUARD_FACE_BALL")]


def test_t3_plain_float(tmp_path: Path) -> None:
    """plain = 最短表示（repr 语义）：0.15→`0.15`；整数值 float 2.0→`2.0`
    **不是** `2.`（trailing_dot 专属）。"""
    src = b"E = 0.15\n"
    adapter, doc, _, _ = _load_synthetic(tmp_path, src)
    assert adapter.save(doc, [EditOp("E", 2)]).rstrip(b"\n") == b"E = 2.0"
    assert adapter.save(doc, [EditOp("E", 0.15)]).rstrip(b"\n") == b"E = 0.15"
    assert adapter.save(doc, [EditOp("E", 1920.5)]).rstrip(b"\n") == b"E = 1920.5"


def test_t3_trailing_dot(tmp_path: Path) -> None:
    """python 金样本无尾点实例（§3.2 示例为 yaml）：合成覆盖。
    `5.` → 3 → `3.`（整数 → N.）；→ 0.5 → `0.5`（非整数 → 最短表示，
    禁止 `0.50`/`0.5.`）；→ -3 → `-3.`。"""
    src = b"C = 5.\n"
    adapter, doc, _, _ = _load_synthetic(tmp_path, src, "td.py")
    assert adapter.save(doc, [EditOp("C", 3)]).rstrip(b"\n") == b"C = 3."
    assert adapter.save(doc, [EditOp("C", 0.5)]).rstrip(b"\n") == b"C = 0.5"
    assert adapter.save(doc, [EditOp("C", -3)]).rstrip(b"\n") == b"C = -3."


def test_t3_fixed_rounding_discretion(tmp_path: Path) -> None:
    """fixed(p) 小数位超出 → Python %.*f 默认舍入（round-half-even 对精确
    二进制值；规范未明说，裁量登记 + 本测试锁定）。"""
    src = b"G: float = 1.50\n"
    adapter, doc, _, _ = _load_synthetic(tmp_path, src, "fix.py")
    # 0.125/0.375 二进制精确 → 真 half-even：0.12（2 偶）、0.38（8 偶）
    assert adapter.save(doc, [EditOp("G", 0.125)]).splitlines()[0] == b"G: float = 0.12"
    assert adapter.save(doc, [EditOp("G", 0.375)]).splitlines()[0] == b"G: float = 0.38"
    # 2.675 二进制略小于十进制 2.675 → 0.67
    assert adapter.save(doc, [EditOp("G", 2.675)]).splitlines()[0] == b"G: float = 2.67"
    # AnnAssign：注解原样保留，只换值 token
    assert adapter.save(doc, [EditOp("G", 2)]).splitlines()[0] == b"G: float = 2.00"


def test_t3_fixed1_rounding_golden(gold) -> None:
    """KICK_POWER_MIN（fixed(1)，`1.0`）收 1.85 → `1.9`（A-5 预演 +
    fixed 舍入裁量在金样本上的实例）。"""
    adapter, doc, raw, _ = gold
    out = adapter.save(doc, [EditOp("KICK_POWER_MIN", 1.85)])
    _assert_t2_t4(raw, out, "KICK_POWER_MIN", "1.9")


def test_t3_int_negative(tmp_path: Path) -> None:
    src = b"N = 4\n"
    adapter, doc, _, _ = _load_synthetic(tmp_path, src, "neg.py")
    assert adapter.save(doc, [EditOp("N", -3)]).rstrip(b"\n") == b"N = -3"


def test_t3_str_double_quote_escaping(tmp_path: Path) -> None:
    """§7.2 str 转义重放：原值 `"say \\"hi\\""` → 新值 `he said "hi"` →
    写回 `"he said \\"hi\\""`（double 风格，`"`→`\\"`）。"""
    src = b'A = "say \\"hi\\""\n'
    adapter, doc, _, raw = _load_synthetic(tmp_path, src, "sd.py")
    by = {it.path: it for it in doc.items}
    assert by["A"].value == 'say "hi"'
    assert by["A"].literal_style.quote == "double"
    out = adapter.save(doc, [EditOp("A", 'he said "hi"')])
    assert out == b'A = "he said \\"hi\\""\n'
    # 语义闭环
    (tmp_path / "o.py").write_bytes(out)
    doc2, _ = adapter.load(tmp_path / "o.py")
    assert doc2.items[0].value == 'he said "hi"'


def test_t3_str_single_quote_escaping(tmp_path: Path) -> None:
    """single 风格：`'` → `\\'`；异种引号（"）无需转义原样保留。"""
    src = b"B = 'it\\'s'\n"
    adapter, doc, _, _ = _load_synthetic(tmp_path, src, "ss.py")
    by = {it.path: it for it in doc.items}
    assert by["B"].value == "it's"
    assert by["B"].literal_style.quote == "single"
    out = adapter.save(doc, [EditOp("B", "don't")])
    assert out == b"B = 'don\\'t'\n"
    out2 = adapter.save(doc, [EditOp("B", 'say "x"')])
    assert out2 == b"B = 'say \"x\"'\n"


def test_t3_str_special_chars(tmp_path: Path) -> None:
    """反斜杠/换行/制表/控制字符 → 标准转义序列（禁止不转义直接写入）。"""
    src = b'A = "x"\n'
    adapter, doc, _, _ = _load_synthetic(tmp_path, src, "sc.py")
    out = adapter.save(doc, [EditOp("A", "l1\nl2\\b\tt\x01")])
    assert out == b'A = "l1\\nl2\\\\b\\tt\\x01"\n'
    (tmp_path / "o2.py").write_bytes(out)
    doc2, _ = adapter.load(tmp_path / "o2.py")
    assert doc2.items[0].value == "l1\nl2\\b\tt\x01"


# ======================================================================
# T4 专项：对齐空格 / 行尾注释 / 尾随空格 / 缩进续行注释 / EOL
# ======================================================================

def test_t4_alignment_neighbour_lines_untouched(gold) -> None:
    """L437 三空格 `CENTRE_KICKOFF_ANGLE_END_DEG   = -70.0` 与 L443 两空格
    `CENTRE_KICKOFF_ANGLE_STEP_DEG  = -1.0`：编辑邻近条目（START）后两行
    逐字节不动（§7.2 禁止反例：不得动对齐空格）。"""
    adapter, doc, raw, _ = gold
    out = adapter.save(doc, [EditOp("CENTRE_KICKOFF_ANGLE_START_DEG", -30)])
    ol = raw.splitlines(keepends=True)
    nl = out.splitlines(keepends=True)
    i_end = _line_of(raw, "CENTRE_KICKOFF_ANGLE_END_DEG")
    i_step = _line_of(raw, "CENTRE_KICKOFF_ANGLE_STEP_DEG")
    assert ol[i_end] == b"CENTRE_KICKOFF_ANGLE_END_DEG   = -70.0\n"  # 前提核对
    assert nl[i_end] == ol[i_end]
    assert nl[i_step] == ol[i_step]
    assert b"   = " in nl[i_end] and b"  = " in nl[i_step]


def test_t4_alignment_own_line_preserved(gold) -> None:
    """编辑对齐行本身：三空格与行位置不动，只换值 token。"""
    adapter, doc, raw, _ = gold
    out = adapter.save(doc, [EditOp("CENTRE_KICKOFF_ANGLE_END_DEG", -75)])
    nl = out.splitlines(keepends=True)
    i_end = _line_of(raw, "CENTRE_KICKOFF_ANGLE_END_DEG")
    assert nl[i_end] == b"CENTRE_KICKOFF_ANGLE_END_DEG   = -75.0\n"


def test_t4_indented_continuation_comment_preserved(gold) -> None:
    """X_OUR_KICKOFF_TARGET 注释块含缩进续行（L406 `#   (x∈[0, 7]…`）：
    编辑该条目后注释块逐字节不动。"""
    adapter, doc, raw, _ = gold
    out = adapter.save(doc, [EditOp("X_OUR_KICKOFF_TARGET", 20000)])
    ol = raw.splitlines(keepends=True)
    nl = out.splitlines(keepends=True)
    i = _line_of(raw, "X_OUR_KICKOFF_TARGET")
    cont = [j for j in range(i - 6, i) if ol[j].startswith(b"#   ")]
    assert cont, "前提：存在缩进续行注释"
    for j in cont:
        assert nl[j] == ol[j]
    assert ol[i].startswith(b"X_OUR_KICKOFF_TARGET = ")
    assert nl[i] == b"X_OUR_KICKOFF_TARGET = 20000\n"


def test_t4_trailing_comment_and_spaces_synthetic(tmp_path: Path) -> None:
    """行尾注释、等号两侧多空格、值后尾随空格：编辑后逐字节保留。"""
    src = b"VERY_LONG_NAME  = 1.50  # comment stays\nA = 1.0   \n"
    adapter, doc, _, _ = _load_synthetic(tmp_path, src, "tc.py")
    out = adapter.save(doc, [EditOp("VERY_LONG_NAME", 2), EditOp("A", 2)])
    assert out.splitlines(keepends=True)[0] == b"VERY_LONG_NAME  = 2.00  # comment stays\n"
    assert out.splitlines(keepends=True)[1] == b"A = 2.0   \n"


def test_t4_mixed_eol_edit_preserves_other_lines(tmp_path: Path) -> None:
    src = b"A = 1.0\r\nB = 2\r\nC = 3.0\n"
    adapter, doc, _, _ = _load_synthetic(tmp_path, src, "eol2.py")
    out = adapter.save(doc, [EditOp("B", 5)])
    assert out == b"A = 1.0\r\nB = 5\r\nC = 3.0\n"


# ======================================================================
# 多条 edits / 同 path 后者生效 / 同行多赋值
# ======================================================================

def test_multi_edits_three_types_one_save(gold) -> None:
    """一次 save 3 条不同类型 edit → diff 恰 3 个变更行、各自正确；
    其余全部行逐字节不动。"""
    adapter, doc, raw, by_path = gold
    assert by_path["X_OUR_KICKOFF_TARGET"].warning is True  # 哨兵黄标不阻塞
    edits = [
        EditOp("GUARD_FACE_BALL", False),
        EditOp("FALLEN_COST", 21.0),
        EditOp("X_OUR_KICKOFF_TARGET", 20000),
    ]
    out = adapter.save(doc, edits)
    changed = _udiff_changed(raw, out)
    assert len(changed) == 6  # 3 × ('-' + '+')
    idx = _changed_line_indices(raw, out)
    assert len(idx) == 3
    nl = out.splitlines(keepends=True)
    assert nl[_line_of(raw, "GUARD_FACE_BALL")] == b"GUARD_FACE_BALL = False\n"
    assert nl[_line_of(raw, "FALLEN_COST")] == b"FALLEN_COST = 21.0\n"
    assert nl[_line_of(raw, "X_OUR_KICKOFF_TARGET")] == b"X_OUR_KICKOFF_TARGET = 20000\n"


def test_same_path_multiple_edits_last_wins(gold) -> None:
    """同 path 双 edit → 后者生效（与 yaml 对齐，裁量登记）。"""
    adapter, doc, raw, _ = gold
    out = adapter.save(
        doc, [EditOp("FALLEN_COST", 21.0), EditOp("FALLEN_COST", 22.0)]
    )
    nl = out.splitlines(keepends=True)
    assert nl[_line_of(raw, "FALLEN_COST")] == b"FALLEN_COST = 22.0\n"
    assert len(_changed_line_indices(raw, out)) == 1


def test_same_line_semicolon_assignments(tmp_path: Path) -> None:
    """`A = 1; B = 2.0` 同行两赋值（locator.small_index）：两条 edit 叠加，
    分隔符与空格不动。"""
    src = b"A = 1; B = 2.0\n"
    adapter, doc, _, _ = _load_synthetic(tmp_path, src, "semi.py")
    assert [it.path for it in doc.items] == ["A", "B"]
    out = adapter.save(doc, [EditOp("A", 5), EditOp("B", 3)])
    assert out == b"A = 5; B = 3.0\n"


# ======================================================================
# 异常语义（§3.8/§4.1/§8.2 防御；与 yaml 适配器对齐）
# ======================================================================

def test_unknown_path_raises(gold) -> None:
    adapter, doc, raw, _ = gold
    with pytest.raises(ValueError, match="未知 path"):
        adapter.save(doc, [EditOp("NO_SUCH_ITEM", 1)])


@pytest.mark.parametrize(
    "path",
    ["KICK_POWER_BY_X", "READY_OUR_KICKOFF_SLOT1_XY", "PLAN_STEP", "PLAN_MAX_OFFSET"],
)
def test_readonly_item_edit_raises(gold, path) -> None:
    """只读条目（container / non_literal）收到 EditOp → ValueError（裁量，
    与 yaml 一致）。"""
    adapter, doc, raw, by_path = gold
    assert by_path[path].readonly is True
    with pytest.raises(ValueError, match="只读"):
        adapter.save(doc, [EditOp(path, 1)])


def test_type_mismatch_raises(gold, tmp_path: Path) -> None:
    """int 条目收 float → ValueError（session 门控该拦的，adapter 防御）。"""
    adapter, doc, raw, _ = gold
    with pytest.raises(ValueError, match="只接受整数"):
        adapter.save(doc, [EditOp("X_OUR_KICKOFF_TARGET", 1.5)])

    src = b"B = True\nS = 'x'\nF = 1.0\nI = 2\n"
    adapter2, doc2, _, _ = _load_synthetic(tmp_path, src, "ty.py")
    with pytest.raises(ValueError):  # bool ← int（不得写成 True）
        adapter2.save(doc2, [EditOp("B", 1)])
    with pytest.raises(ValueError):  # str ← int
        adapter2.save(doc2, [EditOp("S", 3)])
    with pytest.raises(ValueError):  # float ← bool
        adapter2.save(doc2, [EditOp("F", True)])
    with pytest.raises(ValueError):  # int ← bool
        adapter2.save(doc2, [EditOp("I", True)])
    with pytest.raises(ValueError):  # int ← str
        adapter2.save(doc2, [EditOp("I", "x")])
    with pytest.raises(ValueError):  # float ← inf（非有限，无重放规则）
        adapter2.save(doc2, [EditOp("F", float("inf"))])
    with pytest.raises(ValueError):  # float ← nan
        adapter2.save(doc2, [EditOp("F", float("nan"))])


def test_stale_locator_raises(tmp_path: Path) -> None:
    """locator 失效防御：raw_literal 与树不一致 / locator 指向他 module →
    RuntimeError（须重新 load，§7.6，与 yaml 基线校验对齐）。"""
    src = b"A = 1.0\n"
    adapter, doc, _, _ = _load_synthetic(tmp_path, src, "st.py")
    doc.items[0].raw_literal = "9.99"  # 伪造：与树中 token 不符
    with pytest.raises(RuntimeError, match="重新 load"):
        adapter.save(doc, [EditOp("A", 2)])

    adapter2, doc2, _, _ = _load_synthetic(tmp_path, src, "st2.py")
    import libcst as cst
    doc2.items[0].locator.module = cst.parse_module("A = 1.0\n")  # 他树 module
    with pytest.raises(RuntimeError):
        adapter2.save(doc2, [EditOp("A", 2)])


# ======================================================================
# R-6 交互：重复赋值以首个为准（edit 只改首个，第二处原样）
# ======================================================================

def test_dup_assign_edit_changes_first_only(tmp_path: Path) -> None:
    src = b"X = 1.0\nY = 2\nX = 3.0\n"
    adapter, doc, diags, _ = _load_synthetic(tmp_path, src, "dup.py")
    assert [d.code for d in diags] == ["E-DUP-ASSIGN"]
    out = adapter.save(doc, [EditOp("X", 9)])
    lines = out.splitlines(keepends=True)
    assert lines == [b"X = 9.0\n", b"Y = 2\n", b"X = 3.0\n"]
    assert len(_changed_line_indices(src, out)) == 1


# ======================================================================
# §11 A-5 预演（金样本真实条目端到端）
# ======================================================================

def test_a5_rehearsal_each_edit_one_line(gold) -> None:
    """A-4/A-5 实例逐处：GUARD_FACE_BALL True→False、FALLEN_COST→21.0
    （fixed(1) 风格核对）、KICK_POWER_MIN→1.85（fixed(1) 舍入 → `1.9`，
    裁量锁定）；每处 diff 恰 1 行。"""
    adapter, doc, raw, _ = gold
    for path, value, literal in [
        ("GUARD_FACE_BALL", False, "False"),
        ("FALLEN_COST", 21.0, "21.0"),
        ("KICK_POWER_MIN", 1.85, "1.9"),
    ]:
        out = adapter.save(doc, [EditOp(path, value)])
        idx = _assert_t2_t4(raw, out, path, literal)
        assert idx == _line_of(raw, path)


def test_a5_rehearsal_end_to_end_session_like(gold, tmp_path: Path) -> None:
    """端到端模拟 session：三处修改一次 save → 落盘（tmp，不动 testdata）→
    重新 load（架构裁决：每次提交后重新 load）→ 值/风格正确 + 新基线 T1。"""
    adapter, doc, raw, _ = gold
    edits = [
        EditOp("GUARD_FACE_BALL", False),
        EditOp("FALLEN_COST", 21.0),
        EditOp("KICK_POWER_MIN", 1.85),
    ]
    out = adapter.save(doc, edits)
    assert len(_changed_line_indices(raw, out)) == 3
    p = tmp_path / "param_session.py"
    p.write_bytes(out)
    doc2, _ = adapter.load(p)
    by2 = {it.path: it for it in doc2.items}
    assert by2["GUARD_FACE_BALL"].value is False
    assert by2["FALLEN_COST"].value == pytest.approx(21.0)
    assert by2["FALLEN_COST"].raw_literal == "21.0"
    assert by2["KICK_POWER_MIN"].value == pytest.approx(1.9)  # fixed(1) 舍入
    assert by2["KICK_POWER_MIN"].raw_literal == "1.9"
    # 新基线恒等（T1）与再编辑（撤销语义：改回原值 → 恢复原字面量）
    assert adapter.save(doc2, []) == out
    back = adapter.save(
        doc2,
        [
            EditOp("GUARD_FACE_BALL", True),
            EditOp("FALLEN_COST", 20.0),
            EditOp("KICK_POWER_MIN", 1.0),
        ],
    )
    ol = raw.splitlines(keepends=True)
    nl = back.splitlines(keepends=True)
    diff = [i for i, (x, y) in enumerate(zip(ol, nl)) if x != y]
    # 风格重放可逆：三处改回原值 → 三行全部恢复原字节（1.0 经 fixed(1)
    # 重放仍是 `1.0`），整文件与 raw 恒等（撤销落盘语义，§7.7/A-14）。
    assert diff == []
    assert back == raw
