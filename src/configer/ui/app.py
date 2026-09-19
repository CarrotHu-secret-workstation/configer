"""GUI 入口（规范 §10，接缝契约见下）。

接缝契约（与 CLI 定死）：CLI 懒 import :class:`GuiOptions` 与 :func:`run_gui`。

- ``GuiOptions.host=None`` → 绑定 ``127.0.0.1``（§10 v0.4 决议：浏览器模式
  默认不暴露局域网，远程走 SSH 端口转发）；
- ``GuiOptions.port=None`` → 随机可用端口（裁量：启动前先用 socket 绑定 0
  号端口取实际端口再传给 ``ui.run``——NiceGUI/uvicorn 无公开 API 拿"实际
  绑定端口"，此法有理论 TOCTOU 窗口，登记备查）；
- ``run_gui`` 阻塞运行；返回退出码 0=正常 / 1=启动阶段取消 / 3=落盘失败
  被放弃（§7.7/§9.2）。G2b 已接退出 flush：交互式路径 = header「退出」
  按钮走 §7.7 决策树（state.begin_exit）；非交互路径见下。

退出拦截（G2b 裁量登记，§7.7/任务书"做不到真拦截就把决策树做成退出前
必经流程"）：

- **浏览器标签关闭无法被服务端阻止**——NiceGUI 无 beforeunload 拦截 API，
  本层以 ``document.body.dataset.configerGuard`` + 注入 JS 做**原生离开
  提示**（仅当存在防抖计时中/未决暂态/paused 时武装，layout 侧同步）；
- **全部客户端断开 → 自动停服**：``app.on_disconnect`` + 3s 宽限（防页面
  刷新误判）后无连接客户端 → ``state.exit_noninteractive()``：flush 后全
  clean → exit 0；conflict/failed/paused → 无法弹模态（无客户端），视同
  「仍要退出」abandon + exit 3（局限登记见 dialogs.noninteractive_exit_code）；
- **Ctrl+C / 进程停服**：``app.on_shutdown`` → ``state.on_app_shutdown()``
  安全网补一次非交互 flush（交互式流程已 finalize 则幂等跳过）；
- **native 模式**（M3）：窗口关闭即 app shutdown → 同上安全网路径。

线程模型（裁量登记）：``FileSession.on_state_change`` 回调可能来自轮询
线程，UI 更新必须调度回主线程。方案：回调只向 :class:`GuiState` 的线程
安全事件队列投递标志，由 ``ui.timer``（主事件循环）轮询取出并刷新界面。
"""

from __future__ import annotations

import socket
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nicegui import app as nice_app
from nicegui import ui

from ..core.session import OpenResult, SessionManager
from .layout import build_gui
from .state import GuiState

__all__ = ["GuiOptions", "run_gui"]

DEFAULT_HOST = "127.0.0.1"  # §10 决议：不默认暴露局域网
DEFAULT_PORT = 8080
DISCONNECT_GRACE_S = 3.0    # 断连宽限（页面刷新会短暂断开，防误判自动停服）


@dataclass
class GuiOptions:
    """run_gui 的启动选项（接缝契约，字段名/默认值不得改）。"""

    host: str | None = None      # None → 127.0.0.1
    port: int | None = None      # None → 随机可用端口
    poll_interval: float = 2.0


