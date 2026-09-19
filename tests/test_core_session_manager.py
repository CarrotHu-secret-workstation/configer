"""FileSession 集成测试（YamlAdapter + testdata 拷贝）与 SessionManager。

金样本 testdata/config.yaml 只读：一律先拷贝到 tmp_path 再操作。
不触碰 python 适配器（另一工程师并行开发中）：内联注册表只强断言 yaml。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from configer.adapters.yaml_adapter import YamlAdapter
from configer.bytesio import UTF8_BOM
from configer.core.poller import FileState
from configer.core.session import (
    CODE_E_READ,
    FileSession,
    SessionManager,
)
from configer.model import SEV_ERROR, compute_hash

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "testdata" / "config.yaml"

STEP = "ball_predictor.step_interval"


@pytest.fixture()
def cfg(tmp_path: Path) -> Path:
    """testdata/config.yaml 的可写拷贝（金样本只读纪律）。"""
    p = tmp_path / "config.yaml"
    p.write_bytes(GOLDEN.read_bytes())
    return p


def open_yaml(p: Path, events: list[str] | None = None,
              poll_interval: float = 2.0) -> FileSession:
    adapter = YamlAdapter()
    doc, diags = adapter.load(p)
    assert not any(d.severity == SEV_ERROR for d in diags), diags
    return FileSession(p, adapter, doc, diags,
                       poll_interval=poll_interval,
                       on_state_change=(events.append if events is not None
                                        else None))


# ---------------------------------------------------------------------------
# 集成：真实 yaml 适配器上的提交/风格重放/BOM
# ---------------------------------------------------------------------------


def test_yaml_commit_style_replay_and_reload(cfg):
    s = open_yaml(cfg)
    it = next(i for i in s.doc.items if i.path == STEP)
    assert it.raw_literal == "100."  # 金样本原始风格：trailing_dot
    assert s.set_pending_text(STEP, "1920").accepted
    c = s.commit_pending(STEP)
    assert c.status == "committed" and (c.old_value, c.new_value) == (100.0, 1920.0)
    # 盘上字节：风格重放 `100.` → `1920.`（§7.2），行内其余字节不变
    line = next(l for l in cfg.read_text().splitlines() if "step_interval" in l)
    assert line.strip() == "step_interval: 1920."
    # 重载裁决：doc 替换、新 raw_literal、基线=盘上字节
    assert s.doc.original_bytes == cfg.read_bytes()
    assert s.doc.content_hash == compute_hash(cfg.read_bytes())
    new_it = next(i for i in s.doc.items if i.path == STEP)
    assert new_it.raw_literal == "1920." and new_it.value == 1920.0
    # 同一条目连续两次提交（旧 locator 已随重载失效，第二次不得抛错）
    assert s.set_pending_text(STEP, "7").accepted
    assert s.commit_pending(STEP).status == "committed"
    line = next(l for l in cfg.read_text().splitlines() if "step_interval" in l)
    assert line.strip() == "step_interval: 7."
    # undo 链跨重载稳定：两次提交 → 两次 undo 回到金样本值
    assert s.undo().status == "committed"
    assert s.undo().status == "committed"
    line = next(l for l in cfg.read_text().splitlines() if "step_interval" in l)
    assert line.strip() == "step_interval: 100."


def test_yaml_commit_preserves_bom(tmp_path):
    p = tmp_path / "bom.yaml"
    p.write_bytes(UTF8_BOM + GOLDEN.read_bytes())
    s = open_yaml(p)
    s.set_pending_text(STEP, "55")
    assert s.commit_pending(STEP).status == "committed"
    raw = p.read_bytes()
    assert raw.startswith(UTF8_BOM)  # BOM 补回（§4.1）
    assert raw[len(UTF8_BOM):] != GOLDEN.read_bytes()
    # 重载后基线含 BOM、再次提交仍保持
    assert s.doc.original_bytes == raw
    s.set_pending_text(STEP, "56")
    assert s.commit_pending(STEP).status == "committed"
    assert p.read_bytes().startswith(UTF8_BOM)


def test_yaml_conflict_force_writes_our_bytes(cfg):
    s = open_yaml(cfg)
    s.set_pending_text(STEP, "1920")
    cfg.write_text(cfg.read_text().replace("team_id: 66", "team_id: 77"),
                   encoding="utf-8")  # 外部修改
    assert s.commit_pending(STEP).status == "conflict"
    assert "team_id: 77" in cfg.read_text()  # 未写盘
    r = s.resolve_conflict("force")
    assert r.status == "committed"
    text = cfg.read_text()
    assert "step_interval: 1920." in text
    assert "team_id: 66" in text  # 外部修改被覆盖丢失（§7.6 force 语义）


def test_real_poller_events(cfg):
    events: list[str] = []
    got = threading.Event()
    s = open_yaml(cfg, events=events, poll_interval=0.05)

    def cb(ev: str) -> None:
        events.append(ev)
        got.set()

    s.set_on_state_change(cb)   # G2c：公开重绑 API
    s.start_poller()
    try:
        time.sleep(0.12)  # 越过 start 即查的一次
        cfg.write_text(cfg.read_text() + "# 外部追加\n", encoding="utf-8")
        assert got.wait(3.0), events
        assert "external_modified" in events
        got.clear()
        cfg.unlink()
        assert got.wait(3.0), events
        assert "gone" in events
        assert s.gone_readonly
        # MODIFIED 回调已清 gone_readonly 的路径：恢复为修改版文件后
        # poller 不回调 UNCHANGED/GONE→UNCHANGED，主动探测解除
        cfg.write_bytes(GOLDEN.read_bytes())
        r = s.set_pending_text(STEP, "3")
        assert r.accepted and not s.gone_readonly
    finally:
        s.stop_poller()


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


def test_inline_registry_has_yaml():
    reg = SessionManager._inline_registry()
    names = [a.name for a in reg.adapters]
    assert "yaml" in names
    if "python" in names:  # 尽力而为：python 适配器并行开发中，不硬断言
        assert names.index("python") < names.index("yaml")  # v1 固定顺序


def test_open_two_files_and_duplicate(cfg, tmp_path):
    other = tmp_path / "other.yaml"
    other.write_text("a: 1\n", encoding="utf-8")
    mgr = SessionManager()
    results = mgr.open_files([cfg, other])
    assert all(r.session is not None for r in results), results
    assert len(mgr.sessions) == 2
    # 裁量 9：重复打开同一文件 → 返回已有 session
    [again] = mgr.open_files([cfg])
    assert again.session is results[0].session
    assert len(mgr.sessions) == 2
    assert mgr.get(cfg) is results[0].session
    mgr.stop_all()


def test_open_partial_failure(cfg, tmp_path):
    missing = tmp_path / "nope.yaml"
    bad_enc = tmp_path / "bad.yaml"
    bad_enc.write_bytes(b"\xff\xfe\x00garbage")
    mgr = SessionManager()
    results = mgr.open_files([missing, cfg, bad_enc])
    # 裁量 7：不存在 → E-READ error 诊断
    r_missing = results[0]
    assert r_missing.session is None
    assert any(d.code == CODE_E_READ and d.severity == SEV_ERROR
               for d in r_missing.diagnostics)
    # 部分失败不阻断其他文件（§3.10）
    assert results[1].session is not None
    # 非 UTF-8 → load 期 E-ENCODING error → 失败态
    r_bad = results[2]
    assert r_bad.session is None
    assert any(d.code == "E-ENCODING" and d.severity == SEV_ERROR
               for d in r_bad.diagnostics)
    assert mgr.get(missing) is None and mgr.get(bad_enc) is None
    mgr.stop_all()


def test_open_unknown_format(tmp_path):
    # 嗅探全部失败 → E-FORMAT，session=None（用空注册表使失败确定，
    # 不依赖并行开发中的 python 适配器 detect 行为；嗅探逻辑本身由
    # test_registry.py 覆盖，此处验证 manager 对失败态的接线）
    from configer.registry import Registry
    p = tmp_path / "x.unknownext"
    p.write_text("hello", encoding="utf-8")
    mgr = SessionManager()
    [r] = mgr.open_files([p], registry=Registry())
    assert r.session is None
    assert any(d.code == "E-FORMAT" and d.severity == SEV_ERROR
               for d in r.diagnostics)
    assert mgr.get(p) is None


def test_close_file_index(cfg):
    mgr = SessionManager()
    [r] = mgr.open_files([cfg])
    s = r.session
    assert mgr.close_file(cfg).status == "closed"
    assert mgr.get(cfg) is None and s.closed
    # 未在会话中的路径：幂等 closed
    assert mgr.close_file(cfg).status == "closed"


def test_close_file_conflict_keeps_session(cfg):
    mgr = SessionManager()
    [r] = mgr.open_files([cfg])
    s = r.session
    s.set_pending_text(STEP, "1920")
    cfg.write_text(cfg.read_text().replace("team_id: 66", "team_id: 77"),
                   encoding="utf-8")
    result = mgr.close_file(cfg)
    assert result.status == "conflict"
    assert mgr.get(cfg) is s  # 未决不关闭、不移除索引
    s.resolve_conflict("reload")
    assert mgr.close_file(cfg).status == "closed"
    assert mgr.get(cfg) is None


def test_flush_all_mixed(cfg, tmp_path):
    other = tmp_path / "other.yaml"
    other.write_text("a: 1\n", encoding="utf-8")
    mgr = SessionManager()
    r1, r2 = mgr.open_files([cfg, other])
    r1.session.set_pending_text(STEP, "1920")   # 将正常提交
    r2.session.set_pending_text("a", "2")       # 将撞冲突
    other.write_text("a: 999\n", encoding="utf-8")
    out = mgr.flush_all()
    assert out[cfg.resolve()].status == "clean"
    assert out[cfg.resolve()].committed == [STEP]
    assert out[other.resolve()].status == "conflict"
    assert out[other.resolve()].conflicts == ["a"]
    # 互不影响（§3.10）：cfg 的提交已落盘
    assert "step_interval: 1920." in cfg.read_text()
    assert other.read_text() == "a: 999\n"
    mgr.stop_all()


def test_start_stop_pollers(cfg, tmp_path):
    other = tmp_path / "other.yaml"
    other.write_text("a: 1\n", encoding="utf-8")
    mgr = SessionManager()
    r1, r2 = mgr.open_files([cfg, other], poll_interval=0.05)
    mgr.start_pollers()
    assert r1.session.poller.running and r2.session.poller.running
    mgr.stop_all()
    assert not r1.session.poller.running and not r2.session.poller.running


def test_last_open_results_seam_and_public_rebind(cfg):
    """G2c 接缝：last_open_results 初始为空、由调用方（cli）setattr；
    open_files 产出的会话可经公开 API set_on_state_change 重绑。"""
    mgr = SessionManager()
    assert mgr.last_open_results == []          # 初始空（GUI 侧 getattr 防御）
    results = mgr.open_files([cfg])             # 不带回调（CLI 语义）
    mgr.last_open_results = results             # cli setattr（接缝契约）
    assert mgr.last_open_results is results
    events: list[str] = []
    results[0].session.set_on_state_change(events.append)
    # K-9 复核语义（验收后修复）：MODIFIED 回调按当前基线复核磁盘，
    # 须真实改盘使事件为真（否则判为自身提交回声被忽略）。
    cfg.write_text("a: 2\n", encoding="utf-8")
    results[0].session._on_poller_change(FileState.MODIFIED)
    assert events == ["external_modified"]
    mgr.stop_all()
