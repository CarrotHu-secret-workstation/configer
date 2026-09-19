"""YAML 适配器 detect/load 语义测试。

覆盖：§4.2 detect 嗅探、§4.4 Y-1..Y-9、§5.3 YC-1..YC-7、§3.9 不变量 I-1/I-2、
§11 A-2/A-3/A-6、字节层约定（E-ENCODING/BOM/W-EOL-MIXED）。
金样本 testdata/config.yaml 只读；save 输出一律写 tmp_path 或内存比对。
"""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from configer.adapters.yaml_adapter import YamlAdapter
from configer.adapters.yaml_scalar import resolve_core
from configer.model import (
    CODE_E_DUP_KEY,
    CODE_E_ENCODING,
    CODE_E_PARSE,
    CODE_E_PARSE_MULTIDOC,
    CODE_I_ANCHOR_READONLY,
    CODE_I_ENUM_DROPPED,
    CODE_I_SEQ_READONLY,
    CODE_W_EOL_MIXED,
    PROV_COMMENT,
    PROV_INFERRED,
    PROV_NONE,
    READONLY_ANCHOR,
    READONLY_CONTAINER,
    READONLY_NON_PLAIN_SCALAR,
    SEV_ERROR,
    SEV_INFO,
    SEV_WARNING,
    EditOp,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "testdata" / "config.yaml"
ROOT_GROUP = "（根级）"
GOLDEN_GROUPS = [
    "game", "ball_predictor", "robot", "strategy", "obstacle_avoidance",
    "locator", "rl_brain", "rerunLog", "recovery", "vision", "debug", "sound",
]


@pytest.fixture()
def adapter() -> YamlAdapter:
    return YamlAdapter()


@pytest.fixture()
def golden_doc(adapter: YamlAdapter):
    doc, diagnostics = adapter.load(GOLDEN)
    assert not [d for d in diagnostics if d.severity == SEV_ERROR], diagnostics
    return doc, diagnostics


def item(doc, path):
    for it in doc.items:
        if it.path == path:
            return it
    raise AssertionError(f"条目 {path!r} 不存在；现有 {len(doc.items)} 条")


def codes(diags, code):
    return [d for d in diags if d.code == code]


def write(tmp_path: Path, content: bytes, name: str = "t.yaml") -> Path:
    p = tmp_path / name
    p.write_bytes(content)
    return p


# ---------------------------------------------------------------------------
# detect（§4.2）
# ---------------------------------------------------------------------------


def test_detect_extension_priority(adapter: YamlAdapter, tmp_path):
    # 扩展名命中 → True（即使内容是垃圾；确认失败由 registry 流程给 E-PARSE）
    p = write(tmp_path, b"\x00garbage[[[")
    assert adapter.detect(p, b"\x00garbage[[[") is True
    assert adapter.detect(tmp_path / "x.yml", b"") is True
    assert adapter.detect(tmp_path / "X.YAML", b"") is True


def test_detect_sniff_mapping(adapter: YamlAdapter, tmp_path):
    p = tmp_path / "noext"
    assert adapter.detect(p, "a: 1\nb:\n  c: 2\n".encode()) is True


def test_detect_sniff_rejects(adapter: YamlAdapter, tmp_path):
    p = tmp_path / "noext"
    assert adapter.detect(p, "- 1\n- 2\n".encode()) is False          # 非 mapping
    assert adapter.detect(p, b"a: 1\n---\nb: 2\n") is False           # 多文档
    assert adapter.detect(p, b"") is False                            # 空
    assert adapter.detect(p, b"\xff\xfe[[ not yaml") is False         # 坏字节
    assert adapter.detect(p, b"a: [1\n") is False                     # 语法错误


def test_detect_sniff_python_source_is_legal_yaml_mapping(adapter: YamlAdapter, tmp_path):
    """裁决记录（2026-09-08，修正原 ``is False`` 错误断言）：

    §4.2 把 yaml 内容嗅探定义为「可被 ruamel 解析为**单文档 mapping**」。
    ``def f():\\n    return 1\\n`` 在 YAML 里恰是合法单文档 mapping（键
    ``def f()``、值 plain 标量 ``return 1``）→ detect 返回 True 是规范行为，
    不是误判。注册表流程保证不会误伤：``.py`` 扩展名先命中 python 适配器
    （§4.2 扩展名优先）；无扩展名的 python 源按注册表顺序 [Python, Yaml]
    先由 libcst 嗅探（可解析为模块 → python 接管），轮不到 yaml。不采纳
    「长得像 python 就拒绝」的收紧：它偏离 §4.2 嗅探定义的字面，且会误杀
    合法 YAML（键本就允许含括号等任意 plain 字符）。
    """
    p = tmp_path / "noext"
    assert adapter.detect(p, b"def f():\n    return 1\n") is True


# ---------------------------------------------------------------------------
# A-2 金样本解析完整性（§11）
# ---------------------------------------------------------------------------


def test_a2_counts(golden_doc):
    doc, _ = golden_doc
    assert len(doc.items) == 138
    assert all(not it.readonly for it in doc.items)  # 金样本无只读条目
    names = [g.name for g in doc.groups]
    assert len(names) == 13
    assert [n for n in names if n != ROOT_GROUP] == GOLDEN_GROUPS
    assert ROOT_GROUP in names


def test_a2_wrapper_chain_stripped(golden_doc):
    doc, _ = golden_doc
    assert not any(
        p.startswith(("brain_node", "ros__parameters")) for p in
        (it.path for it in doc.items)
    )
    # 深层叶子存在且无前缀
    assert item(doc, "rl_brain.ball_vel.alpha").value == 0.5
    assert item(doc, "strategy.shoot.xmin").value == 0.3
    assert item(doc, "strategy.power_shoot.ymax").value == 0.3
    assert item(doc, "strategy.far_set_play_search.max_move_secs").value == 30.0


def test_a2_root_level_scalars(golden_doc):
    doc, _ = golden_doc
    root_items = [it for it in doc.items if it.group == ROOT_GROUP]
    assert [it.path for it in root_items] == ["enable_com", "game_control_ip"]


def test_a2_commented_out_lines_not_items(golden_doc):
    doc, _ = golden_doc
    vision = [it for it in doc.items if it.group == "vision"]
    # Y-5：L166-169、L174-175 共 6 行注释掉的备选不产生条目
    assert [it.path for it in vision] == [
        "vision.image_topic", "vision.depth_image_topic",
        "vision.cam_pixel_width", "vision.cam_pixel_height",
        "vision.cam_fov_x", "vision.cam_fov_y",
    ]
    # 同名键只有一个活动条目，且取活动行（realsense 值）而非注释行（d-robotics）
    assert sum(1 for it in doc.items if it.path == "vision.cam_pixel_width") == 1
    assert item(doc, "vision.cam_pixel_width").value == 1280.0
    assert sum(1 for it in doc.items if it.path == "vision.cam_fov_x") == 1
    assert item(doc, "vision.cam_fov_x").value == 90.0
    assert not any("image_left_raw" in it.raw_literal for it in doc.items)


def test_i1_paths_unique(golden_doc):
    doc, _ = golden_doc
    paths = [it.path for it in doc.items]
    assert len(paths) == len(set(paths))


def test_i2_value_matches_raw_literal(golden_doc):
    doc, _ = golden_doc
    for it in doc.items:
        raw = it.raw_literal
        if it.type == "str" and it.literal_style.quote in ("double", "single"):
            # 金样本带引号条目均无内部转义：去引号即值
            assert raw[0] == raw[-1] and raw[1:-1] == it.value, it.path
        else:
            kind, val = resolve_core(raw)
            assert kind == it.type and val == it.value, (it.path, raw)


def test_item_order_is_file_order(golden_doc):
    doc, _ = golden_doc
    paths = [it.path for it in doc.items]
    assert paths[0] == "game.team_id"
    assert paths[-1] == "sound.sound_pack"
    # enable_com（L150）在 rl_brain.ball_vel.* 之后、rerunLog 之前
    assert paths.index("enable_com") > paths.index("rl_brain.ball_vel.stale_hold")
    assert paths.index("enable_com") < paths.index("rerunLog.enable_tcp")


def test_types_and_styles_golden(golden_doc):
    doc, _ = golden_doc
    assert item(doc, "game.team_id").type == "int"
    assert item(doc, "ball_predictor.step_interval").raw_literal == "100."
    assert item(doc, "ball_predictor.step_interval").literal_style.float_form == "trailing_dot"
    assert item(doc, "robot.robot_height").raw_literal == "0.90"
    assert item(doc, "robot.robot_height").literal_style.float_form == ("fixed", 2)
    assert item(doc, "robot.odom_factor").literal_style.float_form == "plain"
    assert item(doc, "vision.cam_fov_x").raw_literal == "90.0"
    assert item(doc, "vision.cam_fov_x").literal_style.float_form == ("fixed", 1)
    assert item(doc, "enable_com").raw_literal == "True"
    assert item(doc, "enable_com").literal_style.bool_case == "capital"
    assert item(doc, "sound.enable").raw_literal == "true"
    assert item(doc, "sound.enable").literal_style.bool_case == "lower"
    assert item(doc, "game.field_type").raw_literal == '"adult_size"'
    assert item(doc, "game.field_type").literal_style.quote == "double"
    # Y-2：带引号一律 str（"看起来像 IP" 也是 str）
    ip = item(doc, "rerunLog.server_ip")
    assert ip.type == "str" and ip.value == "192.168.10.110:9876"
    assert item(doc, "rl_brain.own_color").value == ""
    assert item(doc, "rl_brain.own_color").raw_literal == '""'
    # raw_literal 不含行尾空格与注释（span 精确到 token）
    assert item(doc, "ball_predictor.step_cnt").raw_literal == "50"
    assert item(doc, "game.treat_person_as_robot").raw_literal == "false"
    # subgroup 恒 None（yaml v1）、doc.description 恒 None
    assert all(it.subgroup is None for it in doc.items)
    assert doc.description is None


# ---------------------------------------------------------------------------
# A-3 状态标记（§11）+ C-3
# ---------------------------------------------------------------------------


def test_a3_warning_marker(golden_doc):
    doc, _ = golden_doc
    it = item(doc, "game.treat_person_as_robot")
    assert it.warning is True
    assert "!!!!" in it.warning_reason_text
    assert "正式比赛时" in it.warning_reason_text  # 原文行
    # YC-7：yaml v1 无休眠
    assert all(not x.dormant for x in doc.items)


# ---------------------------------------------------------------------------
# YC-1 / YC-2 说明与组描述（§5.3）
# ---------------------------------------------------------------------------


def test_yc1_end_of_line_description(golden_doc):
    doc, _ = golden_doc
    alpha = item(doc, "rl_brain.ball_vel.alpha")
    assert len(alpha.description) == 1
    sec = alpha.description[0]
    assert sec.label is None
    assert sec.text == "低通系数: lp = alpha·v_raw + (1-alpha)·lp"
    assert alpha.description_provenance == PROV_COMMENT
    # 无注释条目 → 空说明、provenance none
    plain = item(doc, "game.number_of_players")
    assert plain.description == []
    assert plain.description_provenance == PROV_NONE
    # `#` 后无空格也算注释（L158 img_interval）
    img = item(doc, "rerunLog.img_interval")
    assert img.description and img.description[0].text.startswith("每多少帧图像")


def test_yc2_group_descriptions(golden_doc):
    doc, _ = golden_doc
    gd = {g.name: g.description for g in doc.groups}
    assert gd["rl_brain"] is not None
    assert gd["rl_brain"].startswith("RL Brain (obs_v1 观测")
    assert gd["rl_brain"].count("\n") == 5  # L116-121 共 6 行
    assert "readapt_v2" in gd["rl_brain"]
    assert gd["sound"] == "for fun"
    assert gd["game"] is None
    assert gd["vision"] is None  # 组键上方无紧邻注释块
    assert gd[ROOT_GROUP] is None


# ---------------------------------------------------------------------------
# A-6 推测枚举（§11、§5.3 YC-3/YC-4/YC-5）
# ---------------------------------------------------------------------------


def test_a6_enums(golden_doc):
    doc, diags = golden_doc
    expected = {
        "game.player_id": [1, 2, 3, 4, 5],
        "game.field_type": ["adult_size", "kid_size", "robo_league"],
        "game.player_role": ["striker", "goal_keeper"],
        "game.player_start_pos": ["right", "left"],
        "rl_brain.backend": ["policy_np", "libtorch", "onnx", "stub"],
        "rl_brain.goalie_mode": ["rl", "classic"],
        "rl_brain.mirror": ["off", "auto"],
    }
    for path, vals in expected.items():
        cands = item(doc, path).enum_candidates
        assert [c.value for c in cands] == vals, path
        assert all(c.provenance == PROV_INFERRED for c in cands), path
        # 类型与条目一致
        t = item(doc, path).type
        assert all(type(c.value).__name__ == t for c in cands), path
    # 金样本枚举条目恰 7 个（A-6：无多余条目被误触发枚举推测）
    with_enums = [it.path for it in doc.items if it.enum_candidates]
    assert sorted(with_enums) == sorted(expected)
    # label（A-6：goalie_mode 的 rl 带 label；mirror 的 off/auto 带 label）
    gm = {c.value: c.label for c in item(doc, "rl_brain.goalie_mode").enum_candidates}
    assert gm["rl"] == "守门员也走 RLDecide"
    assert gm["classic"] == "保留 GoalKeeperPenaltyKick 经典子树"
    mir = {c.value: c.label for c in item(doc, "rl_brain.mirror").enum_candidates}
    assert mir["off"] == "关 (默认, 真机定位已是队伍相对系)"
    assert mir["auto"] == "旧自动逻辑"
    # backend 首段剥括注+取冒号后；末段 `——` 截断
    be = {c.value: c.label for c in item(doc, "rl_brain.backend").enum_candidates}
    assert be["policy_np"] is None and "stub" in be


def test_a6_dropped(golden_doc):
    doc, diags = golden_doc
    drops = codes(diags, CODE_I_ENUM_DROPPED)
    assert all(d.severity == SEV_INFO for d in drops)
    # mirror 的 true（bool 与 str 条目不符）
    assert any(d.path == "rl_brain.mirror" and "true" in d.message for d in drops)
    # own_color 的 `空=未知`（CJK）；`我方队服颜色 red` 段也含空白/CJK 被丢弃
    own = [d for d in drops if d.path == "rl_brain.own_color"]
    assert len(own) == 2
    assert any("空=未知" in d.message for d in own)
    # own_color / opponent_theta_mode 不产生枚举
    assert item(doc, "rl_brain.own_color").enum_candidates == []
    assert item(doc, "rl_brain.opponent_theta_mode").enum_candidates == []
    # 诊断 path 不变量：非空必指向存在条目（§3.7）
    paths = {it.path for it in doc.items}
    assert all(d.path is None or d.path in paths for d in diags)


# ---------------------------------------------------------------------------
# Y-1 / §4.2 结构错误（合成用例）
# ---------------------------------------------------------------------------


def test_y1_multidoc(adapter, tmp_path):
    p = write(tmp_path, b"a: 1\n---\nb: 2\n")
    doc, diags = adapter.load(p)
    assert len(codes(diags, CODE_E_PARSE_MULTIDOC)) == 1
    assert diags[0].severity == SEV_ERROR or any(
        d.severity == SEV_ERROR for d in diags)
    assert doc.items == []
    assert doc.original_bytes == p.read_bytes()


def test_y1_multidoc_trailing_marker(adapter, tmp_path):
    p = write(tmp_path, b"a: 1\n---\n")
    doc, diags = adapter.load(p)
    assert codes(diags, CODE_E_PARSE_MULTIDOC)


def test_y1_toplevel_sequence(adapter, tmp_path):
    p = write(tmp_path, b"- 1\n- 2\n")
    doc, diags = adapter.load(p)
    assert codes(diags, CODE_E_PARSE)
    assert not codes(diags, CODE_E_PARSE_MULTIDOC)
    assert doc.items == []


def test_zero_byte_yaml_e_parse(adapter, tmp_path):
    # §4.2：0 字节 .yaml → 解析结果非 mapping，按 E-PARSE 出诊断
    p = write(tmp_path, b"")
    doc, diags = adapter.load(p)
    assert codes(diags, CODE_E_PARSE)
    assert doc.items == [] and doc.groups == []
    assert doc.original_bytes == b"" and doc.content_hash


def test_comment_only_e_parse(adapter, tmp_path):
    p = write(tmp_path, b"# nothing here\n")
    doc, diags = adapter.load(p)
    assert codes(diags, CODE_E_PARSE)


def test_syntax_error_e_parse(adapter, tmp_path):
    p = write(tmp_path, b"a: [1\nb: {\n")
    doc, diags = adapter.load(p)
    assert codes(diags, CODE_E_PARSE)


def test_e_encoding_non_utf8(adapter, tmp_path):
    p = write(tmp_path, b"a: \xff\xfeb\n")
    doc, diags = adapter.load(p)
    enc = codes(diags, CODE_E_ENCODING)
    assert len(enc) == 1 and enc[0].severity == SEV_ERROR
    assert doc.original_bytes == p.read_bytes()
    assert doc.items == []


def test_bom_stripped_for_parse_save_returns_bomless(adapter, tmp_path):
    p = write(tmp_path, b"\xef\xbb\xbfa: 1\n")
    doc, diags = adapter.load(p)
    assert not [d for d in diags if d.severity == SEV_ERROR]
    assert doc.original_bytes == p.read_bytes()          # 基线含 BOM
    assert item(doc, "a").value == 1                      # 解析只见 BOM-less
    out = adapter.save(doc, [])
    assert out == b"a: 1\n"                               # save 返回 BOM-less
    out2 = adapter.save(doc, [EditOp("a", 2)])
    assert out2 == b"a: 2\n"


def test_w_eol_mixed(adapter, tmp_path):
    p = write(tmp_path, b"a: 1\r\nb: 2\n")
    doc, diags = adapter.load(p)
    w = codes(diags, CODE_W_EOL_MIXED)
    assert len(w) == 1 and w[0].severity == SEV_WARNING
    assert adapter.save(doc, []) == b"a: 1\r\nb: 2\n"      # 逐行保留
    out = adapter.save(doc, [EditOp("b", 3)])
    assert out == b"a: 1\r\nb: 3\n"


def test_pure_crlf_no_warning(adapter, tmp_path):
    p = write(tmp_path, b"a: 1\r\nb: 2\r\n")
    doc, diags = adapter.load(p)
    assert not codes(diags, CODE_W_EOL_MIXED)
    assert adapter.save(doc, []) == b"a: 1\r\nb: 2\r\n"


# ---------------------------------------------------------------------------
# Y-6 序列 / 锚点（合成用例）
# ---------------------------------------------------------------------------


def test_y6_sequence_readonly(adapter, tmp_path):
    p = write(tmp_path, "s: [1, 2] # 列表说明\nb:\n  - x\n  - y\nk: 5\n".encode())
    doc, diags = adapter.load(p)
    s = item(doc, "s")
    assert s.readonly and s.readonly_reason == READONLY_CONTAINER
    assert s.type is None and s.value is None
    assert s.raw_literal == "[1, 2]"
    assert s.description and s.description[0].text == "列表说明"  # YC-1 也挂只读条目
    b = item(doc, "b")
    assert b.readonly and b.readonly_reason == READONLY_CONTAINER
    assert b.raw_literal.strip().endswith("- y")
    infos = codes(diags, CODE_I_SEQ_READONLY)
    assert len(infos) == 2 and all(d.severity == SEV_INFO for d in infos)
    assert item(doc, "k").value == 5 and not item(doc, "k").readonly


def test_y6_anchor_alias_readonly(adapter, tmp_path):
    src = "base: &a\n  k: 1\nchild:\n  sub: *a\nsc: &s 7\nsc2: *s\n"
    p = write(tmp_path, src.encode())
    doc, diags = adapter.load(p)
    for path in ("base", "child.sub", "sc", "sc2"):
        it = item(doc, path)
        assert it.readonly and it.readonly_reason == READONLY_ANCHOR, path
        assert it.type is None and it.value is None
    infos = codes(diags, CODE_I_ANCHOR_READONLY)
    assert {d.path for d in infos} == {"base", "child.sub", "sc", "sc2"}
    # 锚点子树不展开（base.k 不是条目）
    assert not any(it.path == "base.k" for it in doc.items)
    assert adapter.save(doc, []) == src.encode()


# ---------------------------------------------------------------------------
# Y-7 重复键（合成用例）
# ---------------------------------------------------------------------------


def test_y7_duplicate_key(adapter, tmp_path):
    # 裁决记录（2026-09-08）：原夹具 `top:\n  a: 1\n  b: 2\n  a: 3\n` 的根
    # mapping 恰一键 `top` 且值为 mapping → 按 Y-4 是包装键，被剥离后条目路径
    # 为 `a`/`b`，E-DUP-KEY 的 path='a' 恰是首现条目的完整路径——代码符合
    # §3.7（path 非空必指向已存在条目），是用例与 Y-4 冲突。为真正考察
    # 「诊断携带完整点分路径」，加第二个根键 `keep` 使 `top` 成为真实分组。
    src = b"top:\n  a: 1\n  b: 2\n  a: 3\nkeep: 0\n"
    p = write(tmp_path, src)
    doc, diags = adapter.load(p)
    dup = codes(diags, CODE_E_DUP_KEY)
    assert len(dup) == 1 and dup[0].severity == SEV_ERROR
    assert dup[0].path == "top.a"
    assert "第 4 行" in dup[0].message
    assert "top.a" in dup[0].message  # message 含可定位信息（§3.7）
    # 条目取首现
    a = item(doc, "top.a")
    assert a.value == 1 and a.raw_literal == "1"
    assert sum(1 for it in doc.items if it.path == "top.a") == 1
    # save 原样保留重复键（不改写）
    assert adapter.save(doc, []) == src
    # 编辑首现条目 → 只动第一处
    out = adapter.save(doc, [EditOp("top.a", 42)])
    assert out == b"top:\n  a: 42\n  b: 2\n  a: 3\nkeep: 0\n"


def test_y7_dup_key_wrapper_stripped_and_mapping_first(adapter, tmp_path):
    # (a) 单键根 mapping 被 Y-4 剥离：条目路径为 `a`/`b`，诊断 path='a'
    #     即首现条目完整路径，§3.7 不变量成立（原失败用例的真实语义）
    src = b"top:\n  a: 1\n  b: 2\n  a: 3\n"
    p = write(tmp_path, src)
    doc, diags = adapter.load(p)
    dup = codes(diags, CODE_E_DUP_KEY)
    assert len(dup) == 1 and dup[0].path == "a"
    assert item(doc, "a").value == 1 and not any(
        it.path == "top.a" for it in doc.items)
    assert adapter.save(doc, []) == src

    # (b) 首现是 mapping（mapping 本身不是条目）→ 无 path 'm' 的条目，
    #     诊断 path=None（§3.7：path 非空必指向已存在条目）；后现子树不展开
    src2 = b"m:\n  x: 1\nm:\n  y: 2\nk: 0\n"
    p2 = write(tmp_path, src2, name="t2.yaml")
    doc2, diags2 = adapter.load(p2)
    dup2 = codes(diags2, CODE_E_DUP_KEY)
    assert len(dup2) == 1 and dup2[0].path is None
    assert "'m'" in dup2[0].message
    assert [it.path for it in doc2.items] == ["m.x", "k"]
    assert all(d.path is None or d.path in {it.path for it in doc2.items}
               for d in diags2)
    assert adapter.save(doc2, []) == src2


# ---------------------------------------------------------------------------
# Y-9 块标量 / .nan / .inf（合成用例）
# ---------------------------------------------------------------------------


def test_y9_block_scalar_and_special_floats(adapter, tmp_path):
    src = "lit: |\n  hello\n  world\nfold: >\n  abc\nn: .nan\ni: .inf\nj: -.inf\nok: 1\n"
    p = write(tmp_path, src.encode())
    doc, diags = adapter.load(p)
    for path in ("lit", "fold", "n", "i", "j"):
        it = item(doc, path)
        assert it.readonly and it.readonly_reason == READONLY_NON_PLAIN_SCALAR, path
        assert it.type is None and it.value is None
    assert item(doc, "lit").raw_literal.startswith("|")
    assert item(doc, "n").raw_literal == ".nan"
    assert item(doc, "j").raw_literal == "-.inf"
    assert not item(doc, "ok").readonly
    assert adapter.save(doc, []) == src.encode()


def test_exotic_numeric_readonly(adapter, tmp_path):
    src = "h: 0x10\ne: 1e3\no: 0o17\nd: 12\n"
    p = write(tmp_path, src.encode())
    doc, diags = adapter.load(p)
    for path in ("h", "e", "o"):
        it = item(doc, path)
        assert it.readonly and it.readonly_reason == READONLY_NON_PLAIN_SCALAR, path
    assert item(doc, "d").type == "int" and not item(doc, "d").readonly


def test_null_and_tagged_scalar_readonly(adapter, tmp_path):
    src = "n:\ns: !!str 5\nt: 2021-01-01\n"
    p = write(tmp_path, src.encode())
    doc, diags = adapter.load(p)
    n = item(doc, "n")
    assert n.readonly and n.readonly_reason == READONLY_NON_PLAIN_SCALAR
    s = item(doc, "s")
    assert s.readonly and s.readonly_reason == READONLY_NON_PLAIN_SCALAR
    # 1.2 core：timestamp 样式即 str（裁量 1），可编辑
    t = item(doc, "t")
    assert not t.readonly and t.type == "str" and t.value == "2021-01-01"


def test_underscore_number_is_str_per_core(adapter, tmp_path):
    # YAML 1.2 core：`1_000` 非数值字面量 → str（裁量 1，登记于模块 docstring）
    p = write(tmp_path, b"u: 1_000\n")
    doc, diags = adapter.load(p)
    u = item(doc, "u")
    assert u.type == "str" and u.value == "1_000" and not u.readonly


# ---------------------------------------------------------------------------
# 点号键（§4.4 风险表）+ Y-4 包装链合成
# ---------------------------------------------------------------------------


def test_dotted_key_warning_and_save(adapter, tmp_path):
    src = "a.b: 5\nx:\n  y: 1\n"
    p = write(tmp_path, src.encode())
    doc, diags = adapter.load(p)
    w = [d for d in diags if d.code == "W-DOT-KEY"]
    assert len(w) == 1 and w[0].severity == SEV_WARNING
    assert w[0].path == "a.b"
    it = item(doc, "a.b")
    assert it.value == 5 and it.group == ROOT_GROUP
    # 写回走 locator：定位正确
    out = adapter.save(doc, [EditOp("a.b", 7)])
    assert out == b"a.b: 7\nx:\n  y: 1\n"


def test_y4_wrapper_chain_synthetic(adapter, tmp_path):
    src = "w1:\n  w2:\n    leaf: 3\n    grp:\n      deep: true\n"
    p = write(tmp_path, src.encode())
    doc, diags = adapter.load(p)
    assert [it.path for it in doc.items] == ["leaf", "grp.deep"]
    assert item(doc, "leaf").group == ROOT_GROUP  # 剥离后散落标量 → 根级
    assert item(doc, "grp.deep").group == "grp"
    assert [g.name for g in doc.groups] == [ROOT_GROUP, "grp"]


def test_y4_single_key_scalar_not_wrapper(adapter, tmp_path):
    p = write(tmp_path, b"only: 1\n")
    doc, diags = adapter.load(p)
    # 恰一键但值是标量 → 不是包装键，条目为根级散落标量
    assert [it.path for it in doc.items] == ["only"]
    assert item(doc, "only").group == ROOT_GROUP
    assert [g.name for g in doc.groups] == [ROOT_GROUP]


def test_y2_quoted_ip_str_synthetic(adapter, tmp_path):
    # Y-2 合成用例：带引号一律 str（"看起来像 IP:port" 也不做数值/IP 解析）；
    # 无引号的 `192.168.10.110` 按 1.2 core 亦为 str（多个 `.` 非数值字面量）
    p = write(tmp_path, b'ip: "192.168.10.110:9876"\nbare: 192.168.10.110\n')
    doc, diags = adapter.load(p)
    assert not [d for d in diags if d.severity == SEV_ERROR]
    ip = item(doc, "ip")
    assert ip.type == "str" and ip.value == "192.168.10.110:9876"
    assert ip.raw_literal == '"192.168.10.110:9876"'
    assert ip.literal_style.quote == "double"
    bare = item(doc, "bare")
    assert bare.type == "str" and bare.value == "192.168.10.110"
    assert bare.literal_style.quote == "none"


def test_quoted_keys_and_flow_mapping(adapter, tmp_path):
    src = '"a b": 1\nf: {x: 2, y: on}\n'
    p = write(tmp_path, src.encode())
    doc, diags = adapter.load(p)
    assert item(doc, "a b").value == 1          # 引号键解码
    assert item(doc, "f.x").value == 2          # flow mapping 递归展开
    # 1.2 core：`on` 是 str（非 1.1 bool）
    y = item(doc, "f.y")
    assert y.type == "str" and y.value == "on"


# ---------------------------------------------------------------------------
# YC-3 合成：触发/闸门
# ---------------------------------------------------------------------------


def test_yc3_synthetic_gates(adapter, tmp_path):
    src = (
        "m: 1 # 模式: fast=快速 | slow | 慢速段 | 1\n"      # slow 保留；慢速段 CJK 丢；1 与 int 型一致保留？
        "s: x # a b | c\n"                                    # 'a b' 空白丢，剩 1 个 → 无枚举
    )
    p = write(tmp_path, src.encode())
    doc, diags = adapter.load(p)
    m = item(doc, "m")
    # 段：`模式: fast=快速`(ii → fast/label 快速 → CJK 在 label 不影响；候选 fast 是 str 与 int 条目不符 → 丢)
    # `slow`(str 与 int 不符 → 丢)、`慢速段`(CJK → 丢)、`1`(int ✓)
    # 只剩 1 个 → 不产出枚举
    assert m.enum_candidates == []
    drops = [d for d in codes(diags, CODE_I_ENUM_DROPPED) if d.path == "m"]
    assert len(drops) == 3
    assert item(doc, "s").enum_candidates == []
    assert len([d for d in codes(diags, CODE_I_ENUM_DROPPED) if d.path == "s"]) == 1


def test_yc3_two_candidates_min(adapter, tmp_path):
    p = write(tmp_path, b"c: red # red | blue\n")
    doc, diags = adapter.load(p)
    assert [x.value for x in item(doc, "c").enum_candidates] == ["red", "blue"]
    assert not codes(diags, CODE_I_ENUM_DROPPED)


def test_c3_warning_synthetic(adapter, tmp_path):
    p = write(tmp_path, "a: 1 # !! 危险 !!\nb: 2 # 只有一个 ! 标记\nc: 3 # x!!y\n".encode())
    doc, diags = adapter.load(p)
    assert item(doc, "a").warning is True
    assert "!!" in item(doc, "a").warning_reason_text
    assert item(doc, "b").warning is False
    # `x!!y`：! 前后是字母 → 不触发（C-3 前后非字母数字）
    assert item(doc, "c").warning is False


# ---------------------------------------------------------------------------
# save 错误路径（§3.8/§4.1）
# ---------------------------------------------------------------------------


def test_save_unknown_path_raises(adapter, golden_doc):
    doc, _ = golden_doc
    with pytest.raises(Exception):
        adapter.save(doc, [EditOp("no.such.path", 1)])


def test_save_readonly_raises(adapter, tmp_path):
    p = write(tmp_path, b"s: [1, 2]\n")
    doc, diags = adapter.load(p)
    with pytest.raises(Exception):
        adapter.save(doc, [EditOp("s", "x")])


def test_save_locator_stale_raises(adapter, tmp_path):
    p = write(tmp_path, b"a: 1\n")
    doc, diags = adapter.load(p)
    # 基线被外部替换而未重新 load，span 处已不是原 token → 程序错误
    doc.original_bytes = b"b: 9\na: 1\n"
    with pytest.raises(RuntimeError):
        adapter.save(doc, [EditOp("a", 5)])
