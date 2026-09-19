"""YAML 适配器 §7.3 写回保真测试矩阵 T1–T4（金样本 testdata/config.yaml）。

- T1 恒等往返：load → save(空 edits) 输出字节 == 原始字节（含金样本大量
  行尾空格、`True`/`true` 大小写、`100.` 尾点、纯空白缩进行）；
- T2 单点手术：单条 EditOp → 与原文件 unified diff **恰好 1 个变更行**，
  且该行含新字面量（覆盖 int/float/bool/str/深层嵌套 ≥5 条目）；
- T3 风格重放：§7.2 规则表 yaml 相关行逐条断言（trailing_dot/fixed(p)/
  bool_case/quote/int 十进制/裸 str 安全回退/str 转义重放），含 §7.2
  「禁止」反例（不得 `100.`→`100.0`、`True`→`true`、`"espeak"`→espeak 等）；
- T4 逐字节行比对：除变更行外每行与原文件逐字节一致（含行尾空格——
  config.yaml 78 行以单个空格结尾，是专门考察点）。

金样本只读（testdata 由 test_testdata.py 钉死 sha256）；save 输出在内存比对
或写 tmp_path。
"""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from configer.adapters.yaml_adapter import YamlAdapter
from configer.model import SEV_ERROR, EditOp

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "testdata" / "config.yaml"


@pytest.fixture()
def adapter() -> YamlAdapter:
    return YamlAdapter()


@pytest.fixture()
def golden(adapter: YamlAdapter):
    doc, diags = adapter.load(GOLDEN)
    assert not [d for d in diags if d.severity == SEV_ERROR], diags
    return doc


def byte_lines(data: bytes) -> list[bytes]:
    """字节级分行（bytes.splitlines 只认 \\r\\n/\\n/\\r，不受 Unicode 行界影响）。"""
    return data.splitlines(keepends=True)


def changed_pairs(orig: bytes, out: bytes) -> list[tuple[str, str]]:
    """unified diff 的 (-旧行, +新行) 对（T2「恰好 1 变更行」断言工具）。

    金样本为纯 LF、无 U+0085/U+2028 等 Unicode 行界字符（已实测），
    解码后按 str 行走 difflib 与字节行等价。
    """
    a = orig.decode("utf-8").splitlines(keepends=True)
    b = out.decode("utf-8").splitlines(keepends=True)
    diff = list(difflib.unified_diff(a, b, "orig", "out"))
    minus = [l[1:] for l in diff if l.startswith("-") and not l.startswith("---")]
    plus = [l[1:] for l in diff if l.startswith("+") and not l.startswith("+++")]
    assert len(minus) == len(plus), diff
    return list(zip(minus, plus))


def assert_one_change(adapter, doc, path, new_value, old_line, new_line) -> bytes:
    """单条 EditOp → unified diff 恰 1 对变更行，且行内容逐字节符合预期。"""
    orig = GOLDEN.read_bytes()
    out = adapter.save(doc, [EditOp(path, new_value)])
    pairs = changed_pairs(orig, out)
    assert len(pairs) == 1, f"{path}: 变更行数 {len(pairs)} != 1"
    assert pairs[0] == (old_line, new_line)
    # T4 同步校验：字节行级除该行外全部一致
    ol, nl = byte_lines(orig), byte_lines(out)
    assert len(ol) == len(nl)
    diff_idx = [i for i in range(len(ol)) if ol[i] != nl[i]]
    assert len(diff_idx) == 1
    return out


# ---------------------------------------------------------------------------
# T1 恒等往返（§7.3、I-3）
# ---------------------------------------------------------------------------


def test_t1_identity_roundtrip_golden(adapter: YamlAdapter, golden):
    orig = GOLDEN.read_bytes()
    out = adapter.save(golden, [])
    assert out == orig  # 字节级完全一致（含行尾空格/BOM-less/纯 LF）
    # 抽样显式断言 §7.3 专门考察点：行尾空格、True 大小写、尾点浮点
    text = out.decode("utf-8")
    assert "      step_interval: 100. \n" in text      # 值后行尾空格
    assert "      robot_height: 0.90 \n" in text       # 尾零 + 行尾空格
    assert "    enable_com: True  # " in text          # capital bool
    assert sum(1 for l in byte_lines(out) if l.endswith(b" \n")) == 78


