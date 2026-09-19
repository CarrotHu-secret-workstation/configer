"""G2a：提交编排纯层测试（ui/commit.py）——假时钟防抖全分支 + 结果映射 + 控件选型。

零 nicegui 依赖、零事件循环；DebounceManager 用注入假时钟推进（无 sleep）。
"""

from __future__ import annotations

from configer.core.session import CommitResult, PendingResult
from configer.model import (
    PROV_DECLARED,
    PROV_INFERRED,
    ConfigItem,
    EnumCandidate,
    RangeConstraint,
)
from configer.ui.commit import (
    DEBOUNCE_MS,
    DebounceManager,
    commit_action,
    control_kind,
    enum_mode,
    marker_style,
    pending_feedback,
    range_hint_text,
    text_widget_value,
)


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, ms: float) -> None:
        self.t += ms


# ---------------------------------------------------------------------------
# DebounceManager（假时钟全分支）
# ---------------------------------------------------------------------------


def test_debounce_single_keystroke_fires_once_after_400ms():
    clock = FakeClock()
    dm = DebounceManager(clock=clock)
    dm.keystroke("a.b", "1")
    clock.advance(DEBOUNCE_MS - 1)
    assert dm.poll() == []           # 399ms：未到期
    clock.advance(1)
    assert dm.poll() == [("a.b", "1")]  # 恰 400ms：到期恰一次
    assert dm.poll() == []           # 取出即清除，不重复产出
    assert not dm.is_pending("a.b")


def test_debounce_consecutive_keystrokes_merge_to_last_value_once():
    clock = FakeClock()
    dm = DebounceManager(clock=clock)
    for i, text in enumerate(["1", "12", "123"]):
        dm.keystroke("n", text)
        clock.advance(200)           # 每次都在窗口内重置计时
    assert dm.poll() == []           # 最后键击后仅 200ms
    clock.advance(DEBOUNCE_MS)
    assert dm.poll() == [("n", "123")]  # 合并为最后一次值、恰一次


def test_debounce_flush_on_blur_before_expiry():
    clock = FakeClock()
    dm = DebounceManager(clock=clock)
    dm.keystroke("x", "abc")
    clock.advance(100)
    assert dm.flush("x") == ("x", "abc")  # 失焦立即产出，不等防抖
    assert dm.flush("x") is None          # 无计时 → None
    clock.advance(DEBOUNCE_MS)
    assert dm.poll() == []                # flush 后不再重复产出


def test_debounce_flush_all_on_file_switch():
    clock = FakeClock()
    dm = DebounceManager(clock=clock)
    dm.keystroke("p1", "v1")
    clock.advance(50)
    dm.keystroke("p2", "v2")
    out = dm.flush_all()
    assert out == [("p1", "v1"), ("p2", "v2")]  # 插入序稳定
    assert dm.pending_items() == []
    clock.advance(DEBOUNCE_MS * 2)
    assert dm.poll() == []


def test_debounce_multiple_entries_independent():
    clock = FakeClock()
    dm = DebounceManager(clock=clock)
    dm.keystroke("a", "1")
    clock.advance(300)
    dm.keystroke("b", "2")             # b 晚 300ms 起表
    clock.advance(100)                 # a 到期（400），b 仅 100
    assert dm.poll() == [("a", "1")]   # 互不串扰
    assert dm.is_pending("b")
    clock.advance(300)
    assert dm.poll() == [("b", "2")]


def test_debounce_cancel_drops_without_commit():
    dm = DebounceManager(clock=FakeClock())
    dm.keystroke("z", "9")
    dm.cancel("z")
    assert dm.flush("z") is None
    dm.cancel("never-typed")           # 不存在的条目：防御不抛


def test_debounce_default_clock_constructs():
    dm = DebounceManager()             # 默认单调毫秒时钟（运行期路径）
    dm.keystroke("k", "v")             # 不注入 now → 走系统时钟不抛
    assert dm.is_pending("k")
    assert dm.poll() == []             # 刚键入必然未到期


