"""命令行入口（规范 §9）。

契约（§9.1，normative）::

    configer open <file> [<file> ...] [--format auto|python|yaml]
                  [--poll-interval <秒>] [--host <addr>] [--port <n>]
    configer --version
    configer --help

- ``open`` 是 v1 唯一子命令；``check`` 为 **v1.5 占位**（v0.4 决议）：调用
  即 stderr 打"v1 未实现（v1.5 占位，§9.1）"→ exit 2（裁量登记）。
- ``--format`` 作用于全部文件；显式指定时直接选定适配器，load 失败 →
  exit 2，**不得**回退其他适配器（§4.2，A-10）。
- 部分文件加载失败不得阻断会话（stderr 打诊断摘要）；全部失败无法进入
  GUI → exit 2 且 stderr 打印诊断摘要（code + message，§9.2）。
- 退出码（§9.2）：0 正常（含单实例转交完成，§9.4）/ 1 启动阶段取消 /
  2 加载失败或参数错误 / 3 落盘失败 / ≥4 保留不得使用。
- 单实例与转交（§9.4）见 :mod:`configer.single_instance`：HANDED_OFF →
  stdout 一行转交提示 → exit 0（未发生写盘）；陈旧锁自动清理；
  K-11——单实例机制任何异常都不得阻塞用户打开文件（降级继续启动）。

GUI 接缝契约（与 GUI 工程师定死，双方照此；写在 docstring 供 G2 对照）::

    from configer.ui.app import run_gui, GuiOptions
    GuiOptions(host: str | None, port: int | None, poll_interval: float)
    rc = run_gui(manager, GuiOptions(...), on_handoff_open=None)  # kwarg 可选

- ``manager`` 为 :class:`configer.core.session.SessionManager`（pollers 已
  启动）；``manager.last_open_results`` = 启动批次完整 OpenResult 列表
  （含失败文件诊断态，GUI 启动时登记侧栏，U-10）。
- ``on_handoff_open``（G2c，可选关键字参数，主契约不变）：CLI 提供的
  **回调注册器**。run_gui 创建 GuiState 后立即以 ``state.enqueue_handoff``
  （线程安全投递入口）调用之；此后单实例转交（§9.4/U-13）在 CLI 监听
  线程 open 出的完整批次经该入口进 GUI，主循环 drain timer 消费。注册前
  到达 / GUI 未就绪的转交由 CLI 信箱（_HandoffBridge）缓冲，注册时补送；
  GUI 始终未就绪（NotImplementedError 过渡路径）时保底 = 文件已 open 进
  manager（登记：激活/诊断呈现缺失，随进程退出终止）。
- ``run_gui`` 返回码语义（§7.7 / §9.2）：0=正常结束（退出 flush 已在
  run_gui 内部完成）；1=启动阶段取消（未进入会话，未发生写盘）；3=退出
  flush 落盘失败被用户放弃。CLI 原样透传该返回码。
- 过渡期（裁量登记）：``run_gui`` 抛 ``NotImplementedError`` 或
  ``configer.ui.app`` 尚不可 import → stderr 提示 + stop_all → exit 2；
  G2 完工后该分支自然消失。
- 测试可注入 ``gui_runner``（签名同 ``run_gui``，含关键字参数），优先于
  懒 import。

console_script ``configer = configer.cli:main`` 已在 pyproject.toml 登记。
"""

from __future__ import annotations

import argparse
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__, single_instance
from .core.session import OpenResult, SessionManager
from .registry import default_registry

__all__ = ["main"]

_FORMATS = ("auto", "python", "yaml")
_DEFAULT_POLL_INTERVAL = 2.0  # §9.1：默认 2 秒


