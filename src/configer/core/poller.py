"""外部修改检测：纯轮询（规范 §7.6，v0.4 决议）——检测机制部分。

承载语义：
- 打开期间以固定周期重读文件内容并比对 sha256 与会话基线
  （``ConfigDoc.original_bytes`` / ``content_hash``，§3.6）；默认周期 2s，
  可经 CLI ``--poll-interval <秒>`` 配置（§9.1，构造参数 ``interval``）。
- 发现外部变化：由回调通知上层，UI 显示「文件已在磁盘上被修改」横幅；
  **不自动覆盖、不自动重载**（抉择在冲突模态，§7.6，属 session/UI 层）。
- 文件被删除/移动/权限变更（不可读）→ :attr:`FileState.GONE`（横幅文案
  区别于「被修改」，该文件全部条目转只读态——处置在 session/UI 层）。
- 操作系统级监听（watchdog 类）列 v1.5 增强，v1 不依赖；轮询不依赖事件
  通知，在网络文件系统（NFS/SSHFS）上检测可靠性不受影响，开销尽力而为。
- 与提交前基线校验（session，§7.6）互为兜底：轮询管检测及时性，提交前
  比对管竞态。提交前比对可直接复用 :func:`check_once`（纯函数）。

公共 API（session 层对接）：
- :class:`FileState`：UNCHANGED / MODIFIED / GONE 三态；
- :func:`check_once`：单次读盘比对，纯函数、无副作用；
- :class:`ExternalChangePoller`：后台轮询线程，边沿触发回调。
"""

from __future__ import annotations

import threading
from enum import Enum
from pathlib import Path
from typing import Callable

from ..model import compute_hash

__all__ = ["FileState", "check_once", "ExternalChangePoller"]


class FileState(str, Enum):
    """文件相对会话基线的状态（§7.6）。

    - ``UNCHANGED``：磁盘字节哈希 == 基线哈希；
    - ``MODIFIED``：哈希 != 基线（外部修改）；
    - ``GONE``：删除 / 移动 / 权限变更等一切不可读情形（v0.4 决议：
      三种情况 UI 文案同一类「不可读」横幅，处置一致——条目转只读态，
      故合并为一态；上层无需也无法可靠区分）。

    继承 str 以便 JSON 序列化与 ``== "modified"`` 式比较。
    """

    UNCHANGED = "unchanged"
    MODIFIED = "modified"
    GONE = "gone"


