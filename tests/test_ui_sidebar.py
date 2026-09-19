"""侧栏"+"入口（U-2）单测：zenity 系统文件选择器 + textarea 回退。

不真起 zenity / ui.run：解析与可用性判定是纯函数直测；子进程路径用
monkeypatch 替身 :func:`sidebar._open_via_picker` 的 ``create_subprocess_exec``
（asyncio.run 驱动）；打开落地经 :func:`sidebar._open_paths`（真实
SessionManager + GuiState，无头；toast 用 notify_fn 记录器替身）。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from configer.core.session import SessionManager
from configer.registry import Registry
from configer.ui import sidebar
from configer.ui.state import GuiState

YAML_OK = "# 顶\n游戏: 占值\ngame:\n  player_id: 3  # 1|2|3\nrobot:\n  h: 0.9\n"


class _FakeProc:
    """create_subprocess_exec 替身返回的假进程（communicate 一次出结果）。"""

    def __init__(self, returncode: int, stdout: bytes) -> None:
        self.returncode = returncode
        self._stdout = stdout

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""


class _HangingProc:
    """communicate 永久挂起的假进程（超时分支替身；记录 kill/wait 供断言）。"""

    def __init__(self) -> None:
        self.killed = False
        self.reaped = False

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.Event().wait()   # 永不完成 → wait_for 必超时

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.reaped = True             # kill 后被 await 回收（防僵尸）
        return -9


def _record_state(mgr: SessionManager) -> tuple[GuiState, list[tuple[str, str]]]:
    """无头 GuiState + notify 记录器（layout 的 ui.notify 注入替身）。"""
    st = GuiState(mgr)
    notified: list[tuple[str, str]] = []
    st.notify_fn = lambda msg, kind: notified.append((msg, kind))
    return st, notified


# ---------------------------------------------------------------------------
# zenity 输出解析（多路径 / 含空格路径 / 空输出）
# ---------------------------------------------------------------------------


def test_parse_picker_output_multi_and_spaces():
    """多路径与含空格路径：换行分隔符下空格不拆分、原样保留。"""
    out = "/tmp/a.yaml\n/home/pi/My Config/second file.yml\n"
    assert sidebar._parse_picker_output(out) == [
        Path("/tmp/a.yaml"),
        Path("/home/pi/My Config/second file.yml"),
    ]


def test_parse_picker_output_empty_and_blank_lines():
    """空输出 / 只有空行与空白：返回空列表（调用方按取消静默处理）。"""
    assert sidebar._parse_picker_output("") == []
    assert sidebar._parse_picker_output("\n \n\t\n") == []
    assert sidebar._parse_picker_output("  /tmp/x.py  \n") == [Path("/tmp/x.py")]


# ---------------------------------------------------------------------------
# 可用性判定（zenity 缺失 / 无 DISPLAY → 回退）
# ---------------------------------------------------------------------------


def test_picker_available_requires_zenity_and_display(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv("DISPLAY", ":1")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert sidebar._picker_available() is False      # 无 zenity 二进制

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/zenity")
    assert sidebar._picker_available() is True       # zenity + DISPLAY

    monkeypatch.delenv("DISPLAY")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert sidebar._picker_available() is False      # 无图形环境

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert sidebar._picker_available() is True       # Wayland 单独成立


def test_picker_args_shape(monkeypatch):
    monkeypatch.setattr(sidebar, "_last_pick_dir", None)
    args = sidebar._picker_args(Path("/tmp"))
    assert args[:2] == ["zenity", "--file-selection"]
    assert "--multiple" in args
    assert any(a.startswith("--separator=") and "\n" in a for a in args)
    assert "--filename=/tmp/" in args                 # 尾斜杠 = 目录起步
    assert any(a.startswith("--file-filter=配置文件") for a in args)
    assert "--file-filter=所有文件 | *" in args
    # 无起始目录时不带 --filename
    assert not any(a.startswith("--filename") for a in sidebar._picker_args(None))


def test_config_file_filters_from_registry():
    """过滤器扩展名来自适配器注册表（.py/.yaml/.yml）+ 所有文件兜底。"""
    filters = sidebar._config_file_filters()
    assert filters[-1] == "所有文件 | *"
    main = filters[0]
    assert main.startswith("配置文件 | ")
    for ext in (".py", ".yaml", ".yml"):
        assert f"*{ext}" in main


def test_config_file_filters_empty_registry(monkeypatch):
    """空注册表（聚合扩展名为空）：只剩所有文件兜底，不出无模式过滤器。"""
    monkeypatch.setattr(sidebar, "default_registry", lambda: Registry())
    assert sidebar._config_file_filters() == ["所有文件 | *"]


# ---------------------------------------------------------------------------
# _open_paths：打开落地 + 回执（成功 / 失败 / 混合）
# ---------------------------------------------------------------------------


def test_open_paths_mixed_batch(tmp_path):
    """混合批次：成功者入侧栏并激活 + positive toast；失败者 negative toast。"""
    ok = tmp_path / "cfg.yaml"
    ok.write_text(YAML_OK, encoding="utf-8")
    ok2 = tmp_path / "second.yaml"
    ok2.write_text("a:\n  b: 1\n", encoding="utf-8")
    missing = tmp_path / "nope.yaml"
    mgr = SessionManager()
    st, notified = _record_state(mgr)
    try:
        st.register_results(mgr.open_files([ok]))
        st.activate(ok.resolve())
        sidebar._open_paths(st, [ok2, missing])
        assert [e.name for e in st.file_entries()] == [
            "cfg.yaml", "second.yaml", "nope.yaml"]
        assert [e.loaded for e in st.file_entries()] == [True, True, False]
        assert st.active_path == ok2.resolve()       # 首个成功者激活
        assert notified[-1] == ("已打开 1 个文件", "positive")
        assert any(
            kind == "negative" and "nope.yaml" in msg
            for msg, kind in notified
        )
    finally:
        mgr.stop_all()


def test_open_paths_all_failed_keeps_active(tmp_path):
    """全失败批次：只 negative toast，活动文件不变、无 positive。"""
    ok = tmp_path / "cfg.yaml"
    ok.write_text(YAML_OK, encoding="utf-8")
    missing = tmp_path / "nope.yaml"
    mgr = SessionManager()
    st, notified = _record_state(mgr)
    try:
        st.register_results(mgr.open_files([ok]))
        st.activate(ok.resolve())
        sidebar._open_paths(st, [missing])
        assert st.active_path == ok.resolve()
        assert notified and all(kind == "negative" for _msg, kind in notified)
        assert [e.loaded for e in st.file_entries()] == [True, False]
    finally:
        mgr.stop_all()


def test_open_paths_empty_batch_noop(tmp_path):
    """空批次（zenity 空输出前置已拦，防御路径）：无通知、不激活。"""
    ok = tmp_path / "cfg.yaml"
    ok.write_text(YAML_OK, encoding="utf-8")
    mgr = SessionManager()
    st, notified = _record_state(mgr)
    try:
        st.register_results(mgr.open_files([ok]))
        st.activate(ok.resolve())
        sidebar._open_paths(st, [])
        assert notified == []
        assert st.active_path == ok.resolve()
    finally:
        mgr.stop_all()


# ---------------------------------------------------------------------------
# _open_via_picker：子进程路径（替身进程，asyncio.run 驱动）
# ---------------------------------------------------------------------------


def test_open_via_picker_success_multi(tmp_path, monkeypatch):
    """退出码 0：stdout 拆路径打开 + 激活首个成功者 + 记住起始目录。"""
    ok = tmp_path / "cfg.yaml"
    ok.write_text(YAML_OK, encoding="utf-8")
    ok2 = tmp_path / "second.yaml"
    ok2.write_text("a:\n  b: 1\n", encoding="utf-8")
    argv: list[str] = []

    async def fake_exec(*args, **kw):
        argv.extend(args)
        return _FakeProc(0, f"{ok}\n{ok2}\n".encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sidebar, "_last_pick_dir", None)
    mgr = SessionManager()
    st, notified = _record_state(mgr)
    try:
        asyncio.run(sidebar._open_via_picker(st))
        assert argv[0] == "zenity"                    # 确走 zenity 主路径
        assert [e.name for e in st.file_entries()] == [
            "cfg.yaml", "second.yaml"]
        assert st.active_path == ok.resolve()
        assert notified == [("已打开 2 个文件", "positive")]
        assert sidebar._last_pick_dir == tmp_path     # 下次从该目录起步
    finally:
        mgr.stop_all()


def test_open_via_picker_cancel_silent(tmp_path, monkeypatch):
    """退出码 1（用户取消）：静默，无任何状态变化。"""
    ok = tmp_path / "cfg.yaml"
    ok.write_text(YAML_OK, encoding="utf-8")
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        lambda *a, **kw: _coro(_FakeProc(1, b"")))
    mgr = SessionManager()
    st, notified = _record_state(mgr)
    try:
        st.register_results(mgr.open_files([ok]))
        st.activate(ok.resolve())
        asyncio.run(sidebar._open_via_picker(st))
        assert notified == []
        assert st.active_path == ok.resolve()
        assert [e.name for e in st.file_entries()] == ["cfg.yaml"]
    finally:
        mgr.stop_all()


def test_open_via_picker_empty_output_silent(tmp_path, monkeypatch):
    async def fake_exec(*args, **kw):
        return _FakeProc(0, b"")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sidebar, "_last_pick_dir", Path("/somewhere"))
    mgr = SessionManager()
    st, notified = _record_state(mgr)
    try:
        asyncio.run(sidebar._open_via_picker(st))
        assert notified == []                         # 空输出视同取消：静默
        assert st.file_entries() == []
        assert sidebar._last_pick_dir == Path("/somewhere")  # 未被覆盖
    finally:
        mgr.stop_all()


def test_open_via_picker_crash_and_spawn_failure(tmp_path, monkeypatch):
    """异常退出码 / 启动失败：negative toast，不触碰文件登记。"""
    mgr = SessionManager()
    st, notified = _record_state(mgr)
    try:
        async def crash_exec(*args, **kw):
            return _FakeProc(2, b"")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", crash_exec)
        asyncio.run(sidebar._open_via_picker(st))
        assert notified == [("文件选择器异常退出（码 2）。", "negative")]

        async def boom_exec(*args, **kw):
            raise FileNotFoundError("zenity vanished")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom_exec)
        asyncio.run(sidebar._open_via_picker(st))
        assert notified[-1] == ("无法启动系统文件选择器（zenity）。", "negative")
        assert st.file_entries() == []
    finally:
        mgr.stop_all()


def test_open_via_picker_timeout_kills_and_notifies(monkeypatch):
    """communicate 永久挂起：wait_for 超时 → kill + await 回收 + negative
    通知（超时缩短为 0.05s 触发，不真等 10 分钟）。"""
    monkeypatch.setattr(sidebar, "_PICKER_TIMEOUT", 0.05)
    proc = _HangingProc()

    async def hang_exec(*args, **kw):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", hang_exec)
    mgr = SessionManager()
    st, notified = _record_state(mgr)
    try:
        asyncio.run(sidebar._open_via_picker(st))
        assert proc.killed                      # 子进程被 kill
        assert proc.reaped                      # kill 后已 await 回收（防僵尸）
        assert notified == [("文件选择器无响应，已关闭。", "negative")]
        assert st.file_entries() == []          # 未误开文件
    finally:
        mgr.stop_all()


# ---------------------------------------------------------------------------
# 回退输入解析：_parse_text_paths（多行 / 空白行 / ~ 展开 / 空输入）
# ---------------------------------------------------------------------------


def test_parse_text_paths_multi_line_with_blanks():
    """多行输入：逐行 strip、跳过空行与空白行，路径原样保留（含空格）。"""
    raw = "/tmp/a.yaml\n\n   \n/home/pi/My Config/b.yml\n\t\n"
    assert sidebar._parse_text_paths(raw) == [
        Path("/tmp/a.yaml"),
        Path("/home/pi/My Config/b.yml"),
    ]


def test_parse_text_paths_expands_home():
    """``~`` 前缀展开到用户主目录（expanduser，含带空白与子目录）。"""
    assert sidebar._parse_text_paths("~/cfg.yaml") == [Path.home() / "cfg.yaml"]
    assert sidebar._parse_text_paths("  ~/sub/x.py ") == [
        Path.home() / "sub" / "x.py"]


def test_parse_text_paths_empty_input():
    """空输入 / 全空白：返回空列表（回退对话框 _confirm 据此早退）。"""
    assert sidebar._parse_text_paths("") == []
    assert sidebar._parse_text_paths("\n \n\t\n") == []


# ---------------------------------------------------------------------------
# _open_files：主路径 / 回退分流
# ---------------------------------------------------------------------------


def test_open_files_dispatches_fallback_without_picker(monkeypatch):
    """zenity 不可用 → 回退 textarea 对话框（_open_dialog）。"""
    calls: list[str] = []
    monkeypatch.setattr(sidebar, "_picker_available", lambda: False)
    monkeypatch.setattr(sidebar, "_open_dialog",
                        lambda st: calls.append("fallback"))
    mgr = SessionManager()
    try:
        asyncio.run(sidebar._open_files(GuiState(mgr)))
        assert calls == ["fallback"]
    finally:
        mgr.stop_all()


def test_open_files_dispatches_picker_when_available(monkeypatch):
    """zenity 可用 → 走系统选择器（不弹 textarea）。"""
    calls: list[str] = []

    async def fake_picker(st):
        calls.append("picker")

    monkeypatch.setattr(sidebar, "_picker_available", lambda: True)
    monkeypatch.setattr(sidebar, "_open_via_picker", fake_picker)
    monkeypatch.setattr(sidebar, "_open_dialog",
                        lambda st: calls.append("fallback"))
    mgr = SessionManager()
    try:
        asyncio.run(sidebar._open_files(GuiState(mgr)))
        assert calls == ["picker"]
    finally:
        mgr.stop_all()


async def _coro(value):
    """lambda 包装协程工厂（替 create_subprocess_exec 的异步返回）。"""
    return value
