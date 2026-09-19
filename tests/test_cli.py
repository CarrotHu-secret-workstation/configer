"""configer.cli 单元测试（规范 §9.1 命令行、§9.2 退出码、§9.4 转交、A-10）。

GUI 用注入的 ``gui_runner`` 替身（接缝契约签名同 run_gui）；单实例层用
monkeypatch 替身——不碰真实 /tmp socket。金样本从 testdata 拷贝到 tmp_path
（testdata 只读纪律）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from configer import __version__, cli
from configer import single_instance as si
from configer.core.session import SessionManager

TESTDATA = Path(__file__).resolve().parent.parent / "testdata"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _block_ui_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """密闭性：阻断 configer.ui.app（并行开发中，G1/G2）——cli 的懒 import
    一律落入过渡期分支 / GuiOptions 回退替身，测试不触真实 NiceGUI。"""
    import sys
    monkeypatch.setitem(sys.modules, "configer.ui.app", None)


@pytest.fixture()
def golden(tmp_path: Path) -> tuple[Path, Path]:
    """(param.py, config.yaml) 的 tmp_path 拷贝（testdata 只读）。"""
    py = tmp_path / "param.py"
    yml = tmp_path / "config.yaml"
    py.write_bytes((TESTDATA / "param.py").read_bytes())
    yml.write_bytes((TESTDATA / "config.yaml").read_bytes())
    return py, yml


class FakeSingleInstance:
    """cli 视角的单实例替身：可配置结果 / 抛异常，记录 release 次数。

    G2c：记录 on_handoff_received 回调（``handoff_cb``），供测试在 GUI
    阶段模拟第二实例转交（真实实现由服务器线程调用）。
    """

    def __init__(self, result: si.HandoffResult | None = None,
                 exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.acquire_calls: list[list[Path]] = []
        self.release_calls = 0
        self.handoff_cb = None

    def acquire_or_handoff(self, paths, on_handoff_received=None,
                           timeout=2.0, **kwargs) -> si.HandoffResult:
        self.acquire_calls.append(list(paths))
        self.handoff_cb = on_handoff_received
        if self.exc is not None:
            raise self.exc
        if self.result is not None:
            return self.result
        return si.HandoffResult(status=si.ACQUIRED,
                                socket_path=Path("/fake/never.sock"))

    def release(self) -> None:
        self.release_calls += 1


@pytest.fixture()
def fake_si(monkeypatch: pytest.MonkeyPatch) -> FakeSingleInstance:
    fake = FakeSingleInstance()
    monkeypatch.setattr(cli.single_instance, "acquire_or_handoff",
                        fake.acquire_or_handoff)
    monkeypatch.setattr(cli.single_instance, "release", fake.release)
    return fake


@pytest.fixture()
def handoff_si(monkeypatch: pytest.MonkeyPatch) -> FakeSingleInstance:
    """单实例替身：第二实例视角（HANDED_OFF）。"""
    fake = FakeSingleInstance(
        result=si.HandoffResult(status=si.HANDED_OFF,
                                socket_path=Path("/fake/x.sock"),
                                handed_off_paths=[Path("/fake/a.yaml")])
    )
    monkeypatch.setattr(cli.single_instance, "acquire_or_handoff",
                        fake.acquire_or_handoff)
    monkeypatch.setattr(cli.single_instance, "release", fake.release)
    return fake


class FakeRunner:
    """gui_runner 替身（接缝契约：run_gui(manager, options, **kw) -> int）。

    G2c：记录关键字参数（on_handoff_open 回调注册器），可选 ``func``
    钩子在调用点执行（模拟 GUI 就绪后触发单实例转交回调等场景）。
    """

    def __init__(self, rc: int = 0, exc: Exception | None = None,
                 func: Any = None) -> None:
        self.rc = rc
        self.exc = exc
        self.func = func
        self.calls: list[tuple[SessionManager, Any]] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, manager: SessionManager, options: Any,
                 **kwargs: Any) -> int:
        self.calls.append((manager, options))
        self.kwargs.append(kwargs)
        if self.func is not None:
            self.func(manager, options, kwargs)
        if self.exc is not None:
            raise self.exc
        return self.rc

    @property
    def managers(self) -> list[SessionManager]:
        return [m for m, _ in self.calls]


@pytest.fixture()
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture()
def lifecycle(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """记录 SessionManager.start_pollers / stop_all 调用次数（仍执行原逻辑）。"""
    counts = {"start_pollers": 0, "stop_all": 0}
    orig_start = SessionManager.start_pollers
    orig_stop = SessionManager.stop_all

    def start(self):  # type: ignore[no-untyped-def]
        counts["start_pollers"] += 1
        return orig_start(self)

    def stop(self):  # type: ignore[no-untyped-def]
        counts["stop_all"] += 1
        return orig_stop(self)

    monkeypatch.setattr(cli.SessionManager, "start_pollers", start)
    monkeypatch.setattr(cli.SessionManager, "stop_all", stop)
    return counts


# ---------------------------------------------------------------------------
# --version / --help / 参数错误（§9.1/§9.2 → exit 2）
# ---------------------------------------------------------------------------

def test_version_prints_and_returns_0(capsys) -> None:
    assert cli.main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_help_returns_0(capsys) -> None:
    assert cli.main(["--help"]) == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_no_args_returns_2_with_reason(capsys) -> None:
    assert cli.main([]) == 2
    assert "子命令" in capsys.readouterr().err


def test_bad_format_value_returns_2(capsys, fake_si) -> None:
    assert cli.main(["open", "a.py", "--format", "toml"]) == 2
    assert "--format" in capsys.readouterr().err


def test_non_positive_poll_interval_returns_2(capsys, fake_si) -> None:
    assert cli.main(["open", "a.py", "--poll-interval", "0"]) == 2
    err = capsys.readouterr().err
    assert "poll-interval" in err and "正数" in err
    assert cli.main(["open", "a.py", "--poll-interval", "-1.5"]) == 2


def test_non_numeric_poll_interval_returns_2(capsys, fake_si) -> None:
    assert cli.main(["open", "a.py", "--poll-interval", "abc"]) == 2
    assert capsys.readouterr().err


def test_bad_port_returns_2(capsys, fake_si) -> None:
    assert cli.main(["open", "a.py", "--port", "70000"]) == 2
    assert "port" in capsys.readouterr().err


def test_open_without_file_returns_2(capsys, fake_si) -> None:
    assert cli.main(["open"]) == 2
    assert capsys.readouterr().err


def test_check_is_v15_placeholder_returns_2(capsys, fake_si) -> None:
    # 裁量：check → stderr "v1 未实现（v1.5 占位，§9.1）" → exit 2
    assert cli.main(["check", "a.py"]) == 2
    err = capsys.readouterr().err
    assert "v1.5 占位" in err and "未实现" in err


# ---------------------------------------------------------------------------
# open 正常路径（§9.1 ①–⑤）
# ---------------------------------------------------------------------------

def test_open_single_yaml(golden, runner, fake_si, lifecycle, capsys) -> None:
    py, yml = golden
    assert cli.main(["open", str(yml)], gui_runner=runner) == 0
    assert len(runner.calls) == 1
    manager, options = runner.calls[0]
    assert len(manager.sessions) == 1
    assert manager.get(yml) is not None
    assert options.poll_interval == 2.0  # 默认（§9.1）
    assert options.host is None and options.port is None
    assert lifecycle["start_pollers"] == 1  # ④ 轮询已启动
    assert lifecycle["stop_all"] == 1        # ⑥ finally 清理
    assert fake_si.release_calls == 1        # ⑥ 单实例释放
    assert capsys.readouterr().err == ""     # 无失败 → stderr 干净


def test_open_two_files_yaml_and_python(golden, runner, fake_si, lifecycle) -> None:
    # A-10 / U-2：configer open param.py config.yaml 两文件同时入会话
    py, yml = golden
    assert cli.main(["open", str(py), str(yml)], gui_runner=runner) == 0
    manager = runner.managers[-1]
    assert len(manager.sessions) == 2
    assert manager.get(py) is not None and manager.get(yml) is not None


def test_poll_interval_passed_through_to_session(golden, runner, fake_si) -> None:
    py, yml = golden
    assert cli.main(["open", str(yml), "--poll-interval", "0.5"],
                    gui_runner=runner) == 0
    session = runner.managers[-1].get(yml)
    assert session is not None
    assert session.poller.interval == pytest.approx(0.5)


def test_host_port_passed_to_gui_options(golden, runner, fake_si) -> None:
    # 接缝契约：GuiOptions(host, port, poll_interval) 透传（§9.1 显式覆盖）
    py, yml = golden
    assert cli.main(["open", str(yml), "--host", "127.0.0.1",
                     "--port", "8081", "--poll-interval", "0.7"],
                    gui_runner=runner) == 0
    _, options = runner.calls[-1]
    assert options.host == "127.0.0.1"
    assert options.port == 8081
    assert options.poll_interval == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# A-10：--format yaml 打开 .py（§4.2 不得回退，§9.2 exit 2）
# ---------------------------------------------------------------------------

def test_a10_format_yaml_on_py_single_file_exits_2(
    golden, runner, fake_si, lifecycle, capsys
) -> None:
    py, yml = golden
    assert cli.main(["open", str(py), "--format", "yaml"],
                    gui_runner=runner) == 2
    err = capsys.readouterr().err
    assert "E-PARSE" in err            # 诊断摘要 code + message（§9.2）
    assert str(py) in err
    assert runner.calls == []          # 全部失败 → 不进 GUI
    assert lifecycle["stop_all"] == 1  # finally 仍清理


def test_a10_format_yaml_on_py_multi_file_partial_failure_continues(
    golden, runner, fake_si, lifecycle, capsys
) -> None:
    # §9.1：部分失败不阻断——py 失败进 stderr 摘要，yaml 照常入会话
    py, yml = golden
    assert cli.main(["open", str(py), str(yml), "--format", "yaml"],
                    gui_runner=runner) == 0
    err = capsys.readouterr().err
    assert "E-PARSE" in err and str(py) in err
    manager = runner.managers[-1]
    assert len(manager.sessions) == 1
    assert manager.get(yml) is not None
    assert manager.get(py) is None


def test_all_paths_missing_exits_2_with_e_read(
    runner, fake_si, lifecycle, capsys, tmp_path
) -> None:
    missing = [tmp_path / "no_a.yaml", tmp_path / "no_b.py"]
    assert cli.main(["open", *[str(p) for p in missing]],
                    gui_runner=runner) == 2
    err = capsys.readouterr().err
    assert err.count("E-READ") == 2    # 每条失败都有 code+message 摘要
    assert runner.calls == []
    assert lifecycle["start_pollers"] == 0  # 全部失败不进轮询/GUI


# ---------------------------------------------------------------------------
# GUI 阶段退出码透传与过渡期行为（接缝契约）
# ---------------------------------------------------------------------------

def test_runner_return_codes_pass_through(golden, fake_si) -> None:
    py, yml = golden
    for rc in (0, 1, 3):  # §9.2：0 正常 / 1 启动取消 / 3 落盘失败被放弃
        assert cli.main(["open", str(yml)], gui_runner=FakeRunner(rc=rc)) == rc


def test_runner_not_implemented_error_exits_2(
    golden, fake_si, lifecycle, capsys
) -> None:
    # 过渡期（裁量）：GUI 未就绪 → stderr 提示 + stop_all → exit 2
    py, yml = golden
    boom = FakeRunner(exc=NotImplementedError("GUI 在 G1/G2 实现"))
    assert cli.main(["open", str(yml)], gui_runner=boom) == 2
    assert "尚未就绪" in capsys.readouterr().err
    assert lifecycle["stop_all"] == 1


def test_no_runner_and_ui_app_missing_exits_2(
    golden, fake_si, lifecycle, capsys, monkeypatch
) -> None:
    # 未注入 runner 且 configer.ui.app 不可 import → 过渡期 exit 2。
    # sys.modules 置 None 阻断 import（对并行开发中的真实 ui.app 免疫；
    # 相对 import 会以 ImportError 抛出，命中 cli 的过渡期分支）。
    import sys
    monkeypatch.setitem(sys.modules, "configer.ui.app", None)
    py, yml = golden
    assert cli.main(["open", str(yml)]) == 2
    assert "尚未就绪" in capsys.readouterr().err
    assert lifecycle["stop_all"] == 1


# ---------------------------------------------------------------------------
# 单实例（§9.4）：HANDED_OFF → exit 0；机制异常降级（K-11）
# ---------------------------------------------------------------------------

def test_handed_off_exits_0_without_gui(
    golden, handoff_si, lifecycle, capsys
) -> None:
    py, yml = golden
    runner = FakeRunner()
    assert cli.main(["open", str(yml)], gui_runner=runner) == 0
    assert runner.calls == []                 # 未进 GUI、未开会话
    assert lifecycle["start_pollers"] == 0
    out = capsys.readouterr().out
    assert "转交" in out                       # 一行转交提示（裁量）
    # 第二实例不持有任何登记，HANDED_OFF 路径无需 release（acquire 都没发生
    # 登记；真实实现里 release() 也是幂等 no-op）
    assert handoff_si.release_calls == 0


def test_acquire_receives_requested_paths(golden, fake_si, runner) -> None:
    py, yml = golden
    cli.main(["open", str(py), str(yml)], gui_runner=runner)
    assert fake_si.acquire_calls == [[Path(py), Path(yml)]]


def test_single_instance_failure_degrades_and_continues(
    golden, monkeypatch, runner, capsys
) -> None:
    # K-11：单实例机制自身异常不得阻塞用户打开文件 → 降级继续启动
    fake = FakeSingleInstance(exc=RuntimeError("socket boom"))
    monkeypatch.setattr(cli.single_instance, "acquire_or_handoff",
                        fake.acquire_or_handoff)
    monkeypatch.setattr(cli.single_instance, "release", fake.release)
    py, yml = golden
    assert cli.main(["open", str(yml)], gui_runner=runner) == 0
    assert len(runner.managers[-1].sessions) == 1
    assert "单实例检测失败" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# G2c：CLI↔GUI 转交/失败诊断接缝（run_gui on_handoff_open kwarg、
# manager.last_open_results、信箱缓冲）
# ---------------------------------------------------------------------------

def test_last_open_results_seam_includes_failed(golden, fake_si, runner) -> None:
    # U-10 接缝：启动批次完整 OpenResult（含失败文件诊断）交 GUI
    py, yml = golden
    assert cli.main(["open", str(py), str(yml), "--format", "yaml"],
                    gui_runner=runner) == 0
    manager = runner.managers[-1]
    results = manager.last_open_results
    assert [r.session is not None for r in results] == [False, True]
    failed = results[0]
    assert failed.path == Path(py).resolve()
    assert [d.code for d in failed.diagnostics] == ["E-PARSE"]


def test_handoff_registered_to_gui_and_opens_into_manager(
    golden, monkeypatch
) -> None:
    # U-13 接缝全链（无头）：run_gui 注入替身收到 on_handoff_open 注册器
    # → GUI 注册投递入口 → 模拟第二实例转交第三文件 → cli open 进 manager
    # 且完整批次（OpenResult）送达 GUI 回调。
    fake = FakeSingleInstance()
    monkeypatch.setattr(cli.single_instance, "acquire_or_handoff",
                        fake.acquire_or_handoff)
    monkeypatch.setattr(cli.single_instance, "release", fake.release)
    py, yml = golden
    third = py.parent / "third.yaml"
    third.write_text("z:\n  w: 1\n", encoding="utf-8")
    delivered: list = []

    def trigger(manager: SessionManager, options: Any, kwargs: dict) -> None:
        register = kwargs["on_handoff_open"]
        assert callable(register)
        register(delivered.append)          # GUI 侧注册（enqueue 替身）
        fake.handoff_cb([third])            # 服务器线程转交语义（同步模拟）

    runner = FakeRunner(func=trigger)
    assert cli.main(["open", str(yml)], gui_runner=runner) == 0
    manager = runner.managers[-1]
    assert manager.get(third) is not None   # 转交文件已 open 进会话
    assert len(delivered) == 1              # 完整批次送达 GUI 回调
    batch = delivered[0]
    assert batch[0].path == third.resolve() and batch[0].session is not None


def test_handoff_before_gui_registration_is_buffered_then_flushed(
    golden, monkeypatch
) -> None:
    # 信箱：GUI 注册前到达的转交缓冲，注册时补送（GUI 未就绪窗口保底）。
    fake = FakeSingleInstance()
    monkeypatch.setattr(cli.single_instance, "acquire_or_handoff",
                        fake.acquire_or_handoff)
    monkeypatch.setattr(cli.single_instance, "release", fake.release)
    py, yml = golden
    extra = py.parent / "extra.yaml"
    extra.write_text("e:\n  f: 2\n", encoding="utf-8")
    delivered: list = []

    def trigger(manager: SessionManager, options: Any, kwargs: dict) -> None:
        fake.handoff_cb([extra])            # 先转交（GUI 未注册 → 缓冲）
        kwargs["on_handoff_open"](delivered.append)   # 注册 → 补送
        assert len(delivered) == 1          # 注册时同步 flush 信箱

    runner = FakeRunner(func=trigger)
    assert cli.main(["open", str(yml)], gui_runner=runner) == 0
    assert runner.managers[-1].get(extra) is not None


def test_handoff_arriving_before_manager_ready_is_reported(
    golden, monkeypatch, capsys
) -> None:
    # 已知局限（登记）：转交早于 manager 创建（毫秒级启动窗口）→ 提示 +
    # 忽略，不影响第二实例 ack 退出（K-11）。
    fake = FakeSingleInstance()

    def acquire(paths, on_handoff_received=None, timeout=2.0, **kwargs):
        fake.acquire_calls.append(list(paths))
        fake.handoff_cb = on_handoff_received
        on_handoff_received([Path("/never/x.yaml")])  # manager 尚不存在
        return si.HandoffResult(status=si.ACQUIRED,
                                socket_path=Path("/fake/n.sock"))

    monkeypatch.setattr(cli.single_instance, "acquire_or_handoff", acquire)
    monkeypatch.setattr(cli.single_instance, "release", fake.release)
    py, yml = golden
    assert cli.main(["open", str(yml)], gui_runner=FakeRunner()) == 0
    assert "早于会话管理器就绪" in capsys.readouterr().err
