"""单实例与转交（规范 §9.4、§13 K-11）。

机制：Unix domain socket。首个实例 bind 并以后台 daemon 线程监听；后续实例
连接既有 socket，发送规范化绝对路径 JSON 清单，等 ack 后以 exit 0 退出
（转交成功，未发生写盘，§9.2）。

陈旧锁处置（K-11：任何情况下不得阻塞用户打开文件，所有等待都有 timeout）：

- 连接失败 / 握手超时 / 拒绝一律按**陈旧锁**处置：清理 socket 文件后自行
  bind 监听（§9.4）；
- bind 竞态失败（EADDRINUSE）→ 重试连接一次再判（裁量登记：并发启动窗口
  内对方可能刚 bind 成功）；重试仍失败 → 降级为无服务器的 ACQUIRED（宁可
  短暂双实例，也不阻塞用户打开文件，K-11 优先）。

裁量登记：
1. socket 路径 ``$XDG_RUNTIME_DIR/configer-{uid}.sock``，无 XDG_RUNTIME_DIR
   则 ``/tmp/configer-{uid}.sock``；可用 ``sock_path`` 参数覆盖（测试用）。
2. 转交仅限本机同一用户：socket 文件权限 0600。
3. 服务器收到清单 → 回调 ``on_handoff_received(paths)``（真实应用接
   SessionManager.open_files + UI 激活；本模块只管机制）→ 回 ack。回调
   抛异常**不影响 ack**（防御）。
4. 协议：客户端发送单条 JSON ``{"v": 1, "paths": [...]}`` 后半关写端；
   服务器读至 EOF，回 ``{"ok": true}``。载荷解析失败回 ``{"ok": false}``，
   客户端视同握手失败按陈旧锁兜底。
"""

from __future__ import annotations

import errno
import json
import os
import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

__all__ = [
    "ACQUIRED",
    "HANDED_OFF",
    "HandoffResult",
    "acquire_or_handoff",
    "default_socket_path",
    "release",
]

# 结果状态（§9.4）：ACQUIRED=本进程是首实例，继续启动；HANDED_OFF=已转交
# 给既有实例，调用方应 exit 0。
ACQUIRED = "acquired"
HANDED_OFF = "handed_off"

# 单条转交清单的最大字节数（防御畸形/恶意载荷；正常路径清单远小于此）。
_MAX_PAYLOAD = 256 * 1024


def default_socket_path() -> Path:
    """本机同一用户的单实例 socket 路径（裁量 1）。"""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path("/tmp")
    return base / f"configer-{os.getuid()}.sock"


@dataclass
class HandoffResult:
    """:func:`acquire_or_handoff` 的结果对象。

    - ``status == ACQUIRED``：本进程为首实例（服务器已启动，或降级无服务器），
      调用方继续正常启动流程；
    - ``status == HANDED_OFF``：清单已转交并收到 ack，调用方应 exit 0
      （未发生任何写盘，§9.2 / §9.4）。
    """

    status: str
    socket_path: Path
    handed_off_paths: list[Path] = field(default_factory=list)
    server: "_Server | None" = None

    def release(self) -> None:
        """释放本结果持有的服务器（幂等；HANDED_OFF 时为 no-op）。"""
        if self.server is not None:
            self.server.stop()
            self.server = None


# 模块级"当前活动实例"（CLI finally 用 single_instance.release() 清理）。
_active: HandoffResult | None = None
_active_lock = threading.Lock()


def release() -> None:
    """停止模块级活动实例的监听线程并删除 socket 文件（幂等）。

    进程崩溃残留由下一个启动者按陈旧锁清理（§9.4 / K-11）。
    """
    global _active
    with _active_lock:
        current, _active = _active, None
    if current is not None:
        current.release()


def acquire_or_handoff(
    paths: list[Path | str],
    on_handoff_received: Callable[[list[Path]], None] | None = None,
    timeout: float = 2.0,
    sock_path: Path | str | None = None,
) -> HandoffResult:
    """尝试转交给既有实例；无活动实例（或陈旧锁）则自行登记为首实例。

    ``paths`` 规范化为绝对路径（resolve）后转交。``timeout`` 为每一阶段
    （connect / send / 等 ack）的秒数上限——K-11：不得阻塞用户打开文件。

    ACQUIRED 成功时把结果登记为模块级活动实例（:func:`release` 清理）；
    同进程重复 acquire（测试场景）各自持有独立 server，请用
    :meth:`HandoffResult.release` 释放非活动实例。
    """
    global _active
    resolved = [Path(p).resolve() for p in paths]
    sock = Path(sock_path) if sock_path is not None else default_socket_path()

    # ① socket 文件存在 → 先尝试转交（连接/握手失败一律按陈旧锁处置）
    if sock.exists():
        outcome = _try_handoff(resolved, sock, timeout)
        if outcome is not None:
            return outcome
        _cleanup_stale(sock)

    # ② 自行 bind 监听（含竞态重试，裁量 2）
    server = _start_server(sock, on_handoff_received, timeout)
    if server is None:
        # bind 竞态失败（EADDRINUSE）→ 重试连接一次再判
        outcome = _try_handoff(resolved, sock, timeout)
        if outcome is not None:
            return outcome
        _cleanup_stale(sock)
        server = _start_server(sock, on_handoff_received, timeout)
        if server is None:
            # 重试仍失败 → 降级：无服务器 ACQUIRED（K-11 优先，不阻塞打开）
            return HandoffResult(status=ACQUIRED, socket_path=sock,
                                 server=None)

    result = HandoffResult(status=ACQUIRED, socket_path=sock, server=server)
    with _active_lock:
        _active = result
    return result


