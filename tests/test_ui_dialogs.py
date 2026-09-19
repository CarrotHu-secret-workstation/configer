"""G2b：横幅/冲突模态（U-9）+ 关闭文件（U-12）+ 退出 flush（§7.7）+ 快捷键（U-11）。

三层结构：
1. 纯层（ui/dialogs.py）——决策树全分支/文案关键句/注册表，零 nicegui；
2. 编排层（GuiState + 真实 SessionManager + tmp 文件）——冲突处置、关闭
   分流、退出 flush、横幅状态机、非交互退出、beforeunload 守卫；
3. 端到端——真实 poller 事件 → 横幅；金样本拷贝（testdata/config.yaml）
   提交撞冲突 → reload 后 doc=外部内容；关闭/退出前暂态落盘（A-11/A-15/A-16）。

纪律：pytest 进程内绝不真起 ui.run；无头构建冒烟照 G1/G2a 模式。
"""

from __future__ import annotations

import shutil
import textwrap
import time
from pathlib import Path

import pytest

from configer.core.poller import FileState
from configer.core.session import (
    CloseResult,
    CommitResult,
    FlushResult,
    SessionManager,
)
from configer.ui import dialogs
from configer.ui.commit import DebounceManager
from configer.ui.dialogs import (
    ConflictRequest,
    action_scope,
    apply_external_event,
    banner_for_event,
    banner_row_text,
    blocked_lines_of,
    close_item_lines,
    close_plan,
    conflict_dialog_texts,
    flush_exit_plan,
    force_warning_text,
    keybinding_action,
    nav_move,
    nav_paths,
    noninteractive_exit_code,
)
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
# 注：misc 节防止 game 被判为包装链剥离（Y-4，G2a 裁量 11 沿用）。

TESTDATA = Path(__file__).resolve().parent.parent / "testdata"


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, ms: float) -> None:
        self.t += ms


def _make_state(tmp_path, clock=None, name="c.yaml", content=FIXTURE_YAML):
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    mgr = SessionManager()
    st = GuiState(mgr)
    st.notes = []                       # type: ignore[attr-defined]
    st.notify_fn = lambda msg, kind="positive": st.notes.append((msg, kind))
    st.register_results(mgr.open_files([f]))   # G2c：登记即绑 per-path 回调
    st.ensure_state_callbacks()         # 安全网（幂等，不再触碰私有属性）
    st.activate(f.resolve())
    if clock is not None:
        st.debounce = DebounceManager(clock=clock)
    return mgr, st, f


def _make_conflict(tmp_path):
    """真实冲突现场：打开后外部改文件 → 点击类提交撞 §7.6 基线校验。"""
    mgr, st, f = _make_state(tmp_path)
    f.write_text(FIXTURE_YAML.replace("player_id: 3", "player_id: 5"),
                 encoding="utf-8")                    # 外部修改（不经 configer）
    st.commit_click_now("game.enabled", checked=False)  # → conflict
    return mgr, st, f


def _item_value(session, path):
    for it in session.doc.items:
        if it.path == path:
            return it.value
    raise AssertionError(f"条目 {path} 不在 doc 中")


# ===========================================================================
# 1. 纯层：U-9 横幅状态机
# ===========================================================================


def test_banner_texts_modified_vs_gone_are_distinct():
    """§7.6 硬性要求：gone 横幅文案必须区别于"已在磁盘上被修改"。"""
    mod = banner_for_event("external_modified")
    gone = banner_for_event("gone")
    assert mod == dialogs.BANNER_MODIFIED
    assert "已在磁盘上被修改" in mod
    assert gone is not None and gone != mod
    assert "删除" in gone and "不可读" in gone and "只读" in gone
    assert banner_for_event("recovered") == dialogs.BANNER_RECOVERED
    assert banner_for_event("bogus_event") is None    # 未知事件防御


def test_apply_external_event_state_machine():
    key = Path("/x/a.yaml")
    banners: dict = {}
    banners = apply_external_event(banners, key, "external_modified")
    assert banners[key] == dialogs.BANNER_MODIFIED
    # 多文件各自横幅（key 区分，互不覆盖）
    key2 = Path("/x/b.yaml")
    banners = apply_external_event(banners, key2, "gone")
    assert banners[key] == dialogs.BANNER_MODIFIED
    assert banners[key2].startswith(dialogs.BANNER_GONE)
    # recovered → 撤该文件横幅（只读态解除由 session 承载）
    banners = apply_external_event(banners, key2, "recovered")
    assert key2 not in banners and key in banners
    # 未知事件 → 原样；入参不被就地修改（纯函数）
    frozen = dict(banners)
    assert apply_external_event(banners, key, "??") == frozen
    assert banners == frozen


def test_banner_row_text_contains_filename():
    assert banner_row_text("param.py", "文件已在磁盘上被修改") == (
        "param.py：文件已在磁盘上被修改")


# ===========================================================================
# 1. 纯层：§7.6 冲突模态文案
# ===========================================================================


def test_conflict_dialog_texts_carry_file_and_reason():
    req = ConflictRequest(file=Path("/x/a.yaml"), file_name="a.yaml",
                          reason="基线不一致", source="commit",
                          item_path="game.player_id")
    texts = conflict_dialog_texts(req)
    assert texts["title"] == dialogs.CONFLICT_TITLE
    assert "a.yaml" in texts["body"] and "基线不一致" in texts["body"]
    assert "game.player_id" in texts["body"]
    assert texts["reload"] == "重新加载" and texts["force"] == "强制覆盖"
    assert texts["cancel_exit"] == "取消退出"
    assert "undo/redo 栈同时清空" in texts["reload_hint"]  # 重载语义提示


def test_force_warning_states_permanent_loss_and_no_undo():
    """§7.6 硬性要求：二次警示必须明示"永久丢失外部修改"且"无法通过撤销找回"。"""
    text = force_warning_text("config.yaml")
    assert "config.yaml" in text
    assert "永久丢失" in text
    assert "外部修改" in text
    assert "不在本文件撤销栈内" in text
    assert "无法通过撤销找回" in text


# ===========================================================================
# 1. 纯层：U-12 关闭分流
# ===========================================================================


