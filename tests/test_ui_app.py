"""G1 GUI 骨架测试：接缝契约 + GuiState 行为 + 无头构建（不真起 ui.run）。

**纪律**：pytest 进程内绝不真起 ``ui.run``（NiceGUI 事件循环与 pytest 冲突，
CLI 测试侧已用 sys.modules 屏蔽自保）——run_gui 用 monkeypatch 假 ui.run
验证行为；build_gui 无头构建（refreshable 初渲染不需事件循环，layout 侧
``_safe_refresh`` 对 ``core.loop is None`` 有防御）。
"""

from __future__ import annotations

import dataclasses
import inspect
import threading
import typing
from pathlib import Path

import pytest

from configer.core.session import SessionManager
from configer.ui.app import GuiOptions, run_gui
from configer.ui.state import GuiState

YAML_OK = "# 顶\n游戏: 占位\ngame:\n  player_id: 3  # 1|2|3\nrobot:\n  h: 0.9\n"


@pytest.fixture
def files(tmp_path):
    ok = tmp_path / "cfg.yaml"
    ok.write_text(YAML_OK, encoding="utf-8")
    ok2 = tmp_path / "second.yaml"
    ok2.write_text("a:\n  b: 1\n", encoding="utf-8")
    missing = tmp_path / "nope.yaml"
    return ok, ok2, missing


# ---------------------------------------------------------------------------
# 接缝契约（CLI 懒 import 的两个符号）
# ---------------------------------------------------------------------------


def test_contract_gui_options_fields():
    assert dataclasses.is_dataclass(GuiOptions)
    fields = {f.name: f for f in dataclasses.fields(GuiOptions)}
    assert set(fields) == {"host", "port", "poll_interval"}
    o = GuiOptions()
    assert o.host is None and o.port is None and o.poll_interval == 2.0


def test_contract_run_gui_signature():
    sig = inspect.signature(run_gui)
    # G2c：主契约 (manager, options) 不变；on_handoff_open 为仅关键字可选
    # 参数（向后兼容，默认 None——转交回调注册器，见 cli.py 接缝）。
    assert list(sig.parameters) == ["manager", "options", "on_handoff_open"]
    on_handoff = sig.parameters["on_handoff_open"]
    assert on_handoff.kind is inspect.Parameter.KEYWORD_ONLY
    assert on_handoff.default is None
    hints = typing.get_type_hints(run_gui)
    assert hints["manager"] is SessionManager
    assert hints["options"] is GuiOptions
    assert hints["return"] is int


def test_run_gui_passes_run_config(monkeypatch, capsys, files):
    """ui.run 配置契约：reload=False/show=False/title='configer'；
    host 默认 127.0.0.1；port=None 时预取空闲端口；stdout 打访问地址。"""
    captured = {}

    def fake_run(root, **kw):
        captured.update(kw)
        assert callable(root)  # root 函数模式（script mode 重执行坑，见 app.py）

    monkeypatch.setattr("nicegui.ui.run", fake_run)
    mgr = SessionManager()
    code = run_gui(mgr, GuiOptions())
    assert code == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["reload"] is False
    assert captured["show"] is False
    assert captured["title"] == "configer"
    assert isinstance(captured["port"], int) and captured["port"] > 0
    out = capsys.readouterr().out
    assert f"http://127.0.0.1:{captured['port']}" in out