def test_t1_identity_synthetic_tricky(adapter: YamlAdapter, tmp_path):
    """合成疑难样例恒等往返：行尾空格、CRLF 行、块标量、序列、注释。"""
    src = (
        "# 头注释\n"
        "a: 100. # 尾点浮点   \n"        # 注释后行尾空格
        "b:\n"
        "  c: \"x\" \n"                   # 引号值后行尾空格
        "d: True\r\n"                     # CRLF 行（混杂 → W-EOL-MIXED，非 error）
        "e: |\n"                          # 块标量（只读）
        "  block text\n"
        "f: [1, 2] # 序列只读\n"
    ).encode()
    p = tmp_path / "t.yaml"
    p.write_bytes(src)
    doc, diags = adapter.load(p)
    assert not [d for d in diags if d.severity == SEV_ERROR], diags
    assert adapter.save(doc, []) == src


# ---------------------------------------------------------------------------
# T2 单点手术（§7.3、I-4）：int / float / bool / str / 深层嵌套
# ---------------------------------------------------------------------------

T2_CASES = [
    # (path, 新值, 原行, 期望新行, 新字面量)
    ("game.player_id", 4,
     "      player_id: 3 # 1 | 2 | 3 | 4 | 5\n",
     "      player_id: 4 # 1 | 2 | 3 | 4 | 5\n", "4"),                      # int
    ("ball_predictor.step_interval", 1920,
     "      step_interval: 100. \n",
     "      step_interval: 1920. \n", "1920."),                             # float trailing_dot，行尾空格保留
    ("robot.robot_height", 1.05,
     "      robot_height: 0.90 \n",
     "      robot_height: 1.05 \n", "1.05"),                                # float fixed(2)
    ("enable_com", False,
     "    enable_com: True  # 是否开启队友间通信，建议打开\n",
     "    enable_com: False  # 是否开启队友间通信，建议打开\n", "False"),    # bool capital，根级伪组
    ("sound.enable", False,
     "      enable: true \n",
     "      enable: false \n", "false"),                                    # bool lower
    ("game.field_type", "kid_size",
     '      field_type: "adult_size" # adult_size | kid_size | robo_league\n',
     '      field_type: "kid_size" # adult_size | kid_size | robo_league\n',
     '"kid_size"'),                                                          # str double
    ("rerunLog.server_ip", "10.0.0.1:9876",
     '      server_ip: "192.168.10.110:9876" # 开启了 rerun Viewer 的机器地址\n',
     '      server_ip: "10.0.0.1:9876" # 开启了 rerun Viewer 的机器地址\n',
     '"10.0.0.1:9876"'),
    ("rl_brain.ball_vel.alpha", 0.42,
     "        alpha: 0.5 # 低通系数: lp = alpha·v_raw + (1-alpha)·lp\n",
     "        alpha: 0.42 # 低通系数: lp = alpha·v_raw + (1-alpha)·lp\n",
     "0.42"),                                                                # 深层嵌套 3 级，行尾注释不动
    ("strategy.shoot.xmin", 0.45,
     "        xmin: 0.3\n",
     "        xmin: 0.45\n", "0.45"),                                        # 深层嵌套 3 级
]


@pytest.mark.parametrize("path,new_value,old_line,new_line,literal", T2_CASES)
def test_t2_single_point_surgery(adapter, golden, path, new_value, old_line, new_line, literal):
    out = assert_one_change(adapter, golden, path, new_value, old_line, new_line)
    # 变更行含新字面量（T2）；旧字面量行不再出现
    pairs = changed_pairs(GOLDEN.read_bytes(), out)
    assert literal in pairs[0][1]
    assert old_line.encode() not in out