def test_close_plan_all_branches():
    assert close_plan(CloseResult(status="closed")).kind == "removed"

    nc = CloseResult(status="need_confirm",
                     items=[("game.player_id", "文件不可读")])
    action = close_plan(nc)
    assert action.kind == "confirm_discard"
    assert action.lines == ("game.player_id：文件不可读",)

    cf = CloseResult(status="conflict", items=[("a", "基线冲突")])
    assert close_plan(cf).kind == "conflict_modal"

    fa = CloseResult(status="failed", items=[("a", "权限"), ("b", "I/O")])
    action = close_plan(fa)
    assert action.kind == "failed_dialog"
    assert action.lines == ("a：权限", "b：I/O")
    assert close_item_lines([]) == ()


# ===========================================================================
# 1. 纯层：§7.7 退出 flush 决策树（全分支）
# ===========================================================================


def test_exit_plan_all_clean_exits_zero():
    results = {
        Path("/a"): FlushResult(status="clean", committed=["x"]),
        Path("/b"): FlushResult(status="clean"),
    }
    plan = flush_exit_plan(results)
    assert plan.action == "exit_clean" and plan.exit_code == 0
    assert flush_exit_plan({}).action == "exit_clean"   # 空会话也走 0


def test_exit_plan_conflict_shows_modal_first():
    p = Path("/a")
    results = {p: FlushResult(status="conflict", conflicts=["x.y"])}
    plan = flush_exit_plan(results)
    assert plan.action == "show_conflict"
    assert plan.conflict_file is p


def test_exit_plan_paused_follows_conflict_path():
    """裁量：paused 文件按 conflict 路径处理（其暂态因冲突未决滞留）。"""
    p = Path("/a")
    results = {p: FlushResult(
        status="paused", failures=[("x.y", "该文件已暂停自动提交（冲突未决）")])}
    plan = flush_exit_plan(results)
    assert plan.action == "show_conflict" and plan.conflict_file is p
    assert plan.conflict_reason == "该文件已暂停自动提交（冲突未决）"


def test_exit_plan_failed_asks_with_exit3():
    p = Path("/a")
    results = {p: FlushResult(status="failed",
                              failures=[("x.y", "磁盘只读")])}
    plan = flush_exit_plan(results)
    assert plan.action == "ask_failed" and plan.exit_code == 3
    assert plan.failed == ((p, ("x.y：磁盘只读",)),)


def test_exit_plan_conflict_outranks_failed_and_blocked():
    """混合多文件：conflict > failed > blocked > clean（优先级稳定）。"""
    pc, pf = Path("/c"), Path("/f")
    results = {
        pf: FlushResult(status="failed", failures=[("i", "io")]),
        pc: FlushResult(status="conflict", conflicts=["j"]),
    }
    plan = flush_exit_plan(results, {Path("/b"): {"k": "int 违规"}})
    assert plan.action == "show_conflict" and plan.conflict_file is pc


def test_exit_plan_blocked_lists_items_but_exits_zero():
    """§7.7：block 级非法暂态退出即丢弃，UI 应当列出；退出码仍 0（裁量）。"""
    blocked = {Path("/x/param.py"): {"KICK": "非整数", "OTHER": "越界"}}
    plan = flush_exit_plan({Path("/x/param.py"): FlushResult(status="clean")},
                           blocked)
    assert plan.action == "confirm_blocked"
    assert plan.exit_code == 0
    assert plan.blocked == ("param.py · KICK：非整数", "param.py · OTHER：越界")
    assert blocked_lines_of({}) == ()


def test_noninteractive_exit_code_semantics():
    assert noninteractive_exit_code({}) == 0
    assert noninteractive_exit_code(
        {Path("/a"): FlushResult(status="clean")}) == 0
    for status in ("conflict", "failed", "paused"):
        assert noninteractive_exit_code(
            {Path("/a"): FlushResult(status="clean"),
             Path("/b"): FlushResult(status=status)}) == 3


# ===========================================================================
# 1. 纯层：U-11 快捷键注册表
# ===========================================================================


def test_keybinding_registry_full_set():
    assert keybinding_action("b", ctrl=True) == "toggle_sidebar"
    assert keybinding_action("B", ctrl=True) == "toggle_sidebar"  # 大小写归一
    assert keybinding_action("b", ctrl=True, shift=True) is None
    assert keybinding_action("w", ctrl=True) == "close_file"
    # Ctrl+Shift+W 同动作（浏览器保留 Ctrl+W 的 native 备用组合，裁量登记）
    assert keybinding_action("w", ctrl=True, shift=True) == "close_file"
    assert keybinding_action("z", ctrl=True) == "undo"
    assert keybinding_action("z", ctrl=True, shift=True) == "redo"
    assert keybinding_action("y", ctrl=True) is None        # 未登记
    assert keybinding_action("ArrowUp") == "nav_prev"
    assert keybinding_action("ArrowDown") == "nav_next"
    assert keybinding_action("ArrowUp", shift=True) is None  # 修饰即让位
    assert keybinding_action("a") is None
    assert keybinding_action(None) is None                   # 防御
    # alt/meta 组合一律忽略（让位系统/浏览器）
    assert keybinding_action("z", ctrl=True, alt=True) is None
    assert keybinding_action("b", ctrl=True, meta=True) is None


def test_keybinding_scopes_split_input_focus_rule():
    """输入框焦点不劫持规则（G2a 裁量沿用）：编辑类走 ignore=('input',
    'textarea') 层；全局类（Ctrl+B/Ctrl+W）输入焦点内也生效。"""
    assert action_scope("toggle_sidebar") == "global"
    assert action_scope("close_file") == "global"
    for a in ("undo", "redo", "nav_prev", "nav_next"):
        assert action_scope(a) == "edit"
    assert action_scope(None) is None
    assert action_scope("bogus") is None
    assert dialogs.KEYBOARD_IGNORE_SAFE == ("input", "textarea")


