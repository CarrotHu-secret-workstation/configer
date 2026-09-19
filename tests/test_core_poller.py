"""外部修改检测测试（规范 §7.6：check_once 三态 + 轮询器边沿触发）。"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from configer.core.poller import ExternalChangePoller, FileState, check_once
from configer.model import compute_hash

IS_ROOT = os.geteuid() == 0 if hasattr(os, "geteuid") else False
WAIT_TIMEOUT = 2.0


# ---------------------------------------------------------------------------
# check_once：纯函数三态
# ---------------------------------------------------------------------------

class TestCheckOnce:
    def test_unchanged(self, tmp_path: Path):
        f = tmp_path / "config.yaml"
        f.write_bytes(b"a: 1\n")
        assert check_once(f, compute_hash(b"a: 1\n")) == FileState.UNCHANGED

    def test_modified(self, tmp_path: Path):
        f = tmp_path / "config.yaml"
        f.write_bytes(b"a: 2\n")
        assert check_once(f, compute_hash(b"a: 1\n")) == FileState.MODIFIED

    def test_gone_after_delete(self, tmp_path: Path):
        f = tmp_path / "config.yaml"
        f.write_bytes(b"a: 1\n")
        baseline = compute_hash(b"a: 1\n")
        f.unlink()
        assert check_once(f, baseline) == FileState.GONE

    def test_gone_after_move(self, tmp_path: Path):
        f = tmp_path / "config.yaml"
        f.write_bytes(b"a: 1\n")
        baseline = compute_hash(b"a: 1\n")
        f.rename(tmp_path / "moved.yaml")
        assert check_once(f, baseline) == FileState.GONE

    @pytest.mark.skipif(IS_ROOT, reason="root 绕过文件读权限，无法构造不可读")
    def test_gone_on_unreadable_permission(self, tmp_path: Path):
        # 权限变更（不可读）与删除同为 GONE（v0.4 决议：UI 同类横幅）
        f = tmp_path / "config.yaml"
        f.write_bytes(b"a: 1\n")
        baseline = compute_hash(b"a: 1\n")
        os.chmod(f, 0o000)
        try:
            assert check_once(f, baseline) == FileState.GONE
        finally:
            os.chmod(f, 0o644)

    def test_pure_no_side_effects(self, tmp_path: Path):
        f = tmp_path / "config.yaml"
        f.write_bytes(b"a: 1\n")
        before = f.stat()
        check_once(f, compute_hash(b"a: 1\n"))
        after = f.stat()
        assert (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size)


# ---------------------------------------------------------------------------
# ExternalChangePoller：后台线程集成（interval=0.05 加速）
# ---------------------------------------------------------------------------

class Recorder:
    """线程安全回调记录器：states 列表 + 每次回调 set 一个新 Event。"""

    def __init__(self):
        self.states: list[FileState] = []
        self._lock = threading.Lock()
        self._events: list[threading.Event] = []

    def __call__(self, state: FileState) -> None:
        with self._lock:
            self.states.append(state)
            ev = threading.Event()
            self._events.append(ev)

    def wait_for(self, n: int, timeout: float = WAIT_TIMEOUT) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.states) >= n:
                    return True
            time.sleep(0.01)
        return False

    def snapshot(self) -> list[FileState]:
        with self._lock:
            return list(self.states)


@pytest.fixture
def watched(tmp_path: Path):
    f = tmp_path / "config.yaml"
    f.write_bytes(b"v: 1\n")
    return f


def make_poller(f: Path, rec: Recorder, interval: float = 0.05) -> ExternalChangePoller:
    return ExternalChangePoller(f, compute_hash(f.read_bytes()), interval=interval, on_change=rec)


class TestPollerIntegration:
    def test_external_modification_fires_modified(self, watched: Path):
        rec = Recorder()
        p = make_poller(watched, rec)
        p.start()
        try:
            watched.write_bytes(b"v: 2\n")
            assert rec.wait_for(1), f"未收到 MODIFIED 回调：{rec.snapshot()}"
            assert rec.snapshot()[0] == FileState.MODIFIED
        finally:
            p.stop()

    def test_delete_fires_gone(self, watched: Path):
        rec = Recorder()
        p = make_poller(watched, rec)
        p.start()
        try:
            watched.unlink()
            assert rec.wait_for(1)
            assert rec.snapshot()[0] == FileState.GONE
        finally:
            p.stop()

    def test_edge_triggered_no_repeat_burst(self, watched: Path):
        # 持续 MODIFIED 只回调一次（边沿触发，不重复轰炸）
        rec = Recorder()
        p = make_poller(watched, rec)
        p.start()
        try:
            watched.write_bytes(b"v: 2\n")
            assert rec.wait_for(1)
            time.sleep(0.3)  # 约 6 个轮询周期
            assert rec.snapshot() == [FileState.MODIFIED]
        finally:
            p.stop()

    def test_modified_then_gone_fires_again(self, watched: Path):
        # MODIFIED → GONE 是状态迁移，也要回调
        rec = Recorder()
        p = make_poller(watched, rec)
        p.start()
        try:
            watched.write_bytes(b"v: 2\n")
            assert rec.wait_for(1)
            watched.unlink()
            assert rec.wait_for(2)
            assert rec.snapshot() == [FileState.MODIFIED, FileState.GONE]
        finally:
            p.stop()

    def test_update_baseline_silences_and_rearms(self, watched: Path):
        rec = Recorder()
        p = make_poller(watched, rec)
        p.start()
        try:
            watched.write_bytes(b"v: 2\n")
            assert rec.wait_for(1)
            # 模拟提交成功：session 更新基线为当前磁盘哈希
            p.update_baseline(compute_hash(watched.read_bytes()))
            time.sleep(0.3)
            assert rec.snapshot() == [FileState.MODIFIED]  # 不再回调
            # 边沿已随 UNCHANGED 复位：再次外部修改会再次回调
            watched.write_bytes(b"v: 3\n")
            assert rec.wait_for(2)
            assert rec.snapshot() == [FileState.MODIFIED, FileState.MODIFIED]
        finally:
            p.stop()

    def test_restore_to_unchanged_rearms_edge(self, watched: Path):
        # 文件恢复原内容（UNCHANGED）后再次修改 → 再回调（边沿重置；
        # UNCHANGED 本身不回调）
        original = watched.read_bytes()
        rec = Recorder()
        p = make_poller(watched, rec)
        p.start()
        try:
            watched.write_bytes(b"v: 999\n")
            assert rec.wait_for(1)
            watched.write_bytes(original)  # 恢复
            time.sleep(0.2)
            assert rec.snapshot() == [FileState.MODIFIED]  # 复位不回调
            watched.write_bytes(b"v: 1000\n")
            assert rec.wait_for(2)
        finally:
            p.stop()

    def test_stop_idempotent_and_thread_exits(self, watched: Path):
        rec = Recorder()
        p = make_poller(watched, rec)
        p.start()
        p.stop()
        p.stop()  # 幂等
        assert not p.running
        # 未 start 直接 stop 也安全
        p2 = make_poller(watched, Recorder())
        p2.stop()
        p2.stop()

    def test_start_idempotent(self, watched: Path):
        rec = Recorder()
        p = make_poller(watched, rec)
        p.start()
        p.start()  # 幂等：不产生第二个线程
        try:
            watched.write_bytes(b"v: 2\n")
            assert rec.wait_for(1)
            time.sleep(0.2)
            assert rec.snapshot() == [FileState.MODIFIED]  # 单线程单次回调
        finally:
            p.stop()

    def test_stop_is_responsive_despite_interval(self, watched: Path):
        # stop 不必等满一个大 interval（stop_event.wait 分片）
        rec = Recorder()
        p = ExternalChangePoller(watched, compute_hash(watched.read_bytes()),
                                 interval=5.0, on_change=rec)
        p.start()
        t0 = time.monotonic()
        p.stop()
        assert time.monotonic() - t0 < 2.0
        assert not p.running

    def test_callback_exception_does_not_kill_poller(self, watched: Path):
        calls = []

        def bad_cb(state):
            calls.append(state)
            raise RuntimeError("UI 层 bug 不得杀死轮询线程")

        p = ExternalChangePoller(
            watched, compute_hash(watched.read_bytes()), interval=0.05, on_change=bad_cb
        )
        p.start()
        try:
            watched.write_bytes(b"v: 2\n")
            deadline = time.monotonic() + WAIT_TIMEOUT
            while not calls and time.monotonic() < deadline:
                time.sleep(0.01)
            assert calls == [FileState.MODIFIED]
            watched.unlink()
            deadline = time.monotonic() + WAIT_TIMEOUT
            while len(calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert p.running  # 线程仍存活
            assert calls == [FileState.MODIFIED, FileState.GONE]
        finally:
            p.stop()

    def test_no_callback_when_unchanged(self, watched: Path):
        rec = Recorder()
        p = make_poller(watched, rec)
        p.start()
        try:
            time.sleep(0.25)
            assert rec.snapshot() == []
        finally:
            p.stop()