def test_i4_multi_edit_one_commit(adapter, golden):
    """一次提交多条 EditOp → 变更行数 == edits 触及行数（I-4），每行含各自新字面量。"""
    orig = GOLDEN.read_bytes()
    edits = [
        EditOp("game.player_id", 4),
        EditOp("ball_predictor.step_interval", 1920),
        EditOp("enable_com", False),
        EditOp("rl_brain.ball_vel.alpha", 0.42),
    ]
    out = adapter.save(golden, edits)
    pairs = changed_pairs(orig, out)
    assert len(pairs) == 4
    assert set(pairs) == {
        ("      player_id: 3 # 1 | 2 | 3 | 4 | 5\n",
         "      player_id: 4 # 1 | 2 | 3 | 4 | 5\n"),
        ("      step_interval: 100. \n",
         "      step_interval: 1920. \n"),
        ("    enable_com: True  # 是否开启队友间通信，建议打开\n",
         "    enable_com: False  # 是否开启队友间通信，建议打开\n"),
        ("        alpha: 0.5 # 低通系数: lp = alpha·v_raw + (1-alpha)·lp\n",
         "        alpha: 0.42 # 低通系数: lp = alpha·v_raw + (1-alpha)·lp\n"),
    }
    # T4：除这 4 行外逐字节行级一致
    ol, nl = byte_lines(orig), byte_lines(out)
    assert len(ol) == len(nl)
    assert sum(1 for i in range(len(ol)) if ol[i] != nl[i]) == 4


def test_same_path_edits_last_wins(adapter, golden):
    """同一 path 多条 EditOp → 后者生效（登记裁量 4/6）。"""
    out = adapter.save(golden, [
        EditOp("game.player_id", 4),
        EditOp("game.player_id", 5),
    ])
    pairs = changed_pairs(GOLDEN.read_bytes(), out)
    assert pairs == [
        ("      player_id: 3 # 1 | 2 | 3 | 4 | 5\n",
         "      player_id: 5 # 1 | 2 | 3 | 4 | 5\n"),
    ]


# ---------------------------------------------------------------------------
# T3 风格重放（§7.2 规则表 yaml 行逐条）
# ---------------------------------------------------------------------------


def test_t3_trailing_dot(adapter, golden):
    # `100.` → 1920 → `1920.`（禁止写成 `1920.0` / `1920`）
    assert_one_change(adapter, golden, "ball_predictor.step_interval", 1920,
                      "      step_interval: 100. \n",
                      "      step_interval: 1920. \n")
    # `100.` → 0.5 → `0.5`（非整数 → 最短表示）
    assert_one_change(adapter, golden, "ball_predictor.step_interval", 0.5,
                      "      step_interval: 100. \n",
                      "      step_interval: 0.5 \n")
    # 整数值 2 → trailing_dot → `2.`
    assert_one_change(adapter, golden, "ball_predictor.step_interval", 2,
                      "      step_interval: 100. \n",
                      "      step_interval: 2. \n")


def test_t3_fixed_decimals(adapter, golden):
    # `0.90` → 1.05 → `1.05`（fixed(2) 恰好 2 位）
    assert_one_change(adapter, golden, "robot.robot_height", 1.05,
                      "      robot_height: 0.90 \n",
                      "      robot_height: 1.05 \n")
    # `0.90` → 1.2 → `1.20`（fixed(2) 不足位补零；禁止写成 `1.2`）
    assert_one_change(adapter, golden, "robot.robot_height", 1.2,
                      "      robot_height: 0.90 \n",
                      "      robot_height: 1.20 \n")


def test_t3_bool_case(adapter, golden):
    # `True`（capital）→ False → `False`（禁止写成 `false`）
    assert_one_change(adapter, golden, "enable_com", False,
                      "    enable_com: True  # 是否开启队友间通信，建议打开\n",
                      "    enable_com: False  # 是否开启队友间通信，建议打开\n")
    # `true`（lower）→ false → `false`（禁止写成 `False`）
    assert_one_change(adapter, golden, "sound.enable", False,
                      "      enable: true \n",
                      "      enable: false \n")
    # lower `false` → True → `true`（禁止写成 `True`）
    assert_one_change(adapter, golden, "strategy.power_shoot.enable", True,
                      "        enable: false \n",
                      "        enable: true \n")
    # 同值编辑 lower `true` → True：输出与原文恒等（不归一化为 capital）
    out = adapter.save(golden, [EditOp("sound.enable", True)])
    assert out == GOLDEN.read_bytes()


