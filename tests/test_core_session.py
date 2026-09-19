"""FileSession 单元测试（§7.5/§7.6/§7.7）：手工 doc + FakeAdapter。

覆盖门控（中间态/非法/declared block/inferred warn/dormant/readonly）、
提交流（failed/conflict/gone/重载失败）、冲突处置（reload/force/pause）、
undo/redo（200 上限、冲突放回、清 redo）、close/flush/abandon。
集成测试（YamlAdapter + testdata 拷贝）见 test_core_session_manager.py。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from configer.core import session as session_mod
from configer.core.session import (
    UNDO_LIMIT,
    CommitResult,
    FileSession,
)
from configer.core.writeback import WriteError
from configer.model import (
    PROV_DECLARED,
    PROV_INFERRED,
    ConfigDoc,
    ConfigItem,
    EditOp,
    LiteralStyle,
    RangeConstraint,
)


# ---------------------------------------------------------------------------
# FakeAdapter：`key = <int>` 行式假格式，load/save 可控失败
# ---------------------------------------------------------------------------


class FakeAdapter:
    name = "fake"
    extensions = (".cfg",)

    def __init__(self) -> None:
        self.save_calls: list[list[EditOp]] = []
        self.load_count = 0
        self.save_exc: Exception | None = None   # save 抛此异常
        self.load_exc: Exception | None = None    # 每次 load 都抛
        self.load_exc_once_at: int | None = None  # 第 N 次 load 抛（1 起）

    def detect(self, path: Path, head: bytes) -> bool:
        return Path(path).suffix == ".cfg"

    def load(self, path: Path) -> tuple[ConfigDoc, list]:
        self.load_count += 1
        if self.load_exc is not None:
            raise self.load_exc
        if self.load_exc_once_at is not None and \
                self.load_exc_once_at == self.load_count:
            raise OSError("重载失败（测试注入）")
        raw = Path(path).read_bytes()
        doc = ConfigDoc(format=self.name, path=Path(path))
        doc.original_bytes = raw
        doc.refresh_hash()
        text = raw.decode("utf-8")
        for line in text.splitlines():
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            doc.items.append(ConfigItem(
                path=key.strip(), group="（根级）", type="int",
                value=int(val.strip()), raw_literal=val.strip(),
                literal_style=LiteralStyle(), locator=key.strip(),
            ))
        return doc, []

    def save(self, doc: ConfigDoc, edits) -> bytes:
        self.save_calls.append(list(edits))
        if self.save_exc is not None:
            raise self.save_exc
        text = doc.original_bytes.decode("utf-8")
        by_path = {it.path: it for it in doc.items}
        for op in edits:
            it = by_path.get(op.path)
            if it is None:
                raise ValueError(f"未知 path {op.path!r}")
            if it.readonly:
                raise ValueError(f"{op.path!r} 只读")
            lines = text.splitlines(keepends=True)
            for i, line in enumerate(lines):
                if line.split("=")[0].strip() == op.path:
                    eol = "\n" if line.endswith("\n") else ""
                    lines[i] = f"{op.path} = {op.new_value}{eol}"
                    break
            text = "".join(lines)
        return text.encode("utf-8")


def make_file(tmp_path: Path, values: dict[str, int],
              name: str = "f.cfg") -> Path:
    p = tmp_path / name
    p.write_text(
        "".join(f"{k} = {v}\n" for k, v in values.items()), encoding="utf-8"
    )
    return p


def make_session(tmp_path: Path, values: dict[str, int] | None = None,
                 adapter: FakeAdapter | None = None,
                 events: list[str] | None = None,
                 name: str = "f.cfg") -> tuple[FileSession, FakeAdapter, Path]:
    adapter = adapter or FakeAdapter()
    p = make_file(tmp_path, values if values is not None else {"foo": 5},
                  name=name)
    doc, diags = adapter.load(p)
    s = FileSession(
        p, adapter, doc, diags,
        on_state_change=(events.append if events is not None else None),
    )
    return s, adapter, p


def make_item_doc(tmp_path: Path, item: ConfigItem,
                  raw: str | None = None) -> tuple[ConfigDoc, Path]:
    """手工 doc：单条目，值文本 = raw（默认 str(item.value)）。"""
    p = tmp_path / "manual.cfg"
    text = raw if raw is not None else str(item.value)
    p.write_text(text + "\n", encoding="utf-8")
    doc = ConfigDoc(format="fake", path=p)
    doc.original_bytes = p.read_bytes()
    doc.refresh_hash()
    doc.items = [item]
    return doc, p


class ManualAdapter(FakeAdapter):
    """save 按整文件替换为 `path = value` 单行；load 返回预设 doc 结构。

    用于 dormant/readonly/约束等手工 doc 测试：不解析盘上内容，只把
    ConfigItem 元数据原样重建（值取盘上文本）。
    """

    def __init__(self, item: ConfigItem) -> None:
        super().__init__()
        self._item = item

    def load(self, path: Path) -> tuple[ConfigDoc, list]:
        self.load_count += 1
        if self.load_exc is not None:
            raise self.load_exc
        raw = Path(path).read_bytes()
        doc = ConfigDoc(format="fake", path=Path(path))
        doc.original_bytes = raw
        doc.refresh_hash()
        import dataclasses
        new_item = dataclasses.replace(self._item)
        text = raw.decode("utf-8").strip()
        if not new_item.readonly and new_item.type == "int":
            new_item.value = int(text.split("=")[-1].strip())
            new_item.raw_literal = text.split("=")[-1].strip()
        doc.items = [new_item]
        return doc, []

    def save(self, doc: ConfigDoc, edits) -> bytes:
        self.save_calls.append(list(edits))
        if self.save_exc is not None:
            raise self.save_exc
        op = edits[0]
        it = next(i for i in doc.items if i.path == op.path)
        if it.readonly:
            raise ValueError(f"{op.path!r} 只读")
        return f"{op.path} = {op.new_value}\n".encode("utf-8")


def manual_session(tmp_path: Path, item: ConfigItem,
                   events: list[str] | None = None):
    adapter = ManualAdapter(item)
    doc, p = make_item_doc(tmp_path, item)
    p.write_text(f"{item.path} = {item.value}\n", encoding="utf-8")
    doc.original_bytes = p.read_bytes()
    doc.refresh_hash()
    s = FileSession(p, adapter, doc, [],
                    on_state_change=(events.append if events is not None
                                     else None))
    return s, adapter, p


# ---------------------------------------------------------------------------
# §7.5 门控与暂态
# ---------------------------------------------------------------------------


def test_intermediate_keeps_old_pending(tmp_path):
    s, _, _ = make_session(tmp_path, {"foo": 5})
    assert s.set_pending_text("foo", "7").accepted
    r = s.set_pending_text("foo", "-")
    assert not r.accepted and r.level == "intermediate"
    # 裁量 1：中间态不清除已有暂态（键入进行中）
    assert s.get_pending("foo") == 7


def test_invalid_clears_pending_and_records_last_blocked(tmp_path):
    s, _, _ = make_session(tmp_path, {"foo": 5})
    s.set_pending_text("foo", "7")
    r = s.set_pending_text("foo", "abc")
    assert not r.accepted and r.level == "invalid"
    # 裁量 1：确定性非法清除该 path 旧暂态（控件真相模型）
    assert s.get_pending("foo") is None
    # 裁量 2：last_blocked 记录
    assert "foo" in s.last_blocked_items()
    # 后续成功进暂态 → last_blocked 清除
    assert s.set_pending_text("foo", "9").accepted
    assert "foo" not in s.last_blocked_items()


def test_declared_range_block(tmp_path):
    item = ConfigItem(
        path="m", group="g", type="int", value=5, raw_literal="5",
        range=RangeConstraint(min=0, max=10, provenance=PROV_DECLARED),
    )
    s, _, _ = manual_session(tmp_path, item)
    r = s.set_pending_text("m", "99")
    assert not r.accepted and r.level == "block"
    assert "declared" in (r.reason or "")
    assert s.get_pending("m") is None
    assert "m" in s.last_blocked_items()


def test_inferred_warn_commits(tmp_path):
    # I-6：inferred 违规仅黄警，照常提交、盘上字节已改
    item = ConfigItem(
        path="m", group="g", type="int", value=5, raw_literal="5",
        range=RangeConstraint(min=0, max=10, provenance=PROV_INFERRED),
    )
    s, _, p = manual_session(tmp_path, item)
    r = s.set_pending_text("m", "99")
    assert r.accepted and r.level == "warn"
    c = s.commit_pending("m")
    assert c.status == "committed"
    assert p.read_text() == "m = 99\n"


def test_dormant_commits(tmp_path):
    # §7.5：dormant 允许编辑并实时落盘，不是拦截条件（YC-7：手工 doc）
    item = ConfigItem(path="m", group="g", type="int", value=5,
                      raw_literal="5", dormant=True,
                      dormant_reason_text="# 当前不生效")
    s, _, p = manual_session(tmp_path, item)
    r = s.set_pending_text("m", "6")
    assert r.accepted and r.level == "ok"
    assert s.commit_pending("m").status == "committed"
    assert p.read_text() == "m = 6\n"


def test_readonly_block(tmp_path):
    from configer.model import READONLY_CONTAINER
    item = ConfigItem(path="m", group="g", type=None, value=None,
                      raw_literal="[1, 2]", readonly=True,
                      readonly_reason=READONLY_CONTAINER)
    s, _, _ = manual_session(tmp_path, item)
    r = s.set_pending_text("m", "6")
    assert not r.accepted and r.level in ("block", "invalid")
    assert s.pending_items() == {}


def test_bool_pending(tmp_path):
    s, _, _ = make_session(tmp_path, {"foo": 5})
    r = s.set_pending_bool("foo", True)
    assert not r.accepted and r.level == "invalid"  # 非 bool 用开关=程序侧错误


def test_unknown_path_raises(tmp_path):
    s, _, _ = make_session(tmp_path, {"foo": 5})
    with pytest.raises(ValueError):
        s.set_pending_text("nope", "1")
    with pytest.raises(ValueError):
        s.set_pending_bool("nope", True)
    with pytest.raises(ValueError):
        s.commit_pending("nope")
    with pytest.raises(ValueError):
        s.get_pending("nope")
    with pytest.raises(ValueError):
        s.clear_pending("nope")


def test_get_clear_pending(tmp_path):
    s, _, _ = make_session(tmp_path, {"foo": 5})
    s.set_pending_text("foo", "8")
    assert s.get_pending("foo") == 8 and s.has_pending("foo")
    s.clear_pending("foo")
    assert s.get_pending("foo") is None and not s.has_pending("foo")
    assert s.commit_pending("foo").status == "noop"


# ---------------------------------------------------------------------------
# §7.5 提交与提交后重载裁决
# ---------------------------------------------------------------------------


def test_commit_updates_doc_baseline_undo(tmp_path):
    s, adapter, p = make_session(tmp_path, {"foo": 5})
    old_doc = s.doc
    s.set_pending_text("foo", "8")
    c = s.commit_pending("foo")
    assert c.status == "committed"
    assert (c.old_value, c.new_value) == (5, 8)
    assert p.read_text() == "foo = 8\n"
    # 架构裁决：提交后重新 load，doc 被替换、基线=新 doc
    assert s.doc is not old_doc
    assert adapter.load_count == 2  # 初始 + 提交后重载
    assert s.doc.original_bytes == p.read_bytes()
    from configer.model import compute_hash
    assert s.doc.content_hash == compute_hash(p.read_bytes())
    assert s.poller.get_baseline() == s.doc.content_hash
    assert s.undo_depth() == 1 and s.redo_depth() == 0
    assert s.pending_items() == {}


def test_double_commit_same_item(tmp_path):
    # 重载裁决核心验证：提交后旧 locator 失效，第二次提交必须走新 doc
    s, _, p = make_session(tmp_path, {"foo": 5})
    s.set_pending_text("foo", "6")
    assert s.commit_pending("foo").status == "committed"
    s.set_pending_text("foo", "7")
    assert s.commit_pending("foo").status == "committed"  # 不抛 RuntimeError
    assert p.read_text() == "foo = 7\n"


def test_commit_save_error_failed(tmp_path):
    s, adapter, p = make_session(tmp_path, {"foo": 5})
    adapter.save_exc = ValueError("save 注入失败")
    s.set_pending_text("foo", "8")
    c = s.commit_pending("foo")
    assert c.status == "failed" and "save 注入失败" in (c.reason or "")
    assert s.get_pending("foo") == 8  # 暂态保留
    assert p.read_text() == "foo = 5\n"


def test_commit_write_error_failed_and_retry(tmp_path, monkeypatch):
    s, _, p = make_session(tmp_path, {"foo": 5})
    s.set_pending_text("foo", "8")

    def boom(path, payload):
        raise WriteError("磁盘炸了")

    monkeypatch.setattr(session_mod, "atomic_write", boom)
    c = s.commit_pending("foo")
    assert c.status == "failed" and "磁盘炸了" in (c.reason or "")
    assert s.get_pending("foo") == 8  # 暂态保留可重试
    monkeypatch.undo()
    assert s.commit_pending("foo").status == "committed"
    assert p.read_text() == "foo = 8\n"


def test_commit_reload_failure_treated_as_gone(tmp_path, monkeypatch):
    # 架构裁决：写盘成功后重载失败 → 按 GONE 处置
    s, adapter, p = make_session(tmp_path, {"foo": 5})
    real_load = adapter.load

    def flaky_load(path):
        n = real_load(path)
        if adapter.load_count >= 2:
            raise OSError("重载失败（注入）")
        return n

    monkeypatch.setattr(adapter, "load", flaky_load)
    events: list[str] = []
    s.set_on_state_change(events.append)   # G2c：公开重绑 API
    s.set_pending_text("foo", "8")
    c = s.commit_pending("foo")
    assert c.status == "gone"
    assert s.gone_readonly
    assert events == ["gone"]
    assert p.read_text() == "foo = 8\n"  # 写盘本身成功


# ---------------------------------------------------------------------------
# §7.6 基线冲突与处置
# ---------------------------------------------------------------------------


def test_conflict_reload(tmp_path):
    s, _, p = make_session(tmp_path, {"foo": 5})
    s.set_pending_text("foo", "8")
    s.commit_pending("foo")  # 建立一次提交历史 → undo 非空
    s.set_pending_text("foo", "9")
    p.write_text("foo = 100\n", encoding="utf-8")  # 外部修改
    c = s.commit_pending("foo")
    assert c.status == "conflict"
    assert p.read_text() == "foo = 100\n"  # 未写盘
    assert s.get_pending("foo") == 9       # 暂态退回保留
    r = s.resolve_conflict("reload")
    assert r.status == "reloaded"
    assert s.pending_items() == {}          # 全部暂态丢弃
    assert s.undo_depth() == 0 and s.redo_depth() == 0  # §7.6：栈清空
    assert s.doc.items[0].value == 100      # doc=外部内容
    assert not s.paused
    # reload 后可正常继续编辑提交
    s.set_pending_text("foo", "101")
    assert s.commit_pending("foo").status == "committed"
    assert p.read_text() == "foo = 101\n"


def test_conflict_force(tmp_path):
    s, _, p = make_session(tmp_path, {"foo": 5})
    s.set_pending_text("foo", "8")
    p.write_text("foo = 100\n", encoding="utf-8")  # 外部修改
    assert s.commit_pending("foo").status == "conflict"
    r = s.resolve_conflict("force")
    assert r.status == "committed"
    assert (r.old_value, r.new_value) == (5, 8)
    assert p.read_text() == "foo = 8\n"  # 盘上=我方字节，外部修改被覆盖
    assert s.undo_depth() == 1
    assert s.doc.items[0].value == 8     # 正常提交后流程（重载）
    assert s.pending_items() == {}


def test_force_without_conflict_guides_reload(tmp_path):
    s, _, _ = make_session(tmp_path, {"foo": 5})
    r = s.resolve_conflict("force")
    assert r.status == "failed"
    assert "reload" in (r.reason or "") or "重新加载" in (r.reason or "")


def test_resolve_conflict_bad_choice(tmp_path):
    s, _, _ = make_session(tmp_path, {"foo": 5})
    with pytest.raises(ValueError):
        s.resolve_conflict("merge")


def test_pause_blocks_everything(tmp_path):
    s, _, p = make_session(tmp_path, {"foo": 5})
    s.set_pending_text("foo", "8")
    s.commit_pending("foo")
    s.pause_auto_commit()
    assert s.paused
    s.set_pending_text("foo", "9")
    assert s.commit_pending("foo").status == "paused"
    assert s.undo().status == "paused"
    assert s.redo().status == "paused"
    assert p.read_text() == "foo = 8\n"
    s.resume_auto_commit()
    assert not s.paused
    assert s.commit_pending("foo").status == "committed"
    assert p.read_text() == "foo = 9\n"


# ---------------------------------------------------------------------------
# §7.6 GONE 与恢复探测
# ---------------------------------------------------------------------------


def test_gone_and_recovery_probe(tmp_path):
    events: list[str] = []
    s, _, p = make_session(tmp_path, {"foo": 5}, events=events)
    s.set_pending_text("foo", "8")
    p.unlink()  # 删除文件
    c = s.commit_pending("foo")
    assert c.status == "gone"
    assert s.gone_readonly
    assert events == ["gone"]
    # gone 只读态：拒绝编辑
    r = s.set_pending_text("foo", "9")
    assert not r.accepted and r.level == "block"
    assert s.commit_pending("foo").status == "gone"
    # 恢复（原字节）→ set_pending 入口主动探测解除（poller 不回调此转换）
    p.write_text("foo = 5\n", encoding="utf-8")
    r = s.set_pending_text("foo", "9")
    assert r.accepted
    assert not s.gone_readonly
    assert "recovered" in events
    assert s.commit_pending("foo").status == "committed"
    assert p.read_text() == "foo = 9\n"


def test_gone_recovery_with_external_change(tmp_path):
    # 恢复但内容已变 → 解除 gone + external_modified，提交撞冲突
    events: list[str] = []
    s, _, p = make_session(tmp_path, {"foo": 5}, events=events)
    p.unlink()
    assert s.commit_pending("foo").status == "noop"  # 无暂态 → noop，未探测
    assert not s.gone_readonly
    s.set_pending_text("foo", "8")  # 未标记 gone → 照常进暂态
    assert s.commit_pending("foo").status == "gone"  # 提交期探测到不可读
    assert s.gone_readonly and events == ["gone"]
    p.write_text("foo = 77\n", encoding="utf-8")
    r = s.set_pending_text("foo", "8")
    assert r.accepted and not s.gone_readonly
    assert "external_modified" in events
    assert s.commit_pending("foo").status == "conflict"


# ---------------------------------------------------------------------------
# §7.7 undo / redo
# ---------------------------------------------------------------------------


def test_undo_redo_disk_and_stacks(tmp_path):
    s, _, p = make_session(tmp_path, {"foo": 5})
    s.set_pending_text("foo", "6")
    s.commit_pending("foo")
    s.set_pending_text("foo", "7")
    s.commit_pending("foo")
    assert s.undo_depth() == 2 and s.redo_depth() == 0

    u = s.undo()
    assert u.status == "committed" and (u.old_value, u.new_value) == (7, 6)
    assert p.read_text() == "foo = 6\n"
    assert s.undo_depth() == 1 and s.redo_depth() == 1

    u = s.undo()
    assert u.status == "committed"
    assert p.read_text() == "foo = 5\n"
    assert s.undo_depth() == 0 and s.redo_depth() == 2
    assert s.undo().status == "noop"

    d = s.redo()
    assert d.status == "committed" and (d.old_value, d.new_value) == (5, 6)
    assert p.read_text() == "foo = 6\n"
    assert s.undo_depth() == 1 and s.redo_depth() == 1
    s.redo()
    assert p.read_text() == "foo = 7\n"
    assert s.redo().status == "noop"


def test_undo_conflict_puts_entry_back(tmp_path):
    s, _, p = make_session(tmp_path, {"foo": 5})
    s.set_pending_text("foo", "6")
    s.commit_pending("foo")
    p.write_text("foo = 99\n", encoding="utf-8")  # 外部修改
    u = s.undo()
    assert u.status == "conflict"
    # 裁量 4：弹出条目放回原栈（可重试、栈一致）
    assert s.undo_depth() == 1 and s.redo_depth() == 0
    assert p.read_text() == "foo = 99\n"
    # reload 处置 → 栈清空
    assert s.resolve_conflict("reload").status == "reloaded"
    assert s.undo_depth() == 0 and s.redo_depth() == 0


def test_undo_conflict_force_completes_move(tmp_path):
    # undo 撞冲突后 force：条目从 undo 栈移入 redo 栈（身份移除，不重复）
    s, _, p = make_session(tmp_path, {"foo": 5})
    s.set_pending_text("foo", "6")
    s.commit_pending("foo")
    p.write_text("foo = 99\n", encoding="utf-8")
    assert s.undo().status == "conflict"
    r = s.resolve_conflict("force")
    assert r.status == "committed"
    assert p.read_text() == "foo = 5\n"  # undo 的字节被强制写入
    assert s.undo_depth() == 0 and s.redo_depth() == 1
    assert s.redo().status == "committed"
    assert p.read_text() == "foo = 6\n"


def test_undo_limit_200(tmp_path):
    s, _, p = make_session(tmp_path, {"foo": 0})
    for i in range(1, UNDO_LIMIT + 2):  # 201 次提交
        s.set_pending_text("foo", str(i))
        assert s.commit_pending("foo").status == "committed"
    assert s.undo_depth() == UNDO_LIMIT
    # 最旧条目（0→1）已淘汰：undo 到底后盘上停在 1，回不到 0
    for _ in range(UNDO_LIMIT):
        assert s.undo().status == "committed"
    assert p.read_text() == "foo = 1\n"
    assert s.undo().status == "noop"


def test_new_commit_clears_redo(tmp_path):
    s, _, p = make_session(tmp_path, {"foo": 5})
    s.set_pending_text("foo", "6")
    s.commit_pending("foo")
    s.undo()
    assert s.redo_depth() == 1
    s.set_pending_text("foo", "42")
    s.commit_pending("foo")
    assert s.redo_depth() == 0  # 任一新提交落盘后 redo 清空（§7.7）
    # 第一次提交 entry(5,6)；undo 后该 entry 移入 redo；新提交清 redo 并推
    # entry(5,42)（old_value=当前 doc 值 5）→ undo 栈只剩 [(5,42)]
    assert s.undo_depth() == 1
    assert p.read_text() == "foo = 42\n"
    assert s.undo().status == "committed"
    assert p.read_text() == "foo = 5\n"


# ---------------------------------------------------------------------------
# §7.7 close / flush / abandon
# ---------------------------------------------------------------------------


def test_close_commits_pending(tmp_path):
    s, _, p = make_session(tmp_path, {"foo": 5, "bar": 1})
    s.set_pending_text("foo", "6")
    s.set_pending_text("bar", "2")
    r = s.close()
    assert r.status == "closed"
    assert p.read_text() == "foo = 6\nbar = 2\n"
    assert s.closed
    assert s.poller.running is False
    with pytest.raises(RuntimeError):
        s.commit_pending("foo")
    with pytest.raises(RuntimeError):
        s.set_pending_text("foo", "1")


def test_close_conflict_not_closed(tmp_path):
    s, _, p = make_session(tmp_path, {"foo": 5})
    s.set_pending_text("foo", "6")
    p.write_text("foo = 99\n", encoding="utf-8")
    r = s.close()
    assert r.status == "conflict"
    assert not s.closed
    assert s.get_pending("foo") == 6  # 暂态保留
    # 抉择后可关闭
    assert s.resolve_conflict("reload").status == "reloaded"
    assert s.close().status == "closed"


def test_close_failed_blocks_then_discard(tmp_path, monkeypatch):
    s, _, p = make_session(tmp_path, {"foo": 5})
    s.set_pending_text("foo", "6")

    def boom(path, payload):
        raise WriteError("权限拒绝")

    monkeypatch.setattr(session_mod, "atomic_write", boom)
    r = s.close()
    assert r.status == "failed"
    assert [path for path, _ in r.items] == ["foo"]
    assert not s.closed
    r = s.close(discard_illegal=True)
    assert r.status == "closed" and s.closed
    assert p.read_text() == "foo = 5\n"  # 放弃暂态，盘上未动


def test_close_gone_with_pending_need_confirm(tmp_path):
    s, _, p = make_session(tmp_path, {"foo": 5})
    s.set_pending_text("foo", "6")
    p.unlink()
    r = s.close()
    assert r.status == "need_confirm"  # 裁量 8
    assert [path for path, _ in r.items] == ["foo"]
    assert not s.closed
    assert s.close(discard_illegal=True).status == "closed"


def test_close_gone_without_pending(tmp_path):
    s, _, p = make_session(tmp_path, {"foo": 5})
    p.unlink()
    assert s.close().status == "closed"  # 无暂态可直接关闭


def test_flush_clean_conflict_failed(tmp_path, monkeypatch):
    s, _, p = make_session(tmp_path, {"foo": 5, "bar": 1, "baz": 0})
    assert s.flush().status == "clean"  # 无暂态

    s.set_pending_text("foo", "6")
    s.set_pending_text("bar", "2")
    r = s.flush()
    assert r.status == "clean"
    assert sorted(r.committed) == ["bar", "foo"]
    assert p.read_text() == "foo = 6\nbar = 2\nbaz = 0\n"

    s.set_pending_text("baz", "3")
    p.write_text("foo = 6\nbar = 2\nbaz = 999\n", encoding="utf-8")
    r = s.flush()
    assert r.status == "conflict" and r.conflicts == ["baz"]

    s.resolve_conflict("reload")
    s.set_pending_text("baz", "4")
    monkeypatch.setattr(
        session_mod, "atomic_write",
        lambda path, payload: (_ for _ in ()).throw(WriteError("IO 炸")),
    )
    r = s.flush()
    assert r.status == "failed"
    assert [path for path, _ in r.failures] == ["baz"]


def test_flush_paused(tmp_path):
    s, _, _ = make_session(tmp_path, {"foo": 5})
    s.set_pending_text("foo", "6")
    s.pause_auto_commit()
    r = s.flush()
    assert r.status == "paused"
    assert [path for path, _ in r.failures] == ["foo"]


def test_abandon_keeps_last_blocked(tmp_path):
    s, _, p = make_session(tmp_path, {"foo": 5})
    s.set_pending_text("foo", "6")
    s.set_pending_text("foo", "xx")  # invalid → last_blocked
    s.abandon()
    assert s.pending_items() == {}
    assert "foo" in s.last_blocked_items()  # 退出清单参考（裁量 2）
    assert p.read_text() == "foo = 5\n"


# ---------------------------------------------------------------------------
# G2c：set_on_state_change 公开重绑 API（正规化，取代私有属性 hack）
# ---------------------------------------------------------------------------


def test_set_on_state_change_rebind_and_unset(tmp_path):
    """公开重绑：新回调立即生效；传 None 解绑（不再转发）。

    事件用 poller 边沿语义驱动。K-9 复核语义（验收后修复）后，MODIFIED
    回调会按当前基线 check_once 复核——磁盘与基线一致时判为自身提交
    回声并忽略，故此处先真实改盘使该事件为真（否则会被复核吞掉）。
    """
    from configer.core.poller import FileState

    s, _, p = make_session(tmp_path, {"foo": 5})
    first: list[str] = []
    second: list[str] = []
    s.set_on_state_change(first.append)
    s._on_poller_change(FileState.GONE)
    assert first == ["gone"]
    s.set_on_state_change(second.append)   # 运行期重绑（G2c 正规化）
    p.write_text("foo = 6\n", encoding="utf-8")   # 真实外部修改（K-9 复核为真）
    s._on_poller_change(FileState.MODIFIED)
    assert first == ["gone"] and second == ["external_modified"]
    s.set_on_state_change(None)            # 解绑
    s._on_poller_change(FileState.GONE)
    assert second == ["external_modified"]


def test_poller_echo_of_own_commit_is_suppressed(tmp_path):
    """K-9 竞态复核：自身提交落盘后的 poller MODIFIED 回声不冒横幅。

    场景（验收缺陷 2）：commit 在「写盘 → 基线更新」窗口内，poller 以
    旧基线读到本会话刚落盘的新字节并边沿回调 MODIFIED——session 须
    复核磁盘 == 当前基线 → 忽略该次事件（不转发 external_modified），
    且复位 poller 边沿，保证紧随其后的真实外部修改仍能触发。
    """
    from configer.core.poller import FileState

    s, _, p = make_session(tmp_path, {"foo": 5})
    events: list[str] = []
    s.set_on_state_change(events.append)

    class _SpyPoller:
        """记录 reset_edge 调用的最小替身（本测只走回调复核路径）。"""

        def __init__(self):
            self.resets: list[FileState] = []

        def reset_edge(self, state=FileState.UNCHANGED):
            self.resets.append(state)

    spy = _SpyPoller()
    real_poller, s._poller = s._poller, spy
    try:
        # 模拟 poller 回声：磁盘 == 当前基线（提交已完成、基线已同步），
        # 但 poller 仍以旧基线边沿触发 MODIFIED。
        s._on_poller_change(FileState.MODIFIED)
        assert events == []                       # 回声被忽略，不冒横幅
        assert spy.resets == [FileState.UNCHANGED]  # 边沿已复位（不吞后续）
    finally:
        s._poller = real_poller

    # 真实外部修改：磁盘 != 基线 → 照常转发（G2b 检测语义不回归）
    p.write_text("foo = 7\n", encoding="utf-8")
    s._on_poller_change(FileState.MODIFIED)
    assert events == ["external_modified"]


def test_poller_echo_suppressed_when_commit_in_flight(tmp_path, monkeypatch):
    """K-9 确定性时序：poller 回调与提交临界区并发时仍不误报。

    复核持 session 锁 → 与提交路径串行化：回调阻塞至提交完成（doc/基线
    均已换新）后才复核，磁盘 == 新基线 → 忽略。测试用 adapter.load 钩子
    在提交临界区内注入回调（另一线程），结果与线程调度时序无关。
    """
    import threading
    from configer.core.poller import FileState

    s, adapter, p = make_session(tmp_path, {"foo": 5})
    events: list[str] = []
    s.set_on_state_change(events.append)
    assert s.set_pending_text("foo", "6").accepted   # 进暂态

    real_load = adapter.load
    echo_threads: list[threading.Thread] = []

    def racing_load(path):
        # 提交线程已持锁、新字节已落盘、基线尚未换新——此刻 poller
        # 读盘并以旧基线判定 MODIFIED，回调进入（将阻塞在 session 锁上）。
        t = threading.Thread(target=s._on_poller_change,
                             args=(FileState.MODIFIED,))
        echo_threads.append(t)
        t.start()
        import time
        time.sleep(0.05)           # 确保回调已发起并阻塞在锁上
        return real_load(path)

    monkeypatch.setattr(adapter, "load", racing_load)
    result = s.commit_pending("foo")
    assert result.status == "committed"
    echo_threads[-1].join(timeout=5)
    assert not echo_threads[-1].is_alive()
    assert events == []            # 提交全程无 external_modified 误报
    assert p.read_text(encoding="utf-8").startswith("foo = 6")
