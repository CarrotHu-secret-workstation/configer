"""configer.single_instance 单元测试（规范 §9.4 单实例与转交、§13 K-11）。

同进程模拟两个实例：直接对同一 socket 路径两次调用 acquire_or_handoff
（服务器线程独立于调用线程，机制与跨进程等价）。
"""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path

import pytest

from configer import single_instance as si


@pytest.fixture()
def sock(tmp_path: Path) -> Path:
    """每个测试独立的 socket 路径（不碰真实 /tmp/configer-{uid}.sock）。"""
    return tmp_path / "configer-test.sock"


@pytest.fixture(autouse=True)
def _release_active():
    """兜底：清掉模块级活动实例（幂等），避免跨测试泄漏监听线程。"""
    yield
    si.release()


class Tracker:
    """收集 HandoffResult，测试结束逐个 release（含 _active 之外的实例）。"""

    def __init__(self) -> None:
        self.results: list[si.HandoffResult] = []

    def track(self, result: si.HandoffResult) -> si.HandoffResult:
        self.results.append(result)
        return result


@pytest.fixture()
def tracker():
    t = Tracker()
    yield t
    for result in reversed(t.results):
        result.release()


# ---------------------------------------------------------------------------
# 首实例：ACQUIRED + 登记
# ---------------------------------------------------------------------------

def test_acquire_returns_acquired_and_creates_socket(sock: Path, tracker: Tracker) -> None:
    r = tracker.track(si.acquire_or_handoff([], timeout=1.0, sock_path=sock))
    assert r.status == si.ACQUIRED
    assert sock.exists() and sock.is_socket()


def test_socket_permission_is_0600(sock: Path, tracker: Tracker) -> None:
    # 转交仅限本机同一用户（裁量：0600）
    tracker.track(si.acquire_or_handoff([], timeout=1.0, sock_path=sock))
    mode = stat.S_IMODE(os.stat(sock).st_mode)
    assert mode == 0o600


def test_acquire_paths_do_not_need_to_exist(sock: Path, tracker: Tracker) -> None:
    # 首实例路径不经过转交协议，文件存在性由后续 open_files 负责
    r = tracker.track(
        si.acquire_or_handoff([Path("/no/such/file.yaml")], timeout=1.0,
                              sock_path=sock)
    )
    assert r.status == si.ACQUIRED


# ---------------------------------------------------------------------------
# 第二实例：HANDED_OFF + 回调收到规范化路径 + ack
# ---------------------------------------------------------------------------

def test_second_acquire_hands_off_with_ack_and_callback(
    sock: Path, tracker: Tracker, tmp_path: Path
) -> None:
    received: list[list[Path]] = []
    tracker.track(
        si.acquire_or_handoff([], on_handoff_received=received.append,
                              timeout=2.0, sock_path=sock)
    )

    sub = tmp_path / "sub"
    sub.mkdir()
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.py"
    a.touch()
    b.touch()
    # 含一段未规范化绝对路径（sub/..），转交前必须 resolve（§9.4）
    messy = [a, sub / ".." / "b.py"]

    r2 = tracker.track(si.acquire_or_handoff(messy, timeout=2.0, sock_path=sock))
    assert r2.status == si.HANDED_OFF
    # ack 之前回调已执行（服务器先回调后 ack），无需等待
    assert received == [[a.resolve(), b.resolve()]]
    assert r2.handed_off_paths == [a.resolve(), b.resolve()]
    # 首实例的服务器仍在（第二实例未持有 server）
    assert r2.server is None
    assert sock.exists()


def test_handoff_only_when_socket_present_dead_server_is_stale(
    sock: Path, tracker: Tracker
) -> None:
    # socket 文件不存在 → 直接 bind，不走转交
    r = tracker.track(si.acquire_or_handoff([], timeout=1.0, sock_path=sock))
    assert r.status == si.ACQUIRED


def test_callback_exception_does_not_prevent_ack(
    sock: Path, tracker: Tracker
) -> None:
    # 裁量（防御）：回调抛异常仍回 ack，第二实例照常 HANDED_OFF exit 0
    def boom(paths: list[Path]) -> None:
        raise RuntimeError("boom")

    tracker.track(si.acquire_or_handoff([], on_handoff_received=boom,
                                        timeout=2.0, sock_path=sock))
    r2 = tracker.track(si.acquire_or_handoff([Path("/x/a.yaml")],
                                             timeout=2.0, sock_path=sock))
    assert r2.status == si.HANDED_OFF