def test_t3_quote_double(adapter, golden):
    # `"adult_size"` → kid_size → `"kid_size"`（禁止裸写 kid_size）
    assert_one_change(adapter, golden, "game.field_type", "kid_size",
                      '      field_type: "adult_size" # adult_size | kid_size | robo_league\n',
                      '      field_type: "kid_size" # adult_size | kid_size | robo_league\n')
    # `"espeak"` → flite → `"flite"`（§7.2 反例：不得写成 espeak；行尾空格保留）
    assert_one_change(adapter, golden, "sound.sound_pack", "flite",
                      '      sound_pack: "espeak" \n',
                      '      sound_pack: "flite" \n')


def test_t3_int_decimal(adapter, golden):
    # `3` → 4 → `4`（十进制原样）
    assert_one_change(adapter, golden, "game.player_id", 4,
                      "      player_id: 3 # 1 | 2 | 3 | 4 | 5\n",
                      "      player_id: 4 # 1 | 2 | 3 | 4 | 5\n")
    assert_one_change(adapter, golden, "game.team_id", 70,
                      "      team_id: 66 # consistant with GameControl\n",
                      "      team_id: 70 # consistant with GameControl\n")


def test_t3_float_plain_and_int_to_float(adapter, golden):
    # plain float：0.5 → 0.42（最短表示；深层嵌套行尾注释不动）
    assert_one_change(adapter, golden, "rl_brain.ball_vel.alpha", 0.42,
                      "        alpha: 0.5 # 低通系数: lp = alpha·v_raw + (1-alpha)·lp\n",
                      "        alpha: 0.42 # 低通系数: lp = alpha·v_raw + (1-alpha)·lp\n")
    # float 条目收整数值必落 float 字面量（§7.2 数值语义）：plain `1.2` → 2 → `2.0`
    assert_one_change(adapter, golden, "robot.odom_factor", 2,
                      "      odom_factor: 1.2 \n",
                      "      odom_factor: 2.0 \n")
    # 负 plain float：-0.4 → -0.5
    assert_one_change(adapter, golden, "ball_predictor.acceleration", -0.5,
                      "      acceleration: -0.4 \n",
                      "      acceleration: -0.5 \n")


# --- T3 合成用例（金样本无裸 str / 转义 str 实例，§7.2 注明「无金样本实例」）---


BARE_STR_CASES = [
    # (新值, 期望字面量)：quote='none' 裸写合法 → 原样；不合法 → 回退双引号（§7.2）
    ("slow", "slow"),           # 合法裸写
    ("a: b", '"a: b"'),         # 含 `: `
    ("true", '"true"'),         # 按 1.2 core 解析为 bool
    ("123", '"123"'),           # 解析为 int
    ("", '""'),                 # 空串
    ("#tag", '"#tag"'),         # 前导指示符
    (" lead", '" lead"'),       # 前导空白
    ("x\ny", '"x\\ny"'),        # 含换行 → 双引号 + 标准转义
]


@pytest.mark.parametrize("new_value,expected_literal", BARE_STR_CASES)
def test_t3_bare_str_safe_fallback(adapter, tmp_path, new_value, expected_literal):
    p = tmp_path / "t.yaml"
    p.write_bytes(b"mode: fast\n")
    doc, diags = adapter.load(p)
    assert not [d for d in diags if d.severity == SEV_ERROR]
    out = adapter.save(doc, [EditOp("mode", new_value)])
    assert out == f"mode: {expected_literal}\n".encode()
    # 语义回读：写回结果重新 load 后仍是同一 str 值（安全回退的意义所在）
    p2 = tmp_path / "t2.yaml"
    p2.write_bytes(out)
    doc2, _ = adapter.load(p2)
    it2 = next(it for it in doc2.items if it.path == "mode")
    assert it2.type == "str" and it2.value == new_value