# ---------------------------------------------------------------------------
# CommitResult → UI 动作映射（§7.5 第 5 步）
# ---------------------------------------------------------------------------


def test_commit_action_refresh_statuses():
    for status in ("committed", "reloaded", "noop"):
        action = commit_action(CommitResult(status=status, path="a"))
        assert action.kind == "refresh"
        assert action.message is None and action.retry_path is None


def test_commit_action_failed_is_retryable_error():
    action = commit_action(
        CommitResult(status="failed", path="a.b", reason="磁盘只读"))
    assert action.kind == "error"
    assert action.retry_path == "a.b"
    assert "磁盘只读" in action.message and "可重试" in action.message


def test_commit_action_failed_without_reason_has_default_text():
    action = commit_action(CommitResult(status="failed", path="a"))
    assert action.kind == "error" and action.message


def test_commit_action_conflict_and_gone_carry_message():
    c = commit_action(CommitResult(status="conflict", path="a", reason="基线不一致"))
    assert c.kind == "conflict" and c.message == "基线不一致"
    g = commit_action(CommitResult(status="gone", path="a"))
    assert g.kind == "gone" and g.message  # 默认文案兜底


def test_commit_action_paused_is_silent():
    assert commit_action(CommitResult(status="paused", path="a")).kind == "silent"


# ---------------------------------------------------------------------------
# PendingResult → 门控标记映射（U-6 即时反馈）
# ---------------------------------------------------------------------------


def test_pending_feedback_levels():
    assert pending_feedback(PendingResult(True, "ok", value=3)).marker == "none"
    w = pending_feedback(PendingResult(True, "warn", reason="超出推测范围"))
    assert (w.marker, w.reason) == ("warn", "超出推测范围")
    for level in ("block", "invalid"):
        e = pending_feedback(PendingResult(False, level, reason="类型不符"))
        assert (e.marker, e.reason) == ("error", "类型不符")


def test_pending_feedback_intermediate_keeps_marker():
    """中间态（'-'、空串）无骚扰：不设新标记也不清旧标记（裁量登记）。"""
    fb = pending_feedback(PendingResult(False, "intermediate", reason="输入不完整"))
    assert fb.marker == "keep" and fb.reason is None


# ---------------------------------------------------------------------------
# 控件选型（U-6 映射矩阵）
# ---------------------------------------------------------------------------


def _item(**kw) -> ConfigItem:
    base = dict(path="p", group="g", type="int", value=1, raw_literal="1")
    base.update(kw)
    return ConfigItem(**base)


def test_control_kind_matrix():
    assert control_kind(_item(type="bool", value=True)) == "bool"
    assert control_kind(_item(type="int")) == "text"
    assert control_kind(_item(type="float", value=1.5)) == "text"
    assert control_kind(_item(type="str", value="s")) == "text"
    assert control_kind(
        _item(enum_candidates=[EnumCandidate(1, provenance=PROV_INFERRED)])
    ) == "enum"
    assert control_kind(_item(readonly=True, readonly_reason="container")) == "readonly"
    # 防御：type=None（只读容器）即使未标 readonly 也走只读展示
    assert control_kind(_item(type=None, value=None)) == "readonly"
    # readonly 优先于枚举候选
    assert control_kind(
        _item(readonly=True, enum_candidates=[EnumCandidate(1)])
    ) == "readonly"


def test_enum_mode_inferred_vs_declared():
    assert enum_mode(_item()) is None  # 无候选
    inferred = _item(enum_candidates=[
        EnumCandidate(1, provenance=PROV_INFERRED),
        EnumCandidate(2, "二号", provenance=PROV_INFERRED),
    ])
    assert enum_mode(inferred) == "inferred"   # YC-6：允许自定义输入
    declared = _item(enum_candidates=[
        EnumCandidate("a", provenance=PROV_DECLARED)])
    assert enum_mode(declared) == "declared"   # 仅限列表内
    # 防御：混合 provenance（§3.4 禁止并存）按 declared 收紧
    mixed = _item(enum_candidates=[
        EnumCandidate(1, provenance=PROV_INFERRED),
        EnumCandidate(2, provenance=PROV_DECLARED),
    ])
    assert enum_mode(mixed) == "declared"