def test_run_gui_explicit_host_port(monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(
        "nicegui.ui.run", lambda root, **kw: captured.update(kw))
    run_gui(SessionManager(), GuiOptions(host="0.0.0.0", port=9999))
    assert captured["host"] == "0.0.0.0" and captured["port"] == 9999
    assert "http://0.0.0.0:9999" in capsys.readouterr().out


def test_run_gui_startup_failure_raises_not_implemented(monkeypatch):
    """过渡期约定（协调通知）：启动阶段异常 → NotImplementedError
    （CLI 只捕获 NotImplementedError/ImportError → exit 2）。"""
    def boom(root, **kw):
        raise RuntimeError("uvicorn bind failed")

    monkeypatch.setattr("nicegui.ui.run", boom)
    with pytest.raises(NotImplementedError, match="启动失败"):
        run_gui(SessionManager(), GuiOptions(port=12345))


# ---------------------------------------------------------------------------
# GuiState（无头，真实 SessionManager）
# ---------------------------------------------------------------------------


def test_state_file_entries_include_failed(files):
    ok, _ok2, missing = files
    mgr = SessionManager()
    st = GuiState(mgr)
    try:
        results = mgr.open_files([ok, missing])
        st.register_results(results)
        entries = st.file_entries()
        assert [e.name for e in entries] == ["cfg.yaml", "nope.yaml"]
        loaded, failed = entries
        assert loaded.loaded and loaded.fmt == "yaml" and loaded.level == "ok"
        assert not failed.loaded and failed.fmt is None and failed.level == "error"
        assert [d.code for d in st.failed_diagnostics(missing.resolve())] == ["E-READ"]
        # 失败文件也可激活（U-10：主区显示诊断列表，由 layout 切换面板）
        st.activate(missing.resolve())
        assert st.active_session() is None
    finally:
        mgr.stop_all()


def test_state_selection_refetch_by_path(files):
    """关键坑（§7.5）：只存 path，条目对象每次从当前 doc 重取。"""
    ok, _ok2, _missing = files
    mgr = SessionManager()
    st = GuiState(mgr)
    try:
        st.register_results(mgr.open_files([ok]))
        st.activate(ok.resolve())
        assert st.selected_config_item() is None
        st.select_item("game.player_id")
        item = st.selected_config_item()
        assert item is not None and item.raw_literal == "3"
        # 重取按 path 匹配当前 doc，不依赖对象同一性（提交后 doc 重载安全）
        assert st.selected_config_item().path == item.path
        st.select_item("gone.item")
        assert st.selected_config_item() is None
    finally:
        mgr.stop_all()


def test_state_open_more_and_handoff(files):
    ok, ok2, missing = files
    mgr = SessionManager()
    st = GuiState(mgr)
    try:
        st.register_results(mgr.open_files([ok]))
        st.activate(ok.resolve())
        # "+"入口：新文件入侧栏并激活；失败者入 failed 表
        results = st.open_more([ok2, missing])
        assert [r.session is not None for r in results] == [True, False]
        assert [e.name for e in st.file_entries()] == [
            "cfg.yaml", "second.yaml", "nope.yaml"]
        assert mgr.get(ok2.resolve()) is not None  # poller 已启动由 stop_all 收
        # U-13 转交钩子：已打开仅聚焦、未打开加入并激活
        st.on_handoff_received([ok])
        assert st.active_path == ok.resolve()
    finally:
        mgr.stop_all()


# ---------------------------------------------------------------------------
# G2c：CLI/session/UI 接缝（U-10 启动批次失败诊断 + U-13 转交 + 公开重绑）
# ---------------------------------------------------------------------------


def test_state_startup_failed_results_registered_from_manager_seam(files):
    """U-10 接缝：cli setattr manager.last_open_results → GuiState 启动即
    登记失败文件（侧栏诊断态 + 主区诊断列表数据源）。"""
    ok, _ok2, missing = files
    mgr = SessionManager()
    try:
        results = mgr.open_files([ok, missing])
        mgr.last_open_results = results            # cli setattr（接缝契约）
        st = GuiState(mgr)                         # GUI 启动
        entries = st.file_entries()
        assert [e.name for e in entries] == ["cfg.yaml", "nope.yaml"]
        failed = entries[-1]
        assert not failed.loaded and failed.level == "error"
        assert [d.code for d in st.failed_diagnostics(missing.resolve())] \
            == ["E-READ"]
        st.activate(missing.resolve())             # 失败文件可激活看诊断
        assert st.active_session() is None
    finally:
        mgr.stop_all()


def test_register_results_binds_public_path_callbacks(files):
    """G2c 正规化：register_results 收口绑定 per-path 回调（公开 API），
    CLI 直开 manager 的会话由此获得事件接线（不再依赖 drain 补绑）。"""
    ok, _ok2, _missing = files
    mgr = SessionManager()
    st = GuiState(mgr)
    try:
        st.register_results(mgr.open_files([ok]))   # 不带回调（CLI 语义）
        session = mgr.get(ok.resolve())
        session._on_state_change("gone")            # poller 线程语义
        assert st.drain_events() == [(ok.resolve(), "gone")]
    finally:
        mgr.stop_all()


def test_handoff_queue_process_adds_activates_and_registers_failed(files):
    """U-13 跨线程投递 → 主循环 process_handoffs：新文件加入并激活，
    失败文件以诊断态登记（无新成功文件时激活失败项看诊断）。"""
    ok, _ok2, missing = files
    mgr = SessionManager()
    st = GuiState(mgr)
    try:
        st.register_results(mgr.open_files([ok]))
        st.activate(ok.resolve())
        # 模拟 cli 服务器线程：open 后投递完整批次（含失败文件）
        results = mgr.open_files([missing])
        st.enqueue_handoff(results)
        assert st.process_handoffs() == 1
        assert st.process_handoffs() == 0           # 队列已空
        assert st.active_path == missing.resolve()  # 只有新失败文件 → 激活之
        entries = st.file_entries()
        assert [e.loaded for e in entries] == [True, False]
        assert [d.code for d in st.failed_diagnostics(missing.resolve())] \
            == ["E-READ"]
    finally:
        mgr.stop_all()


def test_handoff_mixed_batch_activates_first_new_only(files):
    """U-13 混合批次：首个新文件激活；已打开者仅聚焦（不重复打开）。"""
    ok, ok2, _missing = files
    mgr = SessionManager()
    st = GuiState(mgr)
    try:
        st.register_results(mgr.open_files([ok]))
        st.activate(ok.resolve())
        st.register_results(mgr.open_files([ok2]))  # ok2 已打开，非活动
        st.activate(ok.resolve())
        # 模拟 cli：转交 [已打开 ok2, 新文件 ok3]
        ok3 = ok.parent / "third.yaml"
        ok3.write_text("q:\n  r: 7\n", encoding="utf-8")
        st.enqueue_handoff(mgr.open_files([ok2, ok3]))
        st.process_handoffs()
        assert st.active_path == ok3.resolve()      # 新文件优先激活
        assert mgr.get(ok2.resolve()) is mgr.get(ok2.resolve())  # 未重建
        # 全部已打开批次 → 仅聚焦清单第一个
        st.enqueue_handoff(mgr.open_files([ok, ok2]))
        st.process_handoffs()
        assert st.active_path == ok.resolve()
    finally:
        mgr.stop_all()


def test_reload_failed_reopens_and_activates(files):
    ok, _ok2, missing = files
    mgr = SessionManager()
    st = GuiState(mgr)
    try:
        st.register_results(mgr.open_files([ok, missing]))
        st.activate(missing.resolve())
        assert st.active_session() is None
        missing.write_text("a:\n  b: 1\n", encoding="utf-8")  # 症结解除
        result = st.reload_failed(missing.resolve())
        assert result is not None and result.session is not None
        assert st.active_path == missing.resolve()
        assert st.active_session() is not None      # 诊断态解除、可编辑
        assert st.failed_diagnostics(missing.resolve()) == []
        assert [e.name for e in st.file_entries()] == ["cfg.yaml", "nope.yaml"]
    finally:
        mgr.stop_all()


def test_run_gui_registers_handoff_open(monkeypatch, capsys):
    """U-13 接缝：run_gui 经 on_handoff_open kwarg 注册 GUI 投递入口
    （enqueue_handoff）；注册的回调把转交批次送进该 state 的队列。"""
    monkeypatch.setattr("nicegui.ui.run", lambda root, **kw: None)
    registered: list = []

    def on_handoff_open(cb) -> None:     # cli 侧注册器替身
        registered.append(cb)

    mgr = SessionManager()
    assert run_gui(mgr, GuiOptions(), on_handoff_open=on_handoff_open) == 0
    assert len(registered) == 1
    cb = registered[0]
    assert cb.__func__ is GuiState.enqueue_handoff
    state = cb.__self__                   # 注册入口绑定在该 GuiState 上
    cb([])                                # 空批次投递（cli 信箱补送同路径）
    assert state.handoff_queue.qsize() == 1
    assert state.process_handoffs() == 1


def test_run_gui_without_handoff_open_keeps_main_contract(monkeypatch):
    """主契约不变：不传 kwarg 一切照旧（回调注册器缺省 None）。"""
    monkeypatch.setattr("nicegui.ui.run", lambda root, **kw: None)
    assert run_gui(SessionManager(), GuiOptions()) == 0


def test_state_events_threadsafe_drain(files):
    ok, _ok2, _missing = files
    mgr = SessionManager()
    st = GuiState(mgr)
    try:
        st.register_results(mgr.open_files([ok]))
        cb = st.make_state_callback(ok.resolve())

        def worker():
            cb("external_modified")
            cb("gone")

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        events = st.drain_events()
        assert events == [(ok.resolve(), "external_modified"), (ok.resolve(), "gone")]
        assert st.drain_events() == []
    finally:
        mgr.stop_all()


def test_state_ui_toggles_and_shutdown_hook():
    st = GuiState(SessionManager())
    assert st.sidebar_visible
    st.toggle_sidebar()
    assert not st.sidebar_visible
    st.toggle_sidebar()
    assert st.sidebar_visible
    st.set_search("abc")
    assert st.search_query == "abc"
    st.exit_code = 0
    assert st.on_app_shutdown() is None  # G2 钩子：退出 flush 未接线
    assert st.exit_code == 0
    # 无 ui_refresh 注入时 touch 静默（无头安全）
    st.touch()


# ---------------------------------------------------------------------------
# build_gui 无头构建（结构冒烟：不真起 ui.run）
# ---------------------------------------------------------------------------


def test_build_gui_headless(files):
    from configer.ui.layout import build_gui

    ok, _ok2, missing = files
    mgr = SessionManager()
    st = GuiState(mgr)
    try:
        st.register_results(mgr.open_files([ok, missing]))
        build_gui(st)                      # 构建 auto-index 根页面内容
        assert st.ui_refresh is not None   # 刷新回调已注入
        assert st.active_path == ok.resolve()  # 首帧自动激活第一个文件
        st.ui_refresh()                    # core.loop=None → refresh 防御跳过
        st.select_item("game.player_id")   # touch → ui_refresh → 无异常
        st.activate(missing.resolve())
        st.ui_refresh()
    finally:
        mgr.stop_all()


def test_build_gui_splitters_are_left_right(files):
    """布局回归断言（统筹方终验缺陷）：两处 splitter 均须左右分割。

    Quasar/NiceGUI 的 ``horizontal=True`` 语义是**上下分割**（面板竖向
    叠放，渲染类 ``q-splitter--horizontal column``，面板高=容器高×value%，
    容器 auto 高度时整体塌陷）；IDE 布局（U-1：侧栏 | 主区，条目树 | 详情）
    语义上是左右分割，必须用默认 vertical（分隔线竖直、面板按宽分）。
    """
    from nicegui.elements.splitter import Splitter

    from configer.ui.layout import build_gui

    ok, _ok2, _missing = files
    mgr = SessionManager()
    st = GuiState(mgr)
    try:
        st.register_results(mgr.open_files([ok]))
        build_gui(st)
        root = st.modal_host.root
        top = list(root.ancestors())[-1]
        splitters = [e for e in top.descendants() if isinstance(e, Splitter)]
        # 无头测试共享 auto-index client：早前测试构建的元素仍挂在树上，
        # 按 id 取**本次构建**新建的两棵 splitter（id 单调递增、不复用；
        # 构建顺序外层先于编辑器，故取 id 最大的两个）。
        splitters = sorted(splitters, key=lambda e: e.id)[-2:]
        assert len(splitters) == 2           # 外层（侧栏|主区）+ 编辑器（树|详情）
        for sp in splitters:
            assert sp._props.get("horizontal") is not True   # 左右分割
            assert sp.value is not None      # 初始分割比已设
        outer, editor = splitters
        assert tuple(outer._props.get("limits")) == (0, 60)    # 侧栏 0~60%
        assert tuple(editor._props.get("limits")) == (20, 80)  # 树 20~80%
    finally:
        mgr.stop_all()