def test_nav_move_boundaries_no_wrap():
    paths = ["a", "b", "c"]
    assert nav_move(paths, None, 1) == "a"        # 无选中 → 首条
    assert nav_move(paths, None, -1) == "c"       # 无选中 → 末条
    assert nav_move(paths, "a", 1) == "b"
    assert nav_move(paths, "c", 1) == "c"         # 尽头不环绕
    assert nav_move(paths, "a", -1) == "a"
    assert nav_move(paths, "gone.item", 1) == "a"  # 选中项已消失 → 首条
    assert nav_move([], "a", 1) is None           # 空列表防御


# ===========================================================================
# 2. 编排层：横幅状态机（consume_events）
# ===========================================================================


def test_consume_events_sets_per_file_banners(tmp_path):
    mgr, st, f = _make_state(tmp_path)
    try:
        cb = st.make_state_callback(f.resolve())
        cb("external_modified")
        events = st.consume_events()
        assert events == [(f.resolve(), "external_modified")]
        assert st.banners[f.resolve()] == dialogs.BANNER_MODIFIED
        lines = st.banner_lines()
        assert lines == [(f.resolve(), dialogs.BANNER_MODIFIED)]

        cb("gone")
        st.consume_events()
        assert st.banners[f.resolve()].startswith(dialogs.BANNER_GONE)

        cb("recovered")
        st.consume_events()
        assert st.banners == {}                   # 撤横幅
        assert any(dialogs.BANNER_RECOVERED in m for m, _ in st.notes)  # toast
        assert st.consume_events() == []          # 队列已空
    finally:
        mgr.stop_all()


def test_dismiss_banner_manual_close(tmp_path):
    mgr, st, f = _make_state(tmp_path)
    try:
        st.banners = apply_external_event({}, f.resolve(), "external_modified")
        st.dismiss_banner(f.resolve())
        assert st.banners == {}
        st.dismiss_banner(Path("/never"))          # 不存在键防御不抛
    finally:
        mgr.stop_all()


def test_ensure_state_callbacks_rebinds_missing(tmp_path):
    """CLI 启动批次/转交会话直开 manager（无回调）→ 公开 API 安全网补绑。"""
    f = tmp_path / "d.yaml"
    f.write_text(FIXTURE_YAML, encoding="utf-8")
    mgr = SessionManager()
    st = GuiState(mgr)
    try:
        mgr.open_files([f])                        # 直连 manager（CLI 语义）
        assert st.ensure_state_callbacks() == 1
        assert st.ensure_state_callbacks() == 0    # 幂等（对象同一性判定）
        session = mgr.get(f.resolve())
        session._on_state_change("external_modified")   # poller 线程语义
        assert st.drain_events() == [(f.resolve(), "external_modified")]
    finally:
        mgr.stop_all()


def test_open_more_binds_path_callbacks(tmp_path):
    mgr, st, f = _make_state(tmp_path)
    f2 = tmp_path / "second.yaml"
    f2.write_text("a:\n  b: 1\nmisc:\n  c: 2\n", encoding="utf-8")
    try:
        results = st.open_more([f2])
        assert [r.session is not None for r in results] == [True]
        session = mgr.get(f2.resolve())
        assert getattr(session, "_on_state_change") is not None
        session._on_state_change("gone")
        assert st.drain_events() == [(f2.resolve(), "gone")]
    finally:
        mgr.stop_all()


# ===========================================================================
# 2. 编排层：§7.6 冲突模态处置
# ===========================================================================


def test_commit_conflict_raises_modal_request(tmp_path):
    """A-11：提交撞基线校验 → pending_conflict（默认处置=重新加载）。"""
    mgr, st, f = _make_conflict(tmp_path)
    try:
        req = st.pending_conflict
        assert req is not None
        assert req.source == "commit"
        assert req.file == f.resolve()
        assert req.item_path == "game.enabled"
        assert st.commit_error is None             # 模态接管，非降级错误条
        # 冲突未写盘：磁盘仍是外部修改内容（A-11"全程不被 configer 覆盖"）
        assert "player_id: 5" in f.read_text(encoding="utf-8")
        assert "enabled: true" in f.read_text(encoding="utf-8")
    finally:
        mgr.stop_all()


def test_resolve_reload_discards_pending_and_clears_stacks(tmp_path):
    """重新加载：外部修改保留、暂态丢弃、undo/redo 清空（有提示）、横幅撤。"""
    mgr, st, f = _make_conflict(tmp_path)
    try:
        st.banners[f.resolve()] = dialogs.BANNER_MODIFIED   # 模拟横幅在挂
        result = st.resolve_conflict_choice("reload")
        assert result is not None and result.status == "reloaded"
        session = mgr.get(f.resolve())
        assert _item_value(session, "game.player_id") == 5   # 外部内容
        assert _item_value(session, "game.enabled") is True  # 本次编辑丢弃
        assert session.undo_depth() == 0 and session.redo_depth() == 0
        assert not session.paused
        assert st.pending_conflict is None
        assert st.banners == {}                              # 撤横幅
        assert any("undo/redo 栈已清空" in m for m, _ in st.notes)
        assert "player_id: 5" in f.read_text(encoding="utf-8")  # 盘上=外部内容
    finally:
        mgr.stop_all()


def test_resolve_force_overwrites_external_changes(tmp_path):
    """强制覆盖（UI 已二次警示后调用）：本次编辑写入，外部修改永久丢失。"""
    mgr, st, f = _make_conflict(tmp_path)
    try:
        result = st.resolve_conflict_choice("force")
        assert result is not None and result.status == "committed"
        text = f.read_text(encoding="utf-8")
        assert "enabled: false" in text            # 本次编辑落盘
        assert "player_id: 3" in text              # 外部修改被覆盖（丢失）
        session = mgr.get(f.resolve())
        assert not session.paused
        assert session.undo_depth() == 1           # force 成功入 undo 栈
        assert st.pending_conflict is None
        assert st.banners == {}
        assert st.last_commit_echo == "enabled: True → False"
    finally:
        mgr.stop_all()