def check_once(path: Path | str, baseline_hash: str) -> FileState:
    """单次检测：读盘算 sha256 与 ``baseline_hash`` 比对。纯函数、无副作用。

    任何读失败（FileNotFoundError / PermissionError / 其他 OSError，如
    目录不可执行、EISDIR）→ ``GONE``。提交前基线校验（§7.6 竞态兜底）
    可直接复用本函数。
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return FileState.GONE
    if compute_hash(data) == baseline_hash:
        return FileState.UNCHANGED
    return FileState.MODIFIED


class ExternalChangePoller:
    """后台轮询器（daemon 线程）：周期性 :func:`check_once`，**边沿触发**回调。

    - 构造：``ExternalChangePoller(path, baseline_hash, interval=2.0,
      on_change=None)``。``interval`` 秒，对应 CLI ``--poll-interval``
      （§9.1）；``on_change: Callable[[FileState], None]`` 可后置设置或
      经 :meth:`start` 传入语义等价（本类只经构造注入，None 则静默）。
    - **边沿触发语义**（裁量，防重复轰炸）：状态从 UNCHANGED 变为
      MODIFIED / GONE 时回调**一次**；持续 MODIFIED 不重复回调；
      MODIFIED → GONE 的**变化**同样回调（状态迁移即边沿）；恢复
      UNCHANGED 复位边沿（不回调 UNCHANGED 本身），此后再次 MODIFIED
      会再次回调。
    - :meth:`update_baseline`：提交成功后 session 以新落盘字节哈希调用；
      更新后若磁盘与新基线一致，下轮检测归 UNCHANGED，边沿随之复位。
    - :meth:`start` / :meth:`stop` 幂等；stop 等待线程退出（有超时；线程
      为 daemon，即便 join 超时也不阻塞进程退出）。
    - 线程安全：baseline 与边沿状态由锁保护。

    .. warning::
       回调在**轮询线程**执行。UI 层（NiceGUI 等）必须自行把后续处置
       调度回主线程/事件循环，不得在回调里直接操作 UI 对象。回调抛出
       的异常会被本类吞掉（轮询线程保活优先），故回调内部应自行记录。
    """

    def __init__(
        self,
        path: Path | str,
        baseline_hash: str,
        interval: float = 2.0,
        on_change: Callable[[FileState], None] | None = None,
    ) -> None:
        self._path = Path(path)
        self._interval = float(interval)
        self._on_change = on_change
        self._lock = threading.Lock()
        self._baseline = baseline_hash
        self._last_state = FileState.UNCHANGED  # 边沿状态
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -- 属性 ---------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def interval(self) -> float:
        return self._interval

    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    # -- 基线管理 -----------------------------------------------------------

    def update_baseline(self, new_hash: str) -> None:
        """提交成功后更新会话基线（§7.5 第 4 步、§7.6）。

        裁量：不同时复位边沿状态——若磁盘已与新基线一致，下一轮检测自然
        得到 UNCHANGED 并复位；若磁盘仍与新基线不一致（提交后立刻又被
        外部改写），保持 MODIFIED 边沿不重复回调更符合"每个外部变化事件
        通知一次"的意图。
        """
        with self._lock:
            self._baseline = new_hash

    def reset_edge(self, state: FileState = FileState.UNCHANGED) -> None:
        """复位边沿状态（默认回 UNCHANGED，不触发回调）。

        K-9 竞态配套（验收后修复）：上层（session）对某次 MODIFIED 回调
        复核判定为"自身提交回声"并忽略后，调用本方法把边沿复位——否则
        该次被忽略的回声会占住 MODIFIED 边沿，紧随其后的真实外部修改
        将不再满足边沿条件而被吞掉。

        并发注：仅应在轮询线程的回调链内调用（``_run`` 为单线程循环，
        回调期间不可能并发写边沿）；其他调用方由锁保护，语义上仅在与
        轮询周期无并发时可靠。
        """
        with self._lock:
            self._last_state = state

    def get_baseline(self) -> str:
        with self._lock:
            return self._baseline

    # -- 生命周期 -----------------------------------------------------------

    def start(self) -> None:
        """启动轮询线程（幂等：已在运行则无操作）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"configer-poller({self._path.name})",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """停止轮询（幂等：未启动/已停止均安全）。

        ``timeout`` 为 join 等待秒数，默认 ``interval + 1``；线程是
        daemon，join 超时不阻塞进程退出。
        """
        self._stop_event.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout if timeout is not None else self._interval + 1.0)

    # -- 内部 ---------------------------------------------------------------

    def _run(self) -> None:
        # 启动后立即先查一次（覆盖 start 前已发生的外部变化），随后按周期。
        first = True
        while not self._stop_event.is_set():
            if not first and self._stop_event.wait(self._interval):
                break
            first = False
            with self._lock:
                baseline = self._baseline
                last = self._last_state
            state = check_once(self._path, baseline)
            with self._lock:
                # 边沿触发：非 UNCHANGED 且与上次状态不同才回调；
                # 回到 UNCHANGED 只复位边沿、不回调。
                changed = state != FileState.UNCHANGED and state != last
                self._last_state = state
            if changed and self._on_change is not None:
                try:
                    self._on_change(state)
                except Exception:
                    pass  # 回调异常不得杀死轮询线程（见类 docstring 警示）