# ---------------------------------------------------------------------------
# 客户端：转交握手
# ---------------------------------------------------------------------------


def _try_handoff(
    paths: list[Path], sock: Path, timeout: float
) -> HandoffResult | None:
    """向既有实例转交；成功返回 HANDED_OFF 结果，任何失败返回 None（陈旧锁）。

    失败形态（一律 None，§9.4）：连接拒绝（监听者已死）、连接/发送/等 ack
    超时、载荷被拒（ok=false）、非 JSON 响应。
    """
    payload = json.dumps(
        {"v": 1, "paths": [str(p) for p in paths]}
    ).encode("utf-8")
    client: socket.socket | None = None
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        client.connect(str(sock))
        client.sendall(payload)
        client.shutdown(socket.SHUT_WR)  # 半关写端 = 载荷结束信号（裁量 4）
        chunks: list[bytes] = []
        while True:
            block = client.recv(4096)
            if not block:
                break
            chunks.append(block)
            if sum(len(c) for c in chunks) > _MAX_PAYLOAD:
                return None
        response = json.loads(b"".join(chunks).decode("utf-8"))
        if not isinstance(response, dict) or response.get("ok") is not True:
            return None
        return HandoffResult(status=HANDED_OFF, socket_path=sock,
                             handed_off_paths=list(paths))
    except (OSError, ValueError, UnicodeDecodeError):
        # ConnectionRefusedError / timeout / 畸形响应 → 陈旧锁兜底（K-11）
        return None
    finally:
        if client is not None:
            try:
                client.close()
            except OSError:
                pass


def _cleanup_stale(sock: Path) -> None:
    """清理陈旧 socket 文件（§9.4）；文件消失竞态容忍。"""
    try:
        sock.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 服务器：首实例监听
# ---------------------------------------------------------------------------


class _Server:
    """Unix socket 监听服务器（daemon 线程；stop() 幂等）。"""

    def __init__(
        self,
        sock: Path,
        on_handoff_received: Callable[[list[Path]], None] | None,
        timeout: float,
    ) -> None:
        self._path = sock
        self._callback = on_handoff_received
        self._timeout = max(float(timeout), 0.1)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(sock))
        # 转交仅限本机同一用户（裁量 2）：0600
        try:
            os.chmod(sock, 0o600)
        except OSError:
            pass
        self._sock.listen(8)
        self._sock.settimeout(self._timeout)  # accept 轮询粒度，便于停止
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, name="configer-single-instance", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # socket 已关闭（stop）
            try:
                self._handle(conn)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(self._timeout)
        chunks: list[bytes] = []
        try:
            while True:
                block = conn.recv(4096)
                if not block:
                    break
                chunks.append(block)
                if sum(len(c) for c in chunks) > _MAX_PAYLOAD:
                    conn.sendall(b'{"ok": false}')
                    return
            paths = self._parse(b"".join(chunks))
        except (OSError, socket.timeout):
            return  # 客户端中途消失：无 ack 可送，忽略
        if paths is None:
            try:
                conn.sendall(b'{"ok": false}')
            except OSError:
                pass
            return
        if self._callback is not None:
            try:
                self._callback(paths)
            except Exception:
                pass  # 裁量 3：回调异常不影响 ack（防御）
        try:
            conn.sendall(b'{"ok": true}')
        except OSError:
            pass

    @staticmethod
    def _parse(payload: bytes) -> list[Path] | None:
        try:
            data = json.loads(payload.decode("utf-8"))
            raw = data["paths"]
            if not isinstance(raw, list):
                return None
            return [Path(str(p)) for p in raw]
        except (ValueError, UnicodeDecodeError, KeyError, TypeError):
            return None

    def stop(self) -> None:
        """停监听线程 + 删 socket 文件（幂等）。"""
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=self._timeout + 1.0)
        _cleanup_stale(self._path)


def _start_server(
    sock: Path,
    on_handoff_received: Callable[[list[Path]], None] | None,
    timeout: float,
) -> _Server | None:
    """bind+监听；EADDRINUSE（竞态）返回 None 交由调用方重试连接再判。"""
    try:
        sock.parent.mkdir(parents=True, exist_ok=True)
        return _Server(sock, on_handoff_received, timeout)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return None
        raise