def test_resolve_dismiss_pauses_and_keeps_pending(tmp_path):
    """关闭模态（×/Esc）→ pause_auto_commit + 错误条常驻 + 暂态保留。"""
    mgr, st, f = _make_conflict(tmp_path)
    try:
        assert st.resolve_conflict_choice("dismiss") is None
        session = mgr.get(f.resolve())
        assert session.paused
        assert session.has_pending("game.enabled")   # 暂态保留
        assert st.pending_conflict is None
        assert "自动提交已暂停" in st.commit_error
        assert st.commit_error_retry is None         # 冲突不提供盲目重试
        assert st.commit_error_conflict == f.resolve()   # 恢复入口
        # 暂停期间提交一律静默拒绝（不打扰）
        r = st.commit_click_now("game.enabled", checked=True)
        assert r is not None and r.status == "paused"
        assert "自动提交已暂停" in st.commit_error   # 错误条仍常驻

        # 「处理冲突」入口 → 重开模态 → reload 成功后恢复自动提交
        st.reopen_conflict(f.resolve())
        assert st.pending_conflict is not None
        assert st.commit_error is None and st.commit_error_conflict is None
        result = st.resolve_conflict_choice("reload")
        assert result.status == "reloaded"
        assert not session.paused
        assert st.commit_error is None
    finally:
        mgr.stop_all()


def test_resolve_without_pending_request_is_noop(tmp_path):
    mgr, st, f = _make_state(tmp_path)
    try:
        assert st.resolve_conflict_choice("reload") is None   # 无待决请求
        st.pending_conflict = ConflictRequest(
            file=Path("/nonexistent"), file_name="nonexistent",
            reason=None, source="commit")
        assert st.resolve_conflict_choice("reload") is None   # 会话不存在防御
        assert st.pending_conflict is None
    finally:
        mgr.stop_all()


def test_gone_commit_sets_distinct_banner(tmp_path):
    """gone 期间提交尝试 → status 'gone' → 横幅（区别文案）；恢复后解除。

    现场构造：合法暂态先进入（文件仍在）→ 删除文件（poller GONE 边沿）→
    防抖到期提交 → 核心 gone 探测 → CommitResult 'gone'。
    """
    mgr, st, f = _make_state(tmp_path)
    try:
        session = mgr.get(f.resolve())
        st.on_text_keystroke("game.player_id", "2")   # 合法暂态入 pending
        f.unlink()                                   # 删除文件
        session._on_poller_change(FileState.GONE)    # poller 边沿语义
        st.consume_events()
        assert st.banners[f.resolve()].startswith(dialogs.BANNER_GONE)
        assert session.gone_readonly                 # §7.6 只读态

        result = st.commit_text_now("game.player_id", "2")   # 到期提交
        assert result is not None and result.status == "gone"
        assert st.banners[f.resolve()].startswith(dialogs.BANNER_GONE)

        # 恢复原内容（=基线）→ 下次 set_pending 主动探测 → 'recovered'
        f.write_text(FIXTURE_YAML, encoding="utf-8")
        st.on_text_keystroke("game.player_id", "2")
        assert not session.gone_readonly             # 只读态解除
        st.consume_events()
        assert st.banners == {}                      # recovered 撤横幅
        assert any(dialogs.BANNER_RECOVERED in m for m, _ in st.notes)
    finally:
        mgr.stop_all()


def test_reload_file_from_banner_recovers_gone(tmp_path):
    """横幅【重新加载】= §7.6"用户重载"恢复入口（gone/modified 通用）。"""
    mgr, st, f = _make_state(tmp_path)
    try:
        session = mgr.get(f.resolve())
        f.unlink()
        session._on_poller_change(FileState.GONE)
        st.consume_events()
        assert session.gone_readonly
        # 仍不可读 → 重载失败按 gone 处置，横幅保留
        assert st.reload_file(f.resolve()).status == "gone"
        assert st.banners[f.resolve()].startswith(dialogs.BANNER_GONE)
        # 恢复后重载 → 只读态解除 + 撤横幅
        f.write_text(FIXTURE_YAML, encoding="utf-8")
        result = st.reload_file(f.resolve())
        assert result.status == "reloaded"
        assert not session.gone_readonly
        assert st.banners == {}
        assert st.reload_file(Path("/nonexistent")) is None   # 防御
    finally:
        mgr.stop_all()


# ===========================================================================
# 2. 编排层：U-12 关闭文件
# ===========================================================================


def test_close_file_flushes_debounce_then_removes(tmp_path):
    """A-16：有未提交合法暂态时关闭 → 暂态先落盘再关闭。"""
    clock = FakeClock()
    mgr, st, f = _make_state(tmp_path, clock)
    try:
        st.on_text_keystroke("game.player_id", "2")   # 防抖窗口内（未到期）
        assert "player_id: 3" in f.read_text(encoding="utf-8")
        result = st.request_close_file(f.resolve())
        assert result.status == "closed"
        assert "player_id: 2" in f.read_text(encoding="utf-8")  # 关闭前落盘
        assert st.file_entries() == []                # 侧栏移除
        assert st.active_path is None                 # 主区回空态
        assert mgr.get(f.resolve()) is None           # 会话已释放
        assert st.debounce.pending_items() == []
    finally:
        mgr.stop_all()


def test_close_all_files_leaves_app_running(tmp_path):
    """U-12：全部文件关闭 → 主区空态，应用不退出（"+"可再开）。"""
    mgr, st, f = _make_state(tmp_path)
    stopped = []
    st.request_stop = lambda: stopped.append(1)
    try:
        st.request_close_file(f.resolve())
        assert st.active_path is None
        assert st.file_entries() == []
        assert stopped == []                          # 不触发停服
        assert st.exit_finalized is False
    finally:
        mgr.stop_all()


def test_close_switches_active_to_neighbor(tmp_path):
    """关闭活动文件 → 切换到相邻（原位置的下一个，否则最后一个）。"""
    mgr, st, f = _make_state(tmp_path)
    f2 = tmp_path / "second.yaml"
    f2.write_text("a:\n  b: 1\nmisc:\n  c: 2\n", encoding="utf-8")
    f3 = tmp_path / "third.yaml"
    f3.write_text("a:\n  b: 2\nmisc:\n  c: 3\n", encoding="utf-8")
    try:
        st.open_more([f2, f3])
        st.activate(f2.resolve())                     # 中间文件活动
        st.request_close_file(f2.resolve())
        assert st.active_path == f3.resolve()         # 原下标 → 下一个
        st.request_close_file(f3.resolve())
        assert st.active_path == f.resolve()          # 末尾关闭 → 最后一个
        # 关闭非活动文件不影响活动文件
        st.request_close_file(f.resolve())
        assert st.active_path is None
    finally:
        mgr.stop_all()