def test_malformed_payload_rejected_then_treated_as_stale(
    sock: Path, tracker: Tracker
) -> None:
    # 服务器对畸形载荷回 ok=false；客户端视同握手失败 → 陈旧锁兜底。
    # 先手工放一个会拒绝载荷的"假服务器"，验证客户端不 hang 且能接管。
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock))
    srv.listen(1)

    def _reject() -> None:
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(b'{"ok": false}')
        conn.close()

    import threading
    t = threading.Thread(target=_reject, daemon=True)
    t.start()

    r = tracker.track(si.acquire_or_handoff([Path("/x/a.yaml")],
                                            timeout=1.0, sock_path=sock))
    t.join(timeout=2.0)
    srv.close()
    # ok=false → 按陈旧锁清理并自行启动（K-11：不阻塞打开）
    assert r.status == si.ACQUIRED
    assert sock.exists()


# ---------------------------------------------------------------------------
# 陈旧锁（K-11：握手失败一律清理后自行启动，不得阻塞）
# ---------------------------------------------------------------------------

def test_stale_dead_socket_file_cleaned_and_acquired(
    sock: Path, tracker: Tracker
) -> None:
    # 上次进程崩溃残留：bind 过、无人监听
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(sock))
    dead.close()  # fd 关闭但文件残留 → connect 拒绝
    assert sock.exists()

    r = tracker.track(si.acquire_or_handoff([Path("/x/a.yaml")],
                                            timeout=0.5, sock_path=sock))
    assert r.status == si.ACQUIRED
    assert sock.is_socket()
    # 接管后服务器真实可用：再来一次转交应成功
    r2 = tracker.track(si.acquire_or_handoff([], timeout=1.0, sock_path=sock))
    assert r2.status == si.HANDED_OFF


def test_stale_regular_file_cleaned_and_acquired(
    sock: Path, tracker: Tracker
) -> None:
    # 极端残留：路径上是普通文件（非 socket）
    sock.write_text("stale")
    r = tracker.track(si.acquire_or_handoff([], timeout=0.5, sock_path=sock))
    assert r.status == si.ACQUIRED
    assert sock.is_socket()


def test_handoff_timeout_treated_as_stale(sock: Path, tracker: Tracker) -> None:
    # 监听但永不应答（ deaf socket）→ 等 ack 超时 → 陈旧锁处置
    deaf = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deaf.bind(str(sock))
    deaf.listen(1)
    try:
        r = tracker.track(si.acquire_or_handoff([], timeout=0.3, sock_path=sock))
        assert r.status == si.ACQUIRED
        assert sock.is_socket()
    finally:
        deaf.close()


# ---------------------------------------------------------------------------
# release：停监听 + 删 socket；幂等
# ---------------------------------------------------------------------------

def test_release_stops_server_and_removes_socket(sock: Path) -> None:
    r = si.acquire_or_handoff([], timeout=1.0, sock_path=sock)
    assert r.status == si.ACQUIRED and sock.exists()
    r.release()
    assert not sock.exists()
    r.release()  # 幂等
    assert not sock.exists()
    # release 后可重新 bind（无 TIME_WAIT 类问题）
    r2 = si.acquire_or_handoff([], timeout=1.0, sock_path=sock)
    assert r2.status == si.ACQUIRED
    r2.release()


def test_module_level_release_cleans_active_instance(sock: Path) -> None:
    r = si.acquire_or_handoff([], timeout=1.0, sock_path=sock)
    assert r.status == si.ACQUIRED
    si.release()  # CLI finally 用的模块级入口
    assert not sock.exists()
    si.release()  # 幂等，无活动实例时 no-op
    r.release()   # 句柄级 release 同样幂等


def test_default_socket_path_uses_uid_and_runtime_dir(monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert si.default_socket_path() == Path(
        f"/run/user/1000/configer-{os.getuid()}.sock"
    )
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert si.default_socket_path() == Path(f"/tmp/configer-{os.getuid()}.sock")