def test_range_hint_text():
    assert range_hint_text(_item()) is None  # 无 range
    both = _item(range=RangeConstraint(min=0.05, max=0.30, unit="m",
                                       provenance=PROV_INFERRED))
    assert range_hint_text(both) == "范围 [0.05, 0.3] m"
    lo = _item(range=RangeConstraint(min=1, provenance=PROV_DECLARED))
    assert range_hint_text(lo) == "范围 ≥ 1"      # 单边界只显示存在侧
    hi = _item(range=RangeConstraint(max=50, provenance=PROV_DECLARED))
    assert range_hint_text(hi) == "范围 ≤ 50"


def test_text_widget_value():
    # str 去引号显示（parse 契约接受裸文本，含空串）
    s = _item(type="str", value="adult_size", raw_literal='"adult_size"')
    assert text_widget_value(s) == "adult_size"
    empty = _item(type="str", value="", raw_literal='""')
    assert text_widget_value(empty) == ""
    # 数字规范化显示；落盘风格由 §7.2 重放负责
    f = _item(type="float", value=100.0, raw_literal="100.")
    assert text_widget_value(f) == "100.0"
    i = _item(type="int", value=3, raw_literal="3")
    assert text_widget_value(i) == "3"
    # 防御：value=None → 回退 raw_literal 原文
    n = _item(type="int", value=None, raw_literal="?")
    assert text_widget_value(n) == "?"


def test_marker_style():
    assert "#dc2626" in marker_style("error")   # 红边框
    assert "#d97706" in marker_style("warn")    # 黄边框
    assert marker_style("none") == ""
    assert marker_style("keep") == ""           # 中间态不改样式
    assert marker_style("whatever") == ""       # 未知值防御


def test_marker_class_and_combine():
    from configer.ui.commit import combine_marker, marker_class

    assert marker_class("error") == "gate-error"
    assert marker_class("warn") == "gate-warn"
    assert marker_class("none") == "" and marker_class("keep") == ""

    dormant = _item(dormant=True)
    warning = _item(warning=True, warning_reason_text="原文")
    plain = _item()
    err = pending_feedback(PendingResult(False, "block", reason="越界"))
    warn = pending_feedback(PendingResult(True, "warn", reason="超出推测范围"))
    # 门控红标压倒一切（含 dormant/warning 持续黄标）
    assert combine_marker(err, dormant) == ("error", "越界")
    # 门控黄标带原因
    assert combine_marker(warn, plain) == ("warn", "超出推测范围")
    # dormant/warning → 持续黄标，reason 留空（原文在 _flags 段展示，防双写）
    assert combine_marker(None, dormant) == ("warn", None)
    assert combine_marker(None, warning) == ("warn", None)
    # ok（None）且无状态标志 → 无标记
    assert combine_marker(None, plain) == ("none", None)


# ---------------------------------------------------------------------------
# GuiState 提交编排接线（无头 + 真实 SessionManager + 假时钟防抖）
# ---------------------------------------------------------------------------

import textwrap

from configer.core.session import SessionManager
from configer.ui.state import GuiState

FIXTURE_YAML = textwrap.dedent('''\
    # 顶
    game:
      player_id: 3  # 1|2|3
      enabled: true
      name: "cup"
      ratio: 0.5
      mode: "x"  # a|b
      table: [1, 2]
    misc:
      keep: 1
''')
# 注：misc 节的存在防止 game 被判为包装链剥离（Y-4）——path 保留 game. 前缀。


def _make_state(tmp_path, clock=None, name="c.yaml", content=FIXTURE_YAML):
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    mgr = SessionManager()
    st = GuiState(mgr)
    st.register_results(mgr.open_files([f]))
    st.activate(f.resolve())
    if clock is not None:
        st.debounce = DebounceManager(clock=clock)
    return mgr, st, f