def test_close_gone_with_pending_needs_confirm(tmp_path):
    """gone 且有暂态 → need_confirm 确认框：放弃暂态并关闭 / 取消关闭。"""
    mgr, st, f = _make_state(tmp_path)
    try:
        session = mgr.get(f.resolve())
        st.on_text_keystroke("game.player_id", "2")   # 合法暂态入 pending
        st.debounce.cancel("game.player_id")          # 聚焦分流：直接走 close
        f.unlink()
        session._on_poller_change(FileState.GONE)
        st.consume_events()

        result = st.request_close_file(f.resolve())
        assert result.status == "need_confirm"
        pc = st.pending_close
        assert pc is not None and pc.kind == "need_confirm"
        assert pc.file == f.resolve()
        assert any("不可读" in line for line in pc.lines)

        st.confirm_close("cancel")                    # 取消关闭 → 会话保留
        assert st.pending_close is None
        assert mgr.get(f.resolve()) is not None
        assert st.file_entries()                      # 侧栏仍在

        st.request_close_file(f.resolve())            # 再次关闭 → 再确认
        assert st.pending_close is not None
        st.confirm_close("discard")                   # 放弃暂态并关闭
        assert mgr.get(f.resolve()) is None
        assert st.file_entries() == []
        assert st.pending_close is None
    finally:
        mgr.stop_all()


def test_close_conflict_blocks_until_resolved_no_auto_retry(tmp_path):
    """A-16：关闭撞基线冲突 → 模态未决前不关闭；处置后**不自动重试关闭**。"""
    mgr, st, f = _make_state(tmp_path)
    try:
        st.on_text_keystroke("game.player_id", "2")   # 合法暂态
        st.debounce.cancel("game.player_id")
        f.write_text(FIXTURE_YAML.replace("ratio: 0.5", "ratio: 0.9"),
                     encoding="utf-8")                # 外部修改

        result = st.request_close_file(f.resolve())
        assert result.status == "conflict"
        assert mgr.get(f.resolve()) is not None       # 未决前不关闭
        req = st.pending_conflict
        assert req is not None and req.source == "close"
        assert req.item_path == "game.player_id"

        st.resolve_conflict_choice("reload")          # 处置 = 重载
        assert st.pending_conflict is None
        assert mgr.get(f.resolve()) is not None       # 不自动重试关闭（裁量）
        assert st.file_entries()                      # 侧栏仍在
        assert any("再次点击关闭" in m for m, _ in st.notes)

        st.request_close_file(f.resolve())            # 用户再点 → 正常关闭
        assert mgr.get(f.resolve()) is None
    finally:
        mgr.stop_all()


def test_close_failed_offers_retry_and_discard(tmp_path):
    """A-16：关闭触发提交失败（目录只读）→ 阻止关闭，可重试或放弃暂态。"""
    mgr, st, f = _make_state(tmp_path)
    try:
        st.on_text_keystroke("game.player_id", "2")
        st.debounce.cancel("game.player_id")
        tmp_path.chmod(0o555)                         # 目录只读 → 写盘必败
        try:
            result = st.request_close_file(f.resolve())
            assert result.status == "failed"
            assert mgr.get(f.resolve()) is not None   # 阻止关闭
            pc = st.pending_close
            assert pc is not None and pc.kind == "failed"
            assert st.commit_error and "关闭" in st.commit_error

            st.confirm_close("retry")                 # 重试（目录仍只读）
            # retry 走 request_close_file：pending_close 重建（仍 failed）
            assert st.pending_close is not None and st.pending_close.kind == "failed"

            st.confirm_close("cancel")                # 取消 → 保留现场
            assert st.pending_close is None
            assert mgr.get(f.resolve()) is not None

            st.request_close_file(f.resolve())        # 再点关闭 → 再弹框
            assert st.pending_close is not None
        finally:
            tmp_path.chmod(0o755)

        st.confirm_close("discard")                   # 放弃暂态并关闭
        assert mgr.get(f.resolve()) is None
        assert st.file_entries() == []
        assert "player_id: 3" in f.read_text(encoding="utf-8")  # 未落盘
    finally:
        mgr.stop_all()


def test_confirm_close_without_request_is_noop(tmp_path):
    mgr, st, f = _make_state(tmp_path)
    try:
        st.confirm_close("discard")                   # 无待决确认框 → 防御
        st.confirm_close("cancel")
        assert mgr.get(f.resolve()) is not None
        assert st.request_close_file(None) is None    # 无活动文件防御
    finally:
        mgr.stop_all()


# ===========================================================================
# 2. 编排层：§7.7 退出 flush
# ===========================================================================


def test_exit_flush_commits_debounce_and_exits_zero(tmp_path):
    """A-15：编辑后立即退出（防抖窗口内）→ 退出前 flush，最后编辑已落盘。"""
    clock = FakeClock()
    mgr, st, f = _make_state(tmp_path, clock)
    stopped = []
    st.request_stop = lambda: stopped.append(1)
    try:
        st.on_text_keystroke("game.player_id", "2")
        plan = st.begin_exit()
        assert plan.action == "exit_clean"
        assert "player_id: 2" in f.read_text(encoding="utf-8")
        assert st.exit_code == 0 and st.exit_finalized
        assert stopped == [1]                         # 请求停服恰一次
        assert st.begin_exit() is plan                # finalize 后幂等
    finally:
        mgr.stop_all()


def test_exit_blocked_items_listed_then_confirmed(tmp_path):
    """§7.7：非法暂态退出即丢弃，确认框**列出条目**；退出码仍 0（裁量）。"""
    mgr, st, f = _make_state(tmp_path)
    stopped = []
    st.request_stop = lambda: stopped.append(1)
    try:
        st.on_text_keystroke("game.player_id", "不是数字")   # invalid → blocked
        plan = st.begin_exit()
        assert plan.action == "confirm_blocked"
        pe = st.pending_exit
        assert pe is not None and pe.kind == "blocked"
        assert any("player_id" in line for line in pe.lines)
        assert stopped == []                          # 等待用户确认

        st.confirm_exit("cancel")                     # 取消 = 留在应用
        assert st.pending_exit is None and not st.exit_finalized
        assert st.exiting is False and stopped == []

        st.begin_exit()                               # 再次退出 → 确认
        st.confirm_exit("ok")
        assert st.exit_code == 0 and stopped == [1]
    finally:
        mgr.stop_all()