@dataclass
class _FallbackGuiOptions:
    """``configer.ui.app.GuiOptions`` 不可 import 时的过渡替身（字段同契约）。

    仅在测试注入 ``gui_runner`` 且 GUI 模块尚未就位时使用（裁量登记）；
    G2 完工后默认路径永远走真实 GuiOptions。
    """

    host: str | None = None
    port: int | None = None
    poll_interval: float = _DEFAULT_POLL_INTERVAL


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="configer",
        description="配置文件 GUI 编辑器（规范 docs/spec.md v0.4）",
    )
    parser.add_argument(
        "--version", action="version", version=f"configer {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    open_p = sub.add_parser("open", help="打开一个或多个配置文件（§9.1）")
    open_p.add_argument("file", nargs="+", type=str, help="配置文件路径")
    open_p.add_argument(
        "--format", choices=_FORMATS, default="auto",
        help="格式提示，作用于全部文件（默认 auto，§4.2）",
    )
    open_p.add_argument(
        "--poll-interval", type=float, default=_DEFAULT_POLL_INTERVAL,
        help="外部修改轮询周期秒数（§7.6，默认 2）",
    )
    open_p.add_argument(
        "--host", type=str, default=None,
        help="浏览器模式监听地址显式覆盖（默认仅 127.0.0.1，§9.1）",
    )
    open_p.add_argument(
        "--port", type=int, default=None,
        help="浏览器模式监听端口显式覆盖（默认随机端口，§9.1）",
    )

    check_p = sub.add_parser(
        "check", help="只读校验（v1.5 占位，v1 不实现，§9.1）"
    )
    check_p.add_argument("file", nargs="*", type=str)

    return parser


def _usage_error(parser: argparse.ArgumentParser, message: str) -> int:
    """参数错误 → stderr 输出原因 + exit 2（§9.1/§9.2）。"""
    parser.print_usage(sys.stderr)
    print(f"configer: 参数错误：{message}", file=sys.stderr)
    return 2


def _print_diagnostic_summary(failures: list[OpenResult]) -> None:
    """加载失败诊断摘要（§9.2：必须在 stderr 打印 code + message）。"""
    for result in failures:
        diags = result.diagnostics or []
        if not diags:  # 防御：无诊断也给一行可定位信息
            print(f"{result.path}: 打开失败（无诊断详情）", file=sys.stderr)
        for diag in diags:
            print(
                f"{result.path}: [{diag.severity}] {diag.code}: "
                f"{diag.message}",
                file=sys.stderr,
            )


def _make_gui_options(args: argparse.Namespace) -> Any:
    """构造 GuiOptions（接缝契约）；ui.app 不可用时退回过渡替身。"""
    try:
        from .ui.app import GuiOptions  # 懒 import（接缝契约）
    except Exception:
        GuiOptions = None
    if GuiOptions is not None:
        return GuiOptions(
            host=args.host, port=args.port,
            poll_interval=args.poll_interval,
        )
    return _FallbackGuiOptions(
        host=args.host, port=args.port, poll_interval=args.poll_interval
    )


class _HandoffBridge:
    """CLI↔GUI 单实例转交接缝（线程安全信箱，G2c）。

    单实例服务器线程收到转交 → CLI 完成 ``manager.open_files`` →
    :meth:`deliver` 投递完整 OpenResult 批次（含失败文件诊断态）：

    - GUI 已注册（run_gui 经 ``on_handoff_open`` kwarg 调
      :meth:`register`）→ 立即转交 GUI 投递入口（enqueue_handoff，只入
      队，线程安全）；
    - GUI 未注册（GUI 未启动/过渡期无 GUI）→ 缓冲于信箱，注册时补送；
      GUI 始终未就绪 → 随进程退出丢弃（保底：文件已 open 进 manager，
      激活/诊断呈现缺失，登记于模块 docstring）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[list[OpenResult]] = []
        self._gui_deliver: Callable[[list[OpenResult]], None] | None = None

    def register(
        self, gui_deliver: Callable[[list[OpenResult]], None]
    ) -> None:
        """run_gui 注册 GUI 投递入口；信箱中缓冲的转交批次在此补送。"""
        with self._lock:
            self._gui_deliver = gui_deliver
            pending, self._pending = self._pending, []
        for results in pending:
            gui_deliver(results)

    def deliver(self, results: list[OpenResult]) -> None:
        """服务器线程调用：投递一批转交结果（GUI 未注册则缓冲）。"""
        with self._lock:
            gui_deliver = self._gui_deliver
            if gui_deliver is None:
                self._pending.append(list(results))
                return
        gui_deliver(list(results))


def _run_gui_stage(
    manager: SessionManager,
    args: argparse.Namespace,
    gui_runner: Callable[[SessionManager, Any], int] | None,
    handoff_bridge: _HandoffBridge | None = None,
) -> int:
    """进入 GUI 阶段；返回退出码（§9.2）。

    - ``gui_runner`` 注入优先（测试用），签名同 ``run_gui``（含
      ``on_handoff_open`` 关键字参数）；
    - 默认懒 import ``configer.ui.app.run_gui``（接缝契约，见模块
      docstring）；返回码 0/1/3 原样透传；
    - 过渡期：run_gui 抛 NotImplementedError 或 ui.app 不可 import →
      stderr 提示 → exit 2（stop_all 由 main 的 finally 完成）。
    """
    options = _make_gui_options(args)
    on_handoff_open = (
        handoff_bridge.register if handoff_bridge is not None else None
    )
    if gui_runner is not None:
        try:
            return int(
                gui_runner(manager, options, on_handoff_open=on_handoff_open)
            )
        except NotImplementedError as exc:
            print(
                f"configer: GUI 尚未就绪（{exc}），无法进入图形界面"
                "（过渡期行为，§9）。",
                file=sys.stderr,
            )
            return 2
    try:
        from .ui.app import run_gui  # 懒 import（接缝契约）
    except ImportError:
        print(
            "configer: GUI 尚未就绪（configer.ui.app 不可导入），"
            "无法进入图形界面（过渡期行为，§9）。",
            file=sys.stderr,
        )
        return 2
    try:
        return int(
            run_gui(manager, options, on_handoff_open=on_handoff_open)
        )
    except NotImplementedError as exc:
        print(
            f"configer: GUI 尚未就绪（{exc}），无法进入图形界面"
            "（过渡期行为，§9）。",
            file=sys.stderr,
        )
        return 2


def _cmd_open(
    args: argparse.Namespace,
    gui_runner: Callable[[SessionManager, Any], int] | None,
) -> int:
    """``open`` 子命令主流程（§9.1 ①–⑥）。"""
    paths = [Path(p) for p in args.file]

    # ① 单实例（§9.4）：HANDED_OFF → exit 0（未发生写盘）。
    #    K-11：机制自身异常一律降级为继续启动，不阻塞用户打开文件。
    handoff_holder: dict[str, Any] = {}
    bridge = _HandoffBridge()

    def _on_handoff_received(handed: list[Path]) -> None:
        # G2c：本进程（首实例）收到第二实例转交的文件清单——在服务器
        # 线程 open（format/poll-interval 同本次启动参数），完整批次
        # （含失败文件诊断态）经信箱交 GUI（U-13：未打开加入并激活、
        # 已打开仅聚焦；U-10：失败文件诊断态进侧栏）。GUI 未注册则由
        # 信箱缓冲/保底（登记见模块 docstring）。
        manager = handoff_holder.get("manager")
        if manager is None:
            # 理论窗口极小（服务器先于 manager 创建，毫秒级）：ack 已发、
            # 文件未开——登记为已知局限（K-11：不阻塞第二实例退出）。
            print(
                "configer: 转交到达早于会话管理器就绪，本次清单被忽略。",
                file=sys.stderr,
            )
            return
        results = manager.open_files(
            handed, format_hint=args.format,
            registry=default_registry(),
            poll_interval=args.poll_interval,
        )
        for result in results:
            if result.session is not None:
                result.session.start_poller()   # 幂等；GUI 未起也能轮询
        bridge.deliver(results)

    acquired = None
    try:
        acquired = single_instance.acquire_or_handoff(
            paths, on_handoff_received=_on_handoff_received
        )
    except Exception as exc:  # 防御：K-11 不得阻塞打开
        print(
            f"configer: 单实例检测失败（{exc}），按首实例继续启动。",
            file=sys.stderr,
        )
    if acquired is not None and acquired.status == single_instance.HANDED_OFF:
        print("configer: 已转交给运行中的 configer 实例，本进程退出。")
        return 0

    manager = SessionManager()
    handoff_holder["manager"] = manager
    try:
        # ② 批量打开（§3.10：部分失败不阻断）
        results = manager.open_files(
            paths,
            format_hint=args.format,
            registry=default_registry(),
            poll_interval=args.poll_interval,
        )
        manager.last_open_results = results   # U-10 接缝：完整批次交 GUI
        # ③ 失败分流（§9.1/§9.2）
        failures = [r for r in results if r.session is None]
        if failures:
            _print_diagnostic_summary(failures)
            if len(failures) == len(results):
                return 2  # 全部失败，无法进入 GUI
        # ④ 外部修改轮询（§7.6）
        manager.start_pollers()
        # ⑤ GUI 阶段（接缝契约见模块 docstring）
        return _run_gui_stage(manager, args, gui_runner, handoff_bridge=bridge)
    finally:
        # ⑥ 清理：停轮询 + 释放单实例登记（幂等）
        manager.stop_all()
        single_instance.release()


def main(
    argv: list[str] | None = None,
    gui_runner: Callable[[SessionManager, Any], int] | None = None,
) -> int:
    """CLI 入口（§9.1），返回退出码（§9.2）。

    ``gui_runner`` 为测试注入的 GUI 替身（签名同接缝契约 ``run_gui``）。
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse：--version/--help→0，参数错误→2
        code = exc.code
        return code if isinstance(code, int) else 2

    if args.command == "open":
        # 参数合法性补充校验（argparse type 之外，§9.1：错误 → exit 2）
        if not args.poll_interval > 0:
            return _usage_error(
                parser, f"--poll-interval 必须为正数（得到 {args.poll_interval}）"
            )
        if args.port is not None and not (0 < args.port < 65536):
            return _usage_error(
                parser, f"--port 必须在 1..65535（得到 {args.port}）"
            )
        return _cmd_open(args, gui_runner)

    if args.command == "check":
        # v1.5 占位（v0.4 决议，裁量登记）
        print("configer: check 子命令 v1 未实现（v1.5 占位，§9.1）。",
              file=sys.stderr)
        return 2

    return _usage_error(parser, "缺少子命令（open / check）")


if __name__ == "__main__":
    sys.exit(main())