def test_state_keystroke_debounce_commit_end_to_end(tmp_path):
    """A-13：文本类最后键击 400ms 后自动落盘；窗口内不提交。"""
    clock = FakeClock()
    mgr, st, f = _make_state(tmp_path, clock)
    try:
        fb = st.on_text_keystroke("game.player_id", "2")
        assert fb.marker == "none"                       # ok（候选内）：无标记
        assert mgr.get(f.resolve()).has_pending("game.player_id")
        clock.advance(DEBOUNCE_MS - 1)
        st.drain_debounce()
        assert "player_id: 3" in f.read_text(encoding="utf-8")  # 未到期不落盘
        clock.advance(1)
        st.drain_debounce()                              # 到期恰一次提交
        assert "player_id: 2" in f.read_text(encoding="utf-8")
        session = mgr.get(f.resolve())
        assert session.undo_depth() == 1
        assert st.last_commit_echo == "player_id: 3 → 2"  # U-7 回显（已实现）
        assert st.gate_markers == {}                      # 提交成功清标记
        st.drain_debounce()                              # 不重复产出
        assert session.undo_depth() == 1
    finally:
        mgr.stop_all()


def test_state_consecutive_keystrokes_single_commit(tmp_path):
    clock = FakeClock()
    mgr, st, f = _make_state(tmp_path, clock)
    try:
        for text in ("1", "12", "123"):
            st.on_text_keystroke("game.player_id", text)
            clock.advance(200)
        clock.advance(DEBOUNCE_MS)
        st.drain_debounce()
        assert "player_id: 123" in f.read_text(encoding="utf-8")
        assert mgr.get(f.resolve()).undo_depth() == 1    # 合并为一次提交
    finally:
        mgr.stop_all()


def test_state_invalid_and_intermediate_not_committed(tmp_path):
    """A-9：非整数 → 红标、防抖窗口后磁盘字节不变；中间态无骚扰。"""
    clock = FakeClock()
    mgr, st, f = _make_state(tmp_path, clock)
    try:
        before = f.read_bytes()
        fb = st.on_text_keystroke("game.player_id", "abc")
        assert fb.marker == "error" and fb.reason        # 红标 + 原因
        fb2 = st.on_text_keystroke("game.player_id", "-")   # 中间态 → keep
        assert fb2.marker == "error"                     # 保持旧红标不清除
        clock.advance(DEBOUNCE_MS * 2)
        st.drain_debounce()
        assert f.read_bytes() == before                  # 硬校验不落盘
        assert not mgr.get(f.resolve()).has_pending("game.player_id")
        assert st.gate_markers["game.player_id"].marker == "error"
        assert st.last_commit_echo is None
    finally:
        mgr.stop_all()


def test_state_enum_out_of_candidates_warns_but_commits(tmp_path):
    """YC-6/A-9：inferred 枚举列表外值 → 黄标、照常提交落盘。"""
    clock = FakeClock()
    mgr, st, f = _make_state(tmp_path, clock)
    try:
        fb = st.on_text_keystroke("game.player_id", "9")
        assert fb.marker == "warn" and fb.reason         # 黄标 + 原因
        clock.advance(DEBOUNCE_MS)
        st.drain_debounce()
        assert "player_id: 9" in f.read_text(encoding="utf-8")  # 照常落盘
        assert st.gate_markers == {}                     # 提交成功清门控标记
    finally:
        mgr.stop_all()


def test_state_bool_click_commits_immediately(tmp_path):
    """A-13：点击类（bool）值变化即时落盘，不经防抖。"""
    mgr, st, f = _make_state(tmp_path)
    try:
        result = st.commit_click_now("game.enabled", checked=False)
        assert result.status == "committed"
        assert "enabled: false" in f.read_text(encoding="utf-8")
        assert st.debounce.pending_items() == []         # 不经防抖
        assert mgr.get(f.resolve()).undo_depth() == 1
    finally:
        mgr.stop_all()


def test_state_enum_candidate_click_commits_immediately(tmp_path):
    mgr, st, f = _make_state(tmp_path)
    try:
        result = st.commit_click_now("game.player_id", text="2")
        assert result.status == "committed"
        assert "player_id: 2" in f.read_text(encoding="utf-8")
        assert st.debounce.pending_items() == []
    finally:
        mgr.stop_all()