def test_exit_conflict_modal_with_cancel_exit(tmp_path):
    """退出撞冲突 → 模态（source='exit'）；「取消退出」= 留在应用。"""
    mgr, st, f = _make_state(tmp_path)
    stopped = []
    st.request_stop = lambda: stopped.append(1)
    try:
        st.on_text_keystroke("game.player_id", "2")
        f.write_text(FIXTURE_YAML.replace("ratio: 0.5", "ratio: 0.9"),
                     encoding="utf-8")                # 外部修改
        plan = st.begin_exit()
        assert plan.action == "show_conflict"
        req = st.pending_conflict
        assert req is not None and req.source == "exit"
        assert stopped == [] and not st.exit_finalized

        st.resolve_conflict_choice("dismiss")         # = 取消退出
        assert st.exiting is False
        assert not st.exit_finalized and stopped == []
        assert mgr.get(f.resolve()).paused            # 暂停自动提交
    finally:
        mgr.stop_all()


def test_exit_conflict_paused_path_then_reload_continues_exit(tmp_path):
    """paused 文件按 conflict 路径；reload 抉择后**续流**完成退出。"""
    mgr, st, f = _make_state(tmp_path)
    stopped = []
    st.request_stop = lambda: stopped.append(1)
    try:
        st.on_text_keystroke("game.player_id", "2")
        f.write_text(FIXTURE_YAML.replace("ratio: 0.5", "ratio: 0.9"),
                     encoding="utf-8")
        st.begin_exit()                               # → show_conflict
        st.resolve_conflict_choice("dismiss")         # 取消退出 → paused
        assert mgr.get(f.resolve()).paused

        plan = st.begin_exit()                        # 再退出：paused 分流
        assert plan.action == "show_conflict"         # paused 按 conflict 路径
        assert st.pending_conflict.source == "exit"

        st.resolve_conflict_choice("reload")          # 重载 → 续流 → clean
        assert st.exit_finalized and st.exit_code == 0
        assert stopped == [1]
        assert "ratio: 0.9" in f.read_text(encoding="utf-8")  # 外部修改保留
        assert "player_id: 3" in f.read_text(encoding="utf-8")  # 暂态丢弃
    finally:
        mgr.stop_all()


def test_exit_failed_ask_force_quit_abandons_with_exit3(tmp_path):
    """A-15：落盘失败（目录只读）→ 确认框；仍要退出 = abandon + exit 3。"""
    mgr, st, f = _make_state(tmp_path)
    stopped = []
    st.request_stop = lambda: stopped.append(1)
    try:
        st.on_text_keystroke("game.player_id", "2")   # 合法暂态
        tmp_path.chmod(0o555)
        try:
            plan = st.begin_exit()
            assert plan.action == "ask_failed" and plan.exit_code == 3
            pe = st.pending_exit
            assert pe.kind == "failed" and pe.files == (f.resolve(),)
            assert any("player_id" in line for line in pe.lines)
            assert st.commit_error and "退出" in st.commit_error
            assert stopped == []

            st.confirm_exit("cancel")                 # 取消 = 留在应用
            assert not st.exit_finalized and stopped == []
            assert mgr.get(f.resolve()).has_pending("game.player_id")

            st.begin_exit()
            st.confirm_exit("ok")                     # 仍要退出
            assert st.exit_code == 3 and stopped == [1]
            assert mgr.get(f.resolve()).pending_items() == {}   # abandon
        finally:
            tmp_path.chmod(0o755)
        assert "player_id: 3" in f.read_text(encoding="utf-8")  # 未落盘
    finally:
        mgr.stop_all()


def test_exit_noninteractive_and_shutdown_safety_net(tmp_path):
    """非交互退出：clean → 0；conflict → 3（局限登记）；安全网幂等。"""
    clock = FakeClock()
    mgr, st, f = _make_state(tmp_path, clock)
    stopped = []
    st.request_stop = lambda: stopped.append(1)
    try:
        st.on_text_keystroke("game.player_id", "2")   # 防抖计时中
        code = st.exit_noninteractive()
        assert code == 0 and st.exit_code == 0
        assert "player_id: 2" in f.read_text(encoding="utf-8")  # flush 落盘
        assert stopped == [1]
        assert st.on_app_shutdown() is None           # 幂等跳过（G1 契约）
        assert st.exit_code == 0
    finally:
        mgr.stop_all()


def test_exit_noninteractive_conflict_yields_three(tmp_path):
    mgr, st, f = _make_state(tmp_path)
    stopped = []
    st.request_stop = lambda: stopped.append(1)
    try:
        st.on_text_keystroke("game.player_id", "2")
        f.write_text(FIXTURE_YAML.replace("ratio: 0.5", "ratio: 0.9"),
                     encoding="utf-8")
        code = st.exit_noninteractive()
        assert code == 3 and st.exit_code == 3 and stopped == [1]
        assert "ratio: 0.9" in f.read_text(encoding="utf-8")  # 外部修改保留
        assert st.pending_conflict is None            # 无客户端不弹模态
    finally:
        mgr.stop_all()


def test_on_app_shutdown_empty_manager_keeps_zero():
    """G1 契约回归：空会话 on_app_shutdown() → None 且 exit_code 保持 0。"""
    st = GuiState(SessionManager())
    assert st.on_app_shutdown() is None
    assert st.exit_code == 0