def _find_free_port(host: str) -> int:
    """裁量：port=None 时预先取一个空闲端口。见模块 docstring 登记。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def _connected_client_count() -> int:
    """当前有 websocket 连接的客户端数（断连自动停服判定用）。"""
    try:
        from nicegui import Client
        return sum(
            1 for c in Client.instances.values()
            if getattr(c, "has_socket_connection", False)
            and not getattr(c, "is_deleted", False)
        )
    except Exception:
        return -1   # 判定不可用 → 视为仍有客户端（不自动停服，防御）


def _wire_exit(state: GuiState) -> None:
    """退出路径接线（模块 docstring"退出拦截"三条）。"""
    state.request_stop = lambda: nice_app.shutdown()

    nice_app.on_shutdown(state.on_app_shutdown)   # Ctrl+C / 停服安全网

    def _on_disconnect() -> None:
        # 宽限后复查：仍无任何连接客户端 → 非交互 flush + 停服（§7.7）
        def _check() -> None:
            if state.exit_finalized:
                return
            if _connected_client_count() == 0:
                state.exit_noninteractive()

        timer = threading.Timer(DISCONNECT_GRACE_S, _check)
        timer.daemon = True
        timer.start()

    nice_app.on_disconnect(_on_disconnect)


def run_gui(
    manager: SessionManager,
    options: GuiOptions,
    *,
    on_handoff_open: "Callable[[Callable[[list[OpenResult]], None]], None] | None" = None,
) -> int:
    """阻塞运行 GUI，返回退出码（0/1/3，§9.2）。

    ``on_handoff_open``（G2c 可选关键字参数，向后兼容——主契约
    ``run_gui(manager, options)`` 不变）：CLI 提供的**回调注册器**。
    run_gui 创建 :class:`GuiState` 后立即调用
    ``on_handoff_open(state.enqueue_handoff)``，把 GUI 侧的转交投递入口
    （线程安全，只入队）注册给 CLI——此后单实例转交（§9.4/U-13）在 CLI
    监听线程 open 出的完整 OpenResult 批次（含失败文件诊断态）经该入口
    进 GUI，主循环 drain timer 消费。CLI 侧未注册/注册前到达的转交由
    CLI 信箱缓冲，注册时补送（见 cli.py）。

    过渡期约定（与 CLI 接缝）：**启动阶段失败一律抛 NotImplementedError**
    ——CLI 只捕获 NotImplementedError/ImportError 作为"GUI 未就绪"信号
    （stderr + exit 2）。G1 完工后正常路径不再触发。
    """
    host = options.host or DEFAULT_HOST
    port = options.port if options.port is not None else _find_free_port(host)

    state = GuiState(manager=manager, poll_interval=options.poll_interval)
    if on_handoff_open is not None:
        on_handoff_open(state.enqueue_handoff)   # U-13 接缝：注册转交投递入口
    _wire_exit(state)

    # NiceGUI 3.x 关键裁量：**必须**用 root 函数模式（ui.run(root=fn)）。
    # 全局作用域直接建元素会进入"script mode"——NiceGUI 在每次页面请求时
    # runpy 重执行 sys.argv[0]（对库入口是灾难：重复 open_files + 请求
    # 上下文内 SystemExit → 500）。root 模式下页面内容由 fn 构建。
    def _root() -> None:
        build_gui(state)

    print(f"configer 浏览器模式: http://{host}:{port}", flush=True)

    try:
        ui.run(
            _root,
            host=host,
            port=port,
            reload=False,
            show=False,
            title="configer",
        )
    except NotImplementedError:
        raise
    except KeyboardInterrupt:
        # Ctrl+C（裁量登记）：SIGINT 会以 KeyboardInterrupt 穿透
        # uvicorn/asyncio（NiceGUI shutdown 钩子可能来不及执行），此处
        # 手动补一次退出安全网（非交互 flush，幂等），再按契约返回
        # 退出码——CLI finally 的 stop_all/release 得以正常执行。
        pass
    except Exception as exc:  # 启动阶段失败 → CLI 过渡信号（裁量登记）
        raise NotImplementedError(f"configer GUI 启动失败：{exc}") from exc
    # 安全网兜底：ui.run 返回/被打断后补一次非交互 flush（已 finalize 则
    # 幂等跳过——交互式退出流程或 app.on_shutdown 已处理时不重复）。
    state.on_app_shutdown()
    return state.exit_code


if __name__ in {"__main__", "__mp_main__"}:
    # 直接调试入口：configer python -m configer.ui.app <files...>
    mgr = SessionManager()
    mgr.open_files([Path(a) for a in sys.argv[1:]])
    sys.exit(run_gui(mgr, GuiOptions()))