def test_state_str_edit_debounced(tmp_path):
    clock = FakeClock()
    mgr, st, f = _make_state(tmp_path, clock)
    try:
        st.on_text_keystroke("game.name", "kid_size")    # 裸文本（去引号契约）
        clock.advance(DEBOUNCE_MS)
        st.drain_debounce()
        assert 'name: "kid_size"' in f.read_text(encoding="utf-8")
        assert st.last_commit_echo == "name: cup → kid_size"
    finally:
        mgr.stop_all()


def test_state_flush_on_select_item_switch(tmp_path):
    """§7.5 第 3 步：切换选中条目 = 旧条目失焦 → 立即提交不等防抖。"""
    clock = FakeClock()
    mgr, st, f = _make_state(tmp_path, clock)
    try:
        st.select_item("game.player_id")
        st.on_text_keystroke("game.player_id", "5")
        st.select_item("game.enabled")                   # 切换条目
        assert "player_id: 5" in f.read_text(encoding="utf-8")
        assert st.debounce.pending_items() == []
        clock.advance(DEBOUNCE_MS * 2)
        st.drain_debounce()                              # 不重复提交
        assert mgr.get(f.resolve()).undo_depth() == 1
    finally:
        mgr.stop_all()


def test_state_flush_on_activate_switch(tmp_path):
    """切换活动文件 → flush 旧文件计时中的暂态（§7.5 第 3 步）。"""
    clock = FakeClock()
    f1 = tmp_path / "a.yaml"
    f1.write_text(FIXTURE_YAML, encoding="utf-8")
    f2 = tmp_path / "b.yaml"
    f2.write_text(FIXTURE_YAML, encoding="utf-8")
    mgr = SessionManager()
    st = GuiState(mgr)
    st.debounce = DebounceManager(clock=clock)
    try:
        st.register_results(mgr.open_files([f1, f2]))
        st.activate(f1.resolve())
        st.on_text_keystroke("game.player_id", "4")
        st.activate(f2.resolve())                        # 切换文件
        assert "player_id: 4" in f1.read_text(encoding="utf-8")  # 旧文件已落盘
        assert "player_id: 3" in f2.read_text(encoding="utf-8")  # 新文件不受影响
        assert st.debounce.pending_items() == []
        assert st.gate_markers == {}                     # 标记按文件隔离
    finally:
        mgr.stop_all()


def test_state_undo_redo_toolbar_actions(tmp_path):
    """U-7/A-14：undo/redo 落盘；栈空 → noop 静默。"""
    mgr, st, f = _make_state(tmp_path)
    try:
        assert st.do_undo().status == "noop"             # 栈空
        assert st.do_redo().status == "noop"
        assert st.commit_error is None                   # noop 不进错误条
        st.commit_click_now("game.enabled", checked=False)
        assert "enabled: false" in f.read_text(encoding="utf-8")
        result = st.do_undo()
        assert result.status == "committed"
        assert "enabled: true" in f.read_text(encoding="utf-8")  # 反向已落盘
        assert mgr.get(f.resolve()).redo_depth() == 1
        result = st.do_redo()
        assert result.status == "committed"
        assert "enabled: false" in f.read_text(encoding="utf-8")
    finally:
        mgr.stop_all()


def test_state_undo_flushes_debounce_first(tmp_path):
    """撤销前先 flush 防抖暂态（裁量登记：flush 使键入值成为最新 undo 条目，
    do_undo 随即撤销的正是该条——与真实 UI 中"点工具栏按钮先触发输入框
    blur→flush 再 undo"的顺序一致，语义统一为"撤销最近一次已提交编辑"）。"""
    clock = FakeClock()
    mgr, st, f = _make_state(tmp_path, clock)
    try:
        st.commit_click_now("game.enabled", checked=False)   # undo 栈 1 条
        st.on_text_keystroke("game.player_id", "4")          # 计时中
        result = st.do_undo()
        assert result.status == "committed"
        assert result.path == "game.player_id"               # 撤销的是 flush 提交
        text = f.read_text(encoding="utf-8")
        assert "player_id: 3" in text and "enabled: false" in text
        assert st.debounce.pending_items() == []
        assert mgr.get(f.resolve()).redo_depth() == 1
    finally:
        mgr.stop_all()