def test_beforeunload_guard_tracks_losable_state(tmp_path):
    """守卫标志：防抖计时中/未决暂态/paused → True；全 clean → False。"""
    clock = FakeClock()
    mgr, st, f = _make_state(tmp_path, clock)
    try:
        assert st.beforeunload_guard() is False
        st.on_text_keystroke("game.player_id", "2")
        assert st.beforeunload_guard() is True        # 防抖计时中
        clock.advance(500)
        st.drain_debounce()                           # 提交落盘
        assert st.beforeunload_guard() is False

        st.on_text_keystroke("game.player_id", "不是数字")   # invalid
        assert st.beforeunload_guard() is False       # 非法输入不武装守卫
        st.debounce.cancel("game.player_id")

        st.on_text_keystroke("game.player_id", "1")
        tmp_path.chmod(0o555)
        try:
            clock.advance(500)
            st.drain_debounce()                       # 提交失败 → 暂态保留
            assert st.beforeunload_guard() is True
        finally:
            tmp_path.chmod(0o755)
    finally:
        mgr.stop_all()


# ===========================================================================
# 2. 编排层：U-11 快捷键派发（layout._dispatch_key_action 宿主侧）
# ===========================================================================


def test_key_dispatch_toggle_sidebar_and_undo_redo(tmp_path):
    from configer.ui.layout import _dispatch_key_action

    mgr, st, f = _make_state(tmp_path)
    try:
        assert st.sidebar_visible
        _dispatch_key_action(st, "toggle_sidebar")
        assert not st.sidebar_visible
        _dispatch_key_action(st, "toggle_sidebar")
        assert st.sidebar_visible

        st.commit_click_now("game.enabled", checked=False)
        assert "enabled: false" in f.read_text(encoding="utf-8")
        _dispatch_key_action(st, "undo")              # Ctrl+Z 语义
        assert "enabled: true" in f.read_text(encoding="utf-8")
        _dispatch_key_action(st, "redo")              # Ctrl+Shift+Z 语义
        assert "enabled: false" in f.read_text(encoding="utf-8")
        _dispatch_key_action(st, None)                # 未登记动作防御
        _dispatch_key_action(st, "bogus")
    finally:
        mgr.stop_all()


def test_key_dispatch_nav_and_close_file(tmp_path):
    from configer.ui import logic
    from configer.ui.layout import _dispatch_key_action

    mgr, st, f = _make_state(tmp_path)
    try:
        order = [it.path for it in mgr.get(f.resolve()).doc.items]
        tree = logic.build_group_tree(mgr.get(f.resolve()).doc)
        assert nav_paths(tree) == order               # 树序 = 文件出现序

        _dispatch_key_action(st, "nav_next")
        assert st.selected_item == order[0]
        _dispatch_key_action(st, "nav_next")
        assert st.selected_item == order[1]
        _dispatch_key_action(st, "nav_prev")
        assert st.selected_item == order[0]
        _dispatch_key_action(st, "nav_prev")          # 尽头不环绕
        assert st.selected_item == order[0]

        _dispatch_key_action(st, "close_file")        # Ctrl+W 语义
        assert st.file_entries() == [] and st.active_path is None
        _dispatch_key_action(st, "nav_next")          # 无会话防御不抛
        _dispatch_key_action(st, "close_file")
    finally:
        mgr.stop_all()


# ===========================================================================
# 3. 端到端：真实 poller + 金样本拷贝（testdata/config.yaml）
# ===========================================================================

EXTERNAL_MARKER = "# 外部修改-e2e"
SENTINEL_IP = "203.0.113.7"     # TEST-NET-3，金样本中不存在的哨兵值


def _golden_state(tmp_path):
    """真实 SessionManager + 金样本 tmp 拷贝；短周期 poller + per-path 回调。"""
    src = TESTDATA / "config.yaml"
    dst = tmp_path / "config.yaml"
    shutil.copyfile(src, dst)
    mgr = SessionManager()
    st = GuiState(mgr, poll_interval=0.05)
    st.notes = []                     # type: ignore[attr-defined]
    st.notify_fn = lambda msg, kind="positive": st.notes.append((msg, kind))
    results = st.open_more([dst])     # per-path 回调绑定 + start_poller
    assert results[0].session is not None, results[0].diagnostics
    st.activate(dst.resolve())
    return mgr, st, dst


def _pick_str_item(session):
    """动态选一个可编辑 str 条目（金样本演进免疫；避开枚举候选）。"""
    for it in session.doc.items:
        if it.type == "str" and not it.readonly and not it.enum_candidates:
            return it
    raise AssertionError("金样本中未找到可编辑 str 条目")


def test_e2e_poller_external_modification_shows_banner(tmp_path):
    """A-11 前半：真实 poller 线程 → 事件队列 → consume → 横幅。"""
    mgr, st, f = _golden_state(tmp_path)
    try:
        f.write_text(f.read_text(encoding="utf-8") + f"\n{EXTERNAL_MARKER}\n",
                     encoding="utf-8")
        deadline = time.time() + 5.0
        while time.time() < deadline and not st.banners:
            time.sleep(0.05)
            st.consume_events()
        assert st.banners[f.resolve()] == dialogs.BANNER_MODIFIED
        # 不自动重载：doc 基线仍是打开时内容（标记不在 doc 字节内）
        session = mgr.get(f.resolve())
        assert EXTERNAL_MARKER.encode() not in session.doc.original_bytes
    finally:
        mgr.stop_all()


def test_e2e_commit_conflict_reload_keeps_external_content(tmp_path):
    """A-11 后半：提交撞冲突 → 模态 → reload 后 doc=外部内容，外部修改
    完整保留、全程不被 configer 覆盖。"""
    mgr, st, f = _golden_state(tmp_path)
    try:
        session = mgr.get(f.resolve())
        item = _pick_str_item(session)
        f.write_text(f.read_text(encoding="utf-8") + f"\n{EXTERNAL_MARKER}\n",
                     encoding="utf-8")                 # 外部修改（不经 configer）

        st.on_text_keystroke(item.path, SENTINEL_IP)
        result = st.commit_text_now(item.path, SENTINEL_IP)
        assert result is not None and result.status == "conflict"
        req = st.pending_conflict
        assert req is not None and req.file == f.resolve()

        result = st.resolve_conflict_choice("reload")
        assert result.status == "reloaded"
        # doc = 外部内容（含标记注释的字节）
        assert EXTERNAL_MARKER.encode() in session.doc.original_bytes
        # 盘上：外部修改保留，本次编辑未写入
        text = f.read_text(encoding="utf-8")
        assert EXTERNAL_MARKER in text and SENTINEL_IP not in text
        # 本次编辑的暂态已丢弃、栈已清空
        assert session.pending_items() == {}
        assert session.undo_depth() == 0 and session.redo_depth() == 0
    finally:
        mgr.stop_all()