def test_t3_str_escape_replay(adapter, tmp_path):
    """§7.2 str 转义重放行：`"say \\"hi\\""` → `he said "hi"` → `"he said \\"hi\\""`。"""
    p = tmp_path / "t.yaml"
    p.write_bytes('s: "say \\"hi\\""\n'.encode())
    doc, diags = adapter.load(p)
    assert not [d for d in diags if d.severity == SEV_ERROR]
    it = next(x for x in doc.items if x.path == "s")
    assert it.type == "str" and it.value == 'say "hi"'
    assert it.raw_literal == '"say \\"hi\\""' and it.literal_style.quote == "double"
    out = adapter.save(doc, [EditOp("s", 'he said "hi"')])
    assert out == 's: "he said \\"hi\\""\n'.encode()
    # 反斜杠与换行按双引号风格标准转义
    out = adapter.save(doc, [EditOp("s", "a\\b\nc")])
    assert out == 's: "a\\\\b\\nc"\n'.encode()
    # 语义回读
    p2 = tmp_path / "t2.yaml"
    p2.write_bytes(out)
    doc2, _ = adapter.load(p2)
    assert next(x for x in doc2.items if x.path == "s").value == "a\\b\nc"


def test_t3_single_quote_replay(adapter, tmp_path):
    """单引号风格保留（唯一转义 `'` → `''`）；含换行 → 回退双引号（登记裁量）。"""
    p = tmp_path / "t.yaml"
    p.write_bytes("s: 'it''s ok'\n".encode())
    doc, diags = adapter.load(p)
    assert not [d for d in diags if d.severity == SEV_ERROR]
    it = next(x for x in doc.items if x.path == "s")
    assert it.value == "it's ok" and it.literal_style.quote == "single"
    assert adapter.save(doc, [EditOp("s", "a'b")]) == "s: 'a''b'\n".encode()
    assert adapter.save(doc, [EditOp("s", "x\ny")]) == 's: "x\\ny"\n'.encode()


# ---------------------------------------------------------------------------
# T4 逐字节行比对（§7.3：除变更行外每行逐字节一致，含行尾空格）
# ---------------------------------------------------------------------------


def test_t4_untouched_lines_byte_identical(adapter, golden):
    orig = GOLDEN.read_bytes()
    out = adapter.save(golden, [EditOp("ball_predictor.step_interval", 1920)])
    ol, nl = byte_lines(orig), byte_lines(out)
    assert len(ol) == len(nl) == 185
    old_line = "      step_interval: 100. \n".encode()
    idx = ol.index(old_line)          # 金样本内唯一
    diff_idx = [i for i in range(len(ol)) if ol[i] != nl[i]]
    assert diff_idx == [idx]
    # 变更行本身保留行尾空格（token 手术只动 `100.` 字符区间）
    assert nl[idx] == "      step_interval: 1920. \n".encode()
    # 行尾空格行总数不变（金样本 78 行以单个空格结尾）
    assert sum(1 for l in nl if l.endswith(b" \n")) == 78
    # 纯空白缩进行原样（L11 = 4 空格行、L18 = 6 空格行；ruamel dump 会归一化
    # 这类行，指针手术不会）+ 首行/末行
    assert nl[10] == ol[10] == b"    \n"
    assert nl[17] == ol[17] == b"      \n"
    assert nl[0] == ol[0] and nl[-1] == ol[-1]


# ---------------------------------------------------------------------------
# save 提交期防御（登记裁量 4/6：类型不符 → ValueError）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,bad", [
    ("game.player_id", 4.5),       # int 条目收 float
    ("game.player_id", "4"),       # int 条目收 str
    ("game.player_id", True),      # int 条目收 bool
    ("enable_com", 1),             # bool 条目收 int
    ("game.field_type", 3),        # str 条目收 int
    ("robot.robot_height", True),  # float 条目收 bool
])
def test_save_type_mismatch_raises(adapter, golden, path, bad):
    with pytest.raises(ValueError):
        adapter.save(golden, [EditOp(path, bad)])