def test_state_apply_commit_result_hook_dispatch(tmp_path):
    """结果分流：failed→错误条可重试；conflict/gone→G2b 钩子；paused→静默。"""
    mgr, st, f = _make_state(tmp_path)
    try:
        st.apply_commit_result(
            CommitResult(status="failed", path="game.name", reason="磁盘只读"))
        assert "磁盘只读" in st.commit_error and st.commit_error_retry == "game.name"

        seen = []
        st.on_conflict = seen.append
        st.commit_error = None
        cr = CommitResult(status="conflict", path="game.name", reason="基线不一致")
        st.apply_commit_result(cr)
        assert seen == [cr] and st.commit_error is None  # 钩子接管，无降级
        st.on_conflict = None
        st.apply_commit_result(cr)
        assert st.commit_error and "基线不一致" in st.commit_error  # 未接线降级
        assert st.commit_error_retry is None             # 冲突重试无意义

        gone_seen = []
        st.on_gone = gone_seen.append
        st.commit_error = None
        cr_gone = CommitResult(status="gone", path="game.name")
        st.apply_commit_result(cr_gone)
        assert gone_seen == [cr_gone] and st.commit_error is None

        st.commit_error = None
        st.apply_commit_result(CommitResult(status="paused", path="game.name"))
        assert st.commit_error is None                   # 静默
    finally:
        mgr.stop_all()


def test_state_retry_commit_after_failure(tmp_path):
    """§7.5 第 5 步：failed 暂态保留，错误条重试 → 再次 commit_pending。"""
    mgr, st, f = _make_state(tmp_path)
    try:
        st.on_text_keystroke("game.player_id", "4")      # 进入暂态
        st.debounce.cancel("game.player_id")             # 模拟"到期前曾失败"现场
        st.commit_error = "提交失败：模拟（可重试）"
        st.commit_error_retry = "game.player_id"
        result = st.retry_commit()
        assert result.status == "committed"
        assert "player_id: 4" in f.read_text(encoding="utf-8")
        assert st.commit_error is None and st.commit_error_retry is None
        # 无可重试条目 → None 防御
        assert st.retry_commit() is None
    finally:
        mgr.stop_all()


def test_state_unknown_path_defense(tmp_path):
    """条目重载后消失（gone）：ValueError 防御——丢弃计时与标记不抛。"""
    mgr, st, f = _make_state(tmp_path)
    try:
        assert st.on_text_keystroke("no.such.item", "1") is None
        assert st.commit_text_now("no.such.item", "1") is None
        assert st.commit_click_now("no.such.item", text="1") is None
        st.flush_debounce_all()                          # 空 flush 不抛
    finally:
        mgr.stop_all()


def test_build_gui_renders_all_control_kinds_headless(tmp_path):
    """无头冒烟（G1 模式）：U-6 控件矩阵各类条目 detail 构建不抛。

    覆盖 enum（候选内现值 / 列表外现值防御项）、bool、str/float 文本、
    readonly 容器（type=None）。pytest 内绝不真起 ui.run。
    """
    from configer.ui.layout import build_gui

    f = tmp_path / "c.yaml"
    f.write_text(FIXTURE_YAML, encoding="utf-8")
    mgr = SessionManager()
    try:
        for item_path in ("game.player_id", "game.enabled", "game.name",
                          "game.ratio", "game.mode", "game.table"):
            st = GuiState(mgr)
            st.register_results(mgr.open_files([f]))
            st.activate(f.resolve())
            st.selected_item = item_path
            build_gui(st)
            assert st.ui_refresh is not None
            st.ui_refresh()                              # core.loop=None 防御跳过
    finally:
        mgr.stop_all()