def test_e2e_close_flushes_pending_to_disk(tmp_path):
    """A-16：金样本上编辑（防抖窗口内）→ 关闭 → 暂态先落盘再关闭。"""
    mgr, st, f = _golden_state(tmp_path)
    try:
        session = mgr.get(f.resolve())
        item = _pick_str_item(session)
        st.on_text_keystroke(item.path, SENTINEL_IP)   # 真实 400ms 窗口内
        assert SENTINEL_IP not in f.read_text(encoding="utf-8")
        result = st.request_close_file(f.resolve())
        assert result.status == "closed"
        assert SENTINEL_IP in f.read_text(encoding="utf-8")   # 目标行已落盘
        assert mgr.get(f.resolve()) is None                   # 资源已释放
    finally:
        mgr.stop_all()


def test_e2e_exit_flush_commits_pending(tmp_path):
    """A-15：金样本上编辑（防抖窗口内）→ 退出 → flush 落盘 + exit 0。"""
    mgr, st, f = _golden_state(tmp_path)
    stopped = []
    st.request_stop = lambda: stopped.append(1)
    try:
        session = mgr.get(f.resolve())
        item = _pick_str_item(session)
        st.on_text_keystroke(item.path, SENTINEL_IP)
        plan = st.begin_exit()
        assert plan.action == "exit_clean"
        assert SENTINEL_IP in f.read_text(encoding="utf-8")
        assert st.exit_code == 0 and stopped == [1]
    finally:
        mgr.stop_all()


# ===========================================================================
# 4. 无头构建冒烟：模态/横幅组件可构建（不真起 ui.run）
# ===========================================================================


def test_modal_host_sync_headless(tmp_path):
    """pending_* → sync 建对话框并 open；清空 → close（无头安全）。"""
    from configer.ui.layout import build_gui
    from configer.ui.state import CloseConfirm, ExitConfirm

    mgr, st, f = _make_state(tmp_path)
    try:
        st.pending_conflict = ConflictRequest(
            file=f.resolve(), file_name=f.name, reason="基线不一致",
            source="exit", item_path="game.player_id")
        st.pending_close = CloseConfirm(
            file=f.resolve(), kind="need_confirm", lines=("a：b",))
        st.pending_exit = ExitConfirm(
            kind="failed", lines=("c.yaml · x：io",), files=(f.resolve(),))
        build_gui(st)                       # refresh_all → modal_host.sync
        host = st.modal_host
        assert host is not None
        assert set(host.open_keys()) == {"conflict", "close", "exit"}
        # 清空 pending → touch → sync 全部关闭（幂等，不重复建）
        st.pending_conflict = None
        st.pending_close = None
        st.pending_exit = None
        st.touch()
        assert host.open_keys() == []
        st.touch()                          # 再刷不抛
        assert host.open_keys() == []
    finally:
        mgr.stop_all()


def test_modal_conflict_hide_dismiss_path_headless(tmp_path):
    """冲突模态 hide（×/Esc）→ dismiss 编排（无头经 _on_conflict_hide）。"""
    from configer.ui.layout import build_gui

    mgr, st, f = _make_conflict(tmp_path)
    try:
        build_gui(st)                       # pending_conflict 已由提交挂起
        host = st.modal_host
        assert "conflict" in host.open_keys()
        host._on_conflict_hide()            # 模拟浏览器 hide 事件
        assert st.pending_conflict is None
        assert mgr.get(f.resolve()).paused  # §7.6：暂停自动提交
        assert "自动提交已暂停" in st.commit_error
        assert host.open_keys() == []       # sync 已随 dismiss 的 touch 关闭
    finally:
        mgr.stop_all()


def test_modal_close_hide_cancels_headless(tmp_path):
    """关闭确认框 hide（×/Esc）=「取消关闭」（无头经 _on_close_hide）。

    修复登记：hide 未接线时 pending_close 滞留 → sync 见 key 已在 _open
    不重建 → 再点 × 关闭入口失效；接线后 Esc 取消、入口可重入。
    """
    from configer.ui.layout import build_gui
    from configer.ui.state import CloseConfirm

    mgr, st, f = _make_state(tmp_path)
    try:
        st.pending_close = CloseConfirm(
            file=f.resolve(), kind="need_confirm", lines=("game.enabled：暂态",))
        build_gui(st)
        host = st.modal_host
        assert "close" in host.open_keys()
        host._on_close_hide()               # 模拟浏览器 hide 事件
        assert st.pending_close is None     # 取消：滞留清空
        assert mgr.get(f.resolve()) is not None   # 文件未被关闭
        assert host.open_keys() == []
        # 入口可重入：再走关闭流程能重建确认框
        st.pending_close = CloseConfirm(
            file=f.resolve(), kind="need_confirm", lines=("game.enabled：暂态",))
        st.touch()
        assert "close" in host.open_keys()
    finally:
        mgr.stop_all()


def test_modal_exit_hide_cancels_headless(tmp_path):
    """退出确认框 hide（×/Esc）=「取消」（留在应用，无头经 _on_exit_hide）。"""
    from configer.ui.layout import build_gui
    from configer.ui.state import ExitConfirm

    mgr, st, f = _make_state(tmp_path)
    stopped = []
    st.request_stop = lambda: stopped.append(1)
    try:
        st.pending_exit = ExitConfirm(
            kind="failed", lines=("c.yaml · game.enabled：io",),
            files=(f.resolve(),))
        st.exiting = True
        build_gui(st)
        host = st.modal_host
        assert "exit" in host.open_keys()
        host._on_exit_hide()                # 模拟浏览器 hide 事件
        assert st.pending_exit is None
        assert st.exiting is False          # 取消 = 留在应用
        assert st.exit_code == 0 and stopped == []   # 未停服
        assert mgr.get(f.resolve()) is not None      # 暂态/会话未动
        assert host.open_keys() == []
    finally:
        mgr.stop_all()
