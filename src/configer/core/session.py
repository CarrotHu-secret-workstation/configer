"""编辑生命周期与提交门控、撤销/重做（规范 §7.5–§7.7、§3.10）。

承载语义：
- §7.5 实时落盘生命周期（每文件独立，§3.10）：输入 → 暂态 → 门控校验
  （validation，§8.2）→ 提交（EditOp → 适配器 save → §7.6 基线校验 → §7.4
  原子写 → **重新 load**（架构裁决：save 的 doc 必须是 load() 产物、locator
  有效，提交后基线字节已变、旧 locator 失效）→ 更新基线并记入 undo 栈）
  → 失败分流（基线冲突弹模态 / 其他错误可重试；不阻塞其他文件）。
  dormant 允许编辑并实时落盘，不是拦截条件；readonly 条目不可编辑。
- §7.6 提交前基线校验 + 冲突处置（reload / force）+ 外部轮询接线
  （ExternalChangePoller，边沿触发；GONE → 条目只读态，恢复探测解除）。
- §7.7 每文件独立 undo/redo 栈（只记已提交编辑，undo 栈上限 200 条，新提交
  清空 redo 栈）；undo/redo 走完整提交流程；close/flush/abandon 退出语义
  （exit 0/1/3 由上层 CLI 落地，§9.2）。

提交时机（§7.5 第 3 步，v0.4 决议：点击类即时 / 文本类 400ms 防抖 / 失焦
立即）属 UI 层职责：核心只提供 ``set_pending_*`` 与 ``commit_pending``，
防抖计时器由前端实现。

线程模型（裁量 10）：FileSession 内部 RLock 串行化
set_pending / commit / undo / redo / resolve_conflict / close / flush；
poller 回调运行在轮询线程，session 侧只置标志 + 转发——**on_state_change
回调可能在非主线程被调用，UI 层必须自行调度到主线程**。

扩展码登记（裁量 7）：``E-READ``——文件不存在/不可读（SessionManager 打开
期），类比 yaml 适配器 ``W-DOT-KEY`` 的模块级扩展登记方式。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from ..bytesio import UTF8_BOM, restore_bom
from ..model import (
    CODE_E_PARSE,
    SEV_ERROR,
    ConfigDoc,
    Diagnostic,
    EditOp,
    compute_hash,
)
from ..registry import Registry
from .poller import ExternalChangePoller, FileState, check_once
from .validation import (
    GateResult,
    gate_edit,
    parse_bool_input,
    parse_text_input,
)
from .writeback import WriteError, atomic_write

__all__ = [
    "CODE_E_READ",
    "UNDO_LIMIT",
    "PendingResult",
    "CommitResult",
    "UndoEntry",
    "CloseResult",
    "FlushResult",
    "OpenResult",
    "FileSession",
    "SessionManager",
]

# 扩展码登记（裁量 7）：打开期文件不存在 / 不可读。
CODE_E_READ = "E-READ"

# §7.7：undo 栈上限 200 条，超出淘汰最旧（v0.4 决议）。
UNDO_LIMIT = 200


# ---------------------------------------------------------------------------
# 结果 dataclass
# ---------------------------------------------------------------------------


@dataclass
class PendingResult:
    """:meth:`FileSession.set_pending_text` / ``set_pending_bool`` 的结果。

    - ``accepted=True``：已进入暂态（``level='ok'|'warn'``；warn=inferred
      违规，照常提交、UI 持续黄标，I-6）；``value`` 为门控规范化值；
    - ``level='intermediate'``：中间态（§10 U-6），不进暂态、**不清除**已有
      暂态、不红标（裁量 1）；
    - ``level='invalid'``：确定性非法输入（类型解析失败），不进暂态、
      **清除**该 path 旧暂态、UI 红标（裁量 1）；
    - ``level='block'``：门控拦截（declared 违规 / readonly / 类型不符），
      不进暂态、清除该 path 旧暂态、UI 红标（declared 违规 reason 含
      "declared" 钩子，§7.5）。
    """

    accepted: bool
    level: Literal["ok", "warn", "block", "intermediate", "invalid"]
    reason: str | None = None
    value: Any = None


@dataclass
class CommitResult:
    """提交类操作（commit_pending / undo / redo / resolve_conflict）的结果。

    ``status`` 取值：

    - ``'committed'``：已落盘并完成提交后流程（重载、基线更新、栈维护）；
      附 ``old_value`` / ``new_value``；
    - ``'reloaded'``：resolve_conflict('reload') 成功——暂态已丢弃、
      undo/redo 已清空、已按磁盘内容重载并恢复自动提交（裁量 3）；
    - ``'noop'``：无暂态 / 栈空，无操作；
    - ``'paused'``：该文件已暂停自动提交（§7.6 用户关闭冲突模态），
      一律拒绝提交类操作（裁量 5）；
    - ``'conflict'``：§7.6 基线校验不一致，未写盘；待写字节已暂存，
      等待 :meth:`FileSession.resolve_conflict` 抉择；
    - ``'gone'``：文件不可读（删除/移动/权限），未写盘，条目已转只读态；
    - ``'failed'``：写盘失败（WriteError）或其他失败，暂态/栈条目保留可重试。
    """

    status: Literal[
        "committed", "reloaded", "noop", "paused", "conflict", "gone", "failed"
    ]
    path: str | None = None
    old_value: Any = None
    new_value: Any = None
    reason: str | None = None


@dataclass
class UndoEntry:
    """undo/redo 栈条目：一次已提交编辑（§7.7）。

    按 ``path`` 跨重载稳定（架构裁决：提交后 doc 重载、locator 失效，
    栈条目只记 path 与新旧值，重放时按当前 doc 重新生成 EditOp）。
    """

    path: str
    old_value: Any
    new_value: Any


@dataclass
class CloseResult:
    """:meth:`FileSession.close` 的结果（§7.7 关闭文件语义）。

    - ``'closed'``：已关闭，poller 已停、暂态/栈/诊断已释放；
    - ``'need_confirm'``：暂态无法落盘（gone 且有暂态，裁量 8），UI 弹
      确认框（放弃暂态并关闭 = 重调 ``close(discard_illegal=True)`` /
      取消关闭）；``items`` 为 (path, reason) 清单；
    - ``'conflict'``：关闭触发的提交撞基线冲突，§7.6 抉择完成前**不关闭**；
    - ``'failed'``：关闭触发的提交失败（权限/磁盘 I/O），阻止关闭，可重试
      或 ``close(discard_illegal=True)``；``items`` 为失败清单。
    """

    status: Literal["closed", "need_confirm", "conflict", "failed"]
    items: list[tuple[str, str]] = field(default_factory=list)
    reason: str | None = None


@dataclass
class FlushResult:
    """:meth:`FileSession.flush` 的结果（§7.7 退出语义）。

    - ``'clean'``：全部暂态已提交（或本无暂态）；
    - ``'conflict'``：撞基线冲突，``conflicts`` 为未决 path 清单（UI 弹
      §7.6 模态，含"取消退出"）；
    - ``'failed'``：提交失败，``failures`` 为 (path, reason) 清单（UI 错误条
      + 确认框：仍要退出 = :meth:`FileSession.abandon` 后 exit 3 / 取消 =
      留在应用）；
    - ``'paused'``：该文件暂停自动提交，``failures`` 列出全部滞留暂态。
    """

    status: Literal["clean", "conflict", "failed", "paused"]
    committed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class OpenResult:
    """:meth:`SessionManager.open_files` 的单文件结果。

    ``session=None`` 且 ``diagnostics`` 含 error 级诊断 → 打开失败
    （部分失败不阻断其他文件，§3.10）。
    """

    path: Path
    session: "FileSession | None"
    diagnostics: list[Diagnostic] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 冲突暂存（§7.6：待写字节 + 未完成的栈操作现场）
# ---------------------------------------------------------------------------


@dataclass
class _ConflictInfo:
    """一次撞冲突的提交的完整现场，供 resolve_conflict('force') 续写。"""

    item_path: str
    old_value: Any
    new_value: Any
    payload: bytes                      # 待写字节（已含 BOM 补偿）
    entry: UndoEntry                    # 成功后入栈的条目
    push_to: Literal["undo", "redo"]    # 成功后 entry 推入哪个栈
    from_pending: bool                  # True=普通提交（成功后清暂态+redo）


# ---------------------------------------------------------------------------
# FileSession
# ---------------------------------------------------------------------------


class FileSession:
    """单个已打开文件的编辑会话（§3.10：每文件一个 ConfigDoc，写回独立）。

    构造参数：

    - ``path``：文件路径；``adapter``：load/save 所属适配器；
    - ``doc``：``adapter.load(path)`` 的产物（locator 有效方可提交）；
    - ``diagnostics``：加载期诊断（以适配器 load() 产出为准，裁量 11）；
    - ``poll_interval``：§7.6 轮询周期（默认 2s，CLI --poll-interval）；
    - ``on_state_change``：外部状态事件回调，事件 ∈
      ``'external_modified'``（磁盘被外部修改）/ ``'gone'``（不可读，条目
      已转只读态）/ ``'recovered'``（gone 后恢复可读且与基线一致）。
      **回调可能来自轮询线程或主动探测调用栈，UI 层必须自行调度到主线程**。

    poller 不在构造时自动启动：由 :meth:`start_poller` /
    :meth:`SessionManager.start_pollers` 显式启动（测试可控）。
    """

    def __init__(
        self,
        path: Path,
        adapter: Any,
        doc: ConfigDoc,
        diagnostics: list[Diagnostic],
        poll_interval: float = 2.0,
        on_state_change: Callable[[str], None] | None = None,
    ) -> None:
        self._path = Path(path)
        self._adapter = adapter
        self._doc = doc
        self._diagnostics = list(diagnostics)
        self._on_state_change = on_state_change
        self._lock = threading.RLock()

        self._pending: dict[str, Any] = {}
        self._last_blocked: dict[str, str] = {}
        self._undo: list[UndoEntry] = []
        self._redo: list[UndoEntry] = []

        self._paused = False
        self._gone_readonly = False
        self._conflict: _ConflictInfo | None = None
        self._closed = False

        self._poller = ExternalChangePoller(
            self._path,
            doc.content_hash,
            interval=poll_interval,
            on_change=self._on_poller_change,
        )

    # -- 只读属性 -----------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def adapter(self) -> Any:
        return self._adapter

    @property
    def doc(self) -> ConfigDoc:
        """当前文档（每次成功提交后为重新 load 的产物，架构裁决）。"""
        return self._doc

    @property
    def diagnostics(self) -> list[Diagnostic]:
        return list(self._diagnostics)

    @property
    def paused(self) -> bool:
        """是否暂停自动提交（§7.6 用户关闭冲突模态后）。"""
        return self._paused

    @property
    def gone_readonly(self) -> bool:
        """文件不可读 → 全部条目只读态（§7.6）。"""
        return self._gone_readonly

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def poller(self) -> ExternalChangePoller:
        return self._poller

    def pending_items(self) -> dict[str, Any]:
        """当前暂态快照（path → 门控规范化新值）。"""
        with self._lock:
            return dict(self._pending)

    def last_blocked_items(self) -> dict[str, str]:
        """曾尝试 set 被拦截的 (path → reason) 快照（裁量 2）。

        供退出 flush 时 UI 列出"本就不会落盘、退出即丢弃"的条目（§7.7）；
        同 path 后续成功进入暂态时自动清除。
        """
        with self._lock:
            return dict(self._last_blocked)

    def undo_depth(self) -> int:
        with self._lock:
            return len(self._undo)

    def redo_depth(self) -> int:
        with self._lock:
            return len(self._redo)

    # -- §7.5 第 1–2 步：输入 → 暂态 + 门控 ----------------------------------

    def set_pending_text(self, item_path: str, text: str) -> PendingResult:
        """文本类控件输入进入暂态（int/float/str；bool 用 set_pending_bool）。

        流程：gone 恢复探测 → parse_text_input → 中间态静默（不进暂态、不清
        旧暂态）/ 确定性非法清该 path 暂态 + 红标 → gate_edit → block 不入
        暂态 + 记 last_blocked / warn|ok 入暂态（值为 GateResult.value
        规范化值）。未知 path → ValueError（程序错误，裁量 6）。
        """
        with self._lock:
            item = self._resolve_item(item_path)
            self._probe_recovered_locked()
            if self._gone_readonly:
                return PendingResult(
                    accepted=False, level="block",
                    reason="文件当前不可读（已删除/移动/权限变更），"
                           "全部条目只读，不接受编辑（§7.6）。",
                )
            parsed = parse_text_input(item, text)
            if not parsed.ok:
                if parsed.intermediate:
                    # 中间态：不进暂态、不清旧暂态、不红标（裁量 1，§10 U-6）
                    return PendingResult(
                        accepted=False, level="intermediate",
                        reason=parsed.error,
                    )
                return self._reject(item_path, "invalid", parsed.error)
            return self._gate_and_store(item, parsed.value)

    def set_pending_bool(self, item_path: str, checked: bool) -> PendingResult:
        """点击类（bool）控件输入进入暂态（值变化即提交语义由 UI 触发）。"""
        with self._lock:
            item = self._resolve_item(item_path)
            self._probe_recovered_locked()
            if self._gone_readonly:
                return PendingResult(
                    accepted=False, level="block",
                    reason="文件当前不可读（已删除/移动/权限变更），"
                           "全部条目只读，不接受编辑（§7.6）。",
                )
            parsed = parse_bool_input(item, checked)
            if not parsed.ok:
                return self._reject(item_path, "invalid", parsed.error)
            return self._gate_and_store(item, parsed.value)

    def _gate_and_store(self, item: Any, value: Any) -> PendingResult:
        gated: GateResult = gate_edit(item, value)
        if gated.level == "block":
            return self._reject(item.path, "block", gated.reason)
        self._pending[item.path] = gated.value
        self._last_blocked.pop(item.path, None)  # 裁量 2：成功即清
        return PendingResult(
            accepted=True, level=gated.level, reason=gated.reason,
            value=gated.value,
        )

    def _reject(
        self, item_path: str, level: Literal["invalid", "block"],
        reason: str | None,
    ) -> PendingResult:
        """确定性非法 / 门控拦截：清除该 path 旧暂态 + 记 last_blocked。

        裁量 1：控件真相模型——非法输入即控件当前真相，清除旧暂态防止失焦
        提交陈旧值；裁量 2：记入 last_blocked 供退出清单。
        """
        self._pending.pop(item_path, None)
        if reason is not None:
            self._last_blocked[item_path] = reason
        return PendingResult(accepted=False, level=level, reason=reason)

    def get_pending(self, item_path: str) -> Any:
        """该 path 的暂态值；无暂态返回 None。未知 path → ValueError。"""
        with self._lock:
            self._ensure_open()
            self._require_known(item_path)
            return self._pending.get(item_path)

    def has_pending(self, item_path: str) -> bool:
        with self._lock:
            self._ensure_open()
            self._require_known(item_path)
            return item_path in self._pending

    def clear_pending(self, item_path: str) -> None:
        """清除该 path 暂态（如 UI 放弃键入回显）。未知 path → ValueError。"""
        with self._lock:
            self._ensure_open()
            self._require_known(item_path)
            self._pending.pop(item_path, None)

    # -- §7.5 第 4 步 + §7.6：提交 -------------------------------------------

    def commit_pending(self, item_path: str) -> CommitResult:
        """提交该 path 暂态（防抖到期 / 失焦 / 点击类即时，由 UI 触发）。

        无暂态 → ``'noop'``；paused → ``'paused'``（裁量 5）；流程 =
        EditOp(path, 门控规范化值) → adapter.save → BOM 补偿（save 产出
        BOM-less 字节，has_bom 按基线 original_bytes 判定）→ §7.6 基线校验
        （MODIFIED → ``'conflict'`` 不写盘；GONE → ``'gone'``）→
        :func:`atomic_write` → 成功后**重新 load** 并走提交后流程
        （架构裁决）→ ``'committed'``（附新旧值）。WriteError → ``'failed'``，
        暂态保留可重试。未知 path → ValueError（裁量 6）。
        """
        with self._lock:
            self._ensure_open()
            self._require_known(item_path)
            if self._paused:
                return CommitResult(
                    status="paused", path=item_path,
                    reason="该文件已暂停自动提交（冲突未决），"
                           "请先处置冲突（resolve_conflict）或恢复。",
                )
            if item_path not in self._pending:
                return CommitResult(status="noop", path=item_path)
            new_value = self._pending[item_path]
            old_value = self._item_value(item_path)
            return self._run_commit(
                item_path, old_value, new_value,
                entry=UndoEntry(item_path, old_value, new_value),
                push_to="undo", from_pending=True,
            )

    def _run_commit(
        self,
        item_path: str,
        old_value: Any,
        new_value: Any,
        entry: UndoEntry,
        push_to: Literal["undo", "redo"],
        from_pending: bool,
    ) -> CommitResult:
        """完整提交流程（调用方须持锁）。commit / undo / redo 共用。"""
        # gone 态：先主动恢复探测（poller GONE→UNCHANGED 不回调）
        if self._gone_readonly and not self._probe_recovered_locked():
            return CommitResult(
                status="gone", path=item_path,
                old_value=old_value, new_value=new_value,
                reason="文件当前不可读（已删除/移动/权限变更），无法提交。",
            )
        try:
            payload = self._adapter.save(self._doc, [EditOp(item_path, new_value)])
        except (ValueError, RuntimeError) as exc:
            # 契约上不可达（门控已拦 readonly/未知 path）；防御性转 failed
            return CommitResult(
                status="failed", path=item_path,
                old_value=old_value, new_value=new_value,
                reason=f"生成写回字节失败：{exc}",
            )
        payload = restore_bom(
            payload, self._doc.original_bytes.startswith(UTF8_BOM)
        )

        # §7.6 提交前基线校验（竞态兜底；undo/redo 同样适用）
        state = check_once(self._path, self._doc.content_hash)
        if state is FileState.MODIFIED:
            self._conflict = _ConflictInfo(
                item_path=item_path, old_value=old_value, new_value=new_value,
                payload=payload, entry=entry, push_to=push_to,
                from_pending=from_pending,
            )
            return CommitResult(
                status="conflict", path=item_path,
                old_value=old_value, new_value=new_value,
                reason="文件已在磁盘上被外部修改，与会话基线不一致；"
                       "请选择「重新加载」或「强制覆盖」（§7.6）。",
            )
        if state is FileState.GONE:
            self._gone_readonly = True
            self._fire("gone")
            return CommitResult(
                status="gone", path=item_path,
                old_value=old_value, new_value=new_value,
                reason="文件不可读（已删除/移动/权限变更），条目已转只读态。",
            )

        # §7.4 原子写
        try:
            atomic_write(self._path, payload)
        except WriteError as exc:
            return CommitResult(
                status="failed", path=item_path,
                old_value=old_value, new_value=new_value,
                reason=f"写盘失败：{exc}",
            )
        return self._post_commit(item_path, old_value, new_value, entry,
                                 push_to, from_pending, payload=payload)

    def _post_commit(
        self,
        item_path: str,
        old_value: Any,
        new_value: Any,
        entry: UndoEntry,
        push_to: Literal["undo", "redo"],
        from_pending: bool,
        payload: bytes,
    ) -> CommitResult:
        """写盘成功后的统一流程（调用方须持锁）：基线 → 重载 → 栈 → 清暂态。

        架构裁决：save 的 doc 必须是 load() 产物且 locator 有效；提交后基线
        字节已变、旧 locator 失效，故必须重新 load 替换 doc、以新 doc 的
        original_bytes/content_hash 为新基线并同步 poller。重载失败 →
        按 GONE 处置。undo 栈条目按 path 跨重载稳定。

        K-9（验收后修复）：进入本函数即以**刚落盘字节**的哈希先行刷新
        poller 基线（调用方须持锁，刷新与写盘在同一临界区内），再执行
        reload——否则「atomic_write 之后、下方 update_baseline 之前」的
        窗口内，poller 轮询会以旧基线读到新字节并边沿误触发
        external_modified（实测：提交后 2~3 秒冒误报横幅）。回调侧另有
        check_once 复核兜底，双保险。reload 失败按 GONE 处置时该基线值
        无意义，不影响既有语义。
        """
        self._poller.update_baseline(compute_hash(payload))
        try:
            new_doc, new_diags = self._adapter.load(self._path)
        except OSError as exc:
            self._gone_readonly = True
            self._fire("gone")
            return CommitResult(
                status="gone", path=item_path,
                old_value=old_value, new_value=new_value,
                reason=f"写盘成功但重载失败（按不可读处置）：{exc}",
            )
        self._doc = new_doc
        self._diagnostics = list(new_diags)  # 裁量 11：以适配器产出为准
        self._poller.update_baseline(new_doc.content_hash)

        # undo/redo 栈维护（§7.7）。冲突后 force 续写的场景：条目曾按裁量 4
        # 放回原栈，此处按身份从对侧栈移除，保证不重复。
        if push_to == "undo":
            if from_pending:
                self._redo.clear()  # 任一新提交落盘后 redo 清空
            else:
                self._remove_by_identity(self._redo, entry)
            self._undo.append(entry)
            while len(self._undo) > UNDO_LIMIT:
                self._undo.pop(0)  # 超出上限淘汰最旧（v0.4 决议）
        else:
            self._remove_by_identity(self._undo, entry)
            self._redo.append(entry)

        if from_pending:
            self._pending.pop(item_path, None)
        self._conflict = None
        return CommitResult(status="committed", path=item_path,
                            old_value=old_value, new_value=new_value)

    # -- §7.6：冲突处置与暂停 -------------------------------------------------

    def resolve_conflict(self, choice: Literal["reload", "force"]) -> CommitResult:
        """冲突模态抉择（§7.6）。

        - ``'reload'``（默认）：丢弃**全部**暂态 + 重新 load + 新基线 +
          undo/redo 清空（旧栈条目 locator 对新文档无保证，§7.6 原文）+
          恢复自动提交；成功 → ``'reloaded'``（裁量 3）。重载失败 →
          按 GONE 处置。
        - ``'force'``（须显式选择，UI 二次警示将永久丢失外部修改且不可
          undo 找回）：以暂存待写字节**跳过基线校验**直接原子写，成功走
          正常提交后流程 → ``'committed'``；无待写字节 → ``'failed'``
          并引导走 reload。
        """
        with self._lock:
            self._ensure_open()
            if choice == "reload":
                self._pending.clear()
                self._conflict = None
                try:
                    new_doc, new_diags = self._adapter.load(self._path)
                except OSError as exc:
                    self._gone_readonly = True
                    self._undo.clear()
                    self._redo.clear()
                    self._paused = False
                    self._fire("gone")
                    return CommitResult(
                        status="gone",
                        reason=f"重新加载失败（按不可读处置）：{exc}",
                    )
                self._doc = new_doc
                self._diagnostics = list(new_diags)
                self._undo.clear()
                self._redo.clear()
                self._paused = False
                self._gone_readonly = False
                self._poller.update_baseline(new_doc.content_hash)
                return CommitResult(
                    status="reloaded",
                    reason="已丢弃暂态并按磁盘内容重新加载，"
                           "undo/redo 栈已清空（§7.6）。",
                )
            if choice == "force":
                info = self._conflict
                if info is None:
                    return CommitResult(
                        status="failed",
                        reason="当前没有待写字节（无未决冲突现场），无法"
                               "强制覆盖；请改选「重新加载」（reload）。",
                    )
                self._paused = False  # force 是显式抉择，视为恢复自动提交
                try:
                    atomic_write(self._path, info.payload)
                except WriteError as exc:
                    return CommitResult(
                        status="failed", path=info.item_path,
                        old_value=info.old_value, new_value=info.new_value,
                        reason=f"强制覆盖写盘失败（可重试）：{exc}",
                    )
                result = self._post_commit(
                    info.item_path, info.old_value, info.new_value,
                    info.entry, info.push_to, info.from_pending,
                    payload=info.payload,
                )
                self._paused = False
                return result
            raise ValueError(
                f"choice 必须是 'reload' 或 'force'，收到 {choice!r}"
            )

    def pause_auto_commit(self) -> None:
        """暂停该文件自动提交（§7.6 用户关闭冲突模态暂不处理）。

        暂停期间 commit / undo / redo 一律返回 ``'paused'``（裁量 5），
        暂态保留，待用户重试提交或重载。
        """
        with self._lock:
            self._ensure_open()
            self._paused = True

    def resume_auto_commit(self) -> None:
        """显式恢复自动提交（不触发补提交，重试由 UI 发起）。"""
        with self._lock:
            self._ensure_open()
            self._paused = False

    # -- §7.7：undo / redo ----------------------------------------------------

    def undo(self) -> CommitResult:
        """撤销最近一次已提交编辑：反向 EditOp 走完整提交流程（§7.7）。

        栈空 → ``'noop'``；paused → ``'paused'``；失败/冲突/gone 时弹出条目
        放回 undo 栈（裁量 4，可重试、不破坏栈一致性）；成功后条目推入
        redo 栈。落盘同样过 §7.6 基线校验（冲突现场暂存，force 可续写）。
        """
        with self._lock:
            self._ensure_open()
            if self._paused:
                return CommitResult(
                    status="paused",
                    reason="该文件已暂停自动提交（冲突未决）。",
                )
            if not self._undo:
                return CommitResult(status="noop", reason="undo 栈为空。")
            entry = self._undo.pop()
            result = self._run_commit(
                entry.path, entry.new_value, entry.old_value,
                entry=entry, push_to="redo", from_pending=False,
            )
            if result.status != "committed":
                self._undo.append(entry)  # 裁量 4：放回原栈可重试
            return result

    def redo(self) -> CommitResult:
        """重做最近一次撤销：正向 EditOp 走完整提交流程（§7.7）。

        栈空 → ``'noop'``；paused → ``'paused'``；失败/冲突/gone 时条目放回
        redo 栈；成功后推回 undo 栈（受 :data:`UNDO_LIMIT` 上限约束）。
        """
        with self._lock:
            self._ensure_open()
            if self._paused:
                return CommitResult(
                    status="paused",
                    reason="该文件已暂停自动提交（冲突未决）。",
                )
            if not self._redo:
                return CommitResult(status="noop", reason="redo 栈为空。")
            entry = self._redo.pop()
            result = self._run_commit(
                entry.path, entry.old_value, entry.new_value,
                entry=entry, push_to="undo", from_pending=False,
            )
            if result.status != "committed":
                self._redo.append(entry)  # 裁量 4：放回原栈可重试
            return result

    # -- §7.7：关闭 / flush / abandon -----------------------------------------

    def close(self, discard_illegal: bool = False) -> CloseResult:
        """关闭文件（§7.7；入口 §10 U-12）。

        有暂态先按失焦语义逐条提交；``discard_illegal=True`` = 用户确认
        "放弃暂态并关闭"：不再尝试提交，直接丢弃全部暂态并关闭。

        - gone 且有暂态 → ``'need_confirm'``（裁量 8：暂态无法落盘）；
        - 撞冲突 → ``'conflict'``，抉择完成前不关闭；
        - 提交失败/暂停 → ``'failed'``，阻止关闭，可重试或 discard；
        - 成功 → 停 poller，释放基线/暂态/undo/redo/诊断 → ``'closed'``。
        """
        with self._lock:
            self._ensure_open()
            if discard_illegal:
                self._release()
                return CloseResult(status="closed",
                                   reason="已放弃未提交暂态并关闭。")
            if self._gone_readonly and self._pending:
                self._probe_recovered_locked()
            if self._gone_readonly and self._pending:
                return CloseResult(
                    status="need_confirm",
                    items=[(p, "文件不可读（已删除/移动/权限变更），"
                               "暂态无法落盘") for p in self._pending],
                    reason="文件不可读且存在未提交暂态：放弃暂态并关闭，"
                           "或取消关闭（§7.6/§7.7，裁量 8）。",
                )
            failures: list[tuple[str, str]] = []
            for item_path in list(self._pending):
                result = self.commit_pending(item_path)
                if result.status == "conflict":
                    return CloseResult(
                        status="conflict",
                        items=[(item_path, result.reason or "基线冲突")],
                        reason="关闭触发的提交撞基线冲突，抉择完成前不关闭"
                               "（§7.6）。",
                    )
                if result.status == "gone":
                    return CloseResult(
                        status="need_confirm",
                        items=[(p, result.reason or "文件不可读")
                               for p in self._pending],
                        reason="关闭期间文件转为不可读：放弃暂态并关闭，"
                               "或取消关闭。",
                    )
                if result.status in ("failed", "paused"):
                    failures.append(
                        (item_path, result.reason or result.status)
                    )
            if failures:
                return CloseResult(
                    status="failed", items=failures,
                    reason="关闭触发的提交失败：可重试，或放弃暂态并关闭"
                           "（close(discard_illegal=True)）。",
                )
            self._release()
            return CloseResult(status="closed")

    def flush(self) -> FlushResult:
        """退出前 flush（§7.7）：对所有滞留合法暂态立即逐条提交。

        逐文件处理异常：撞冲突 → ``'conflict'`` + 清单（UI 弹 §7.6 模态，
        含"取消退出"）；提交失败 → ``'failed'`` + 清单（UI 错误条 + 确认框：
        仍要退出 = :meth:`abandon` 后 exit 3 / 取消 = 留在应用）。
        """
        with self._lock:
            self._ensure_open()
            result = FlushResult(status="clean")
            if self._paused:
                result.status = "paused"
                result.failures = [
                    (p, "该文件已暂停自动提交（冲突未决）")
                    for p in self._pending
                ]
                return result
            for item_path in list(self._pending):
                r = self.commit_pending(item_path)
                if r.status == "committed":
                    result.committed.append(item_path)
                elif r.status == "conflict":
                    result.conflicts.append(item_path)
                elif r.status in ("failed", "gone", "paused"):
                    result.failures.append((item_path, r.reason or r.status))
            if result.conflicts:
                result.status = "conflict"
            elif result.failures:
                result.status = "failed"
            return result

    def abandon(self) -> None:
        """丢弃全部未提交暂态（§7.7 "仍要退出" = exit 3 语义由上层落地）。

        :meth:`last_blocked_items` 不清除——UI 退出时据此列出"本就非法、
        不会落盘"的条目清单（裁量 2）。
        """
        with self._lock:
            self._ensure_open()
            self._pending.clear()

    # -- poller 接线（§7.6） ---------------------------------------------------

    def start_poller(self) -> None:
        """启动外部修改轮询（幂等）。"""
        self._poller.start()

    def stop_poller(self) -> None:
        """停止轮询（幂等）。"""
        self._poller.stop()

    def set_on_state_change(self, cb: Callable[[str], None] | None) -> None:
        """公开（重）绑外部状态事件回调（G2c 正规化）。

        语义与构造参数 ``on_state_change`` 完全一致（事件 ∈
        'external_modified'/'gone'/'recovered'）；**回调可能来自轮询线程
        或主动探测调用栈，UI 层必须自行调度到主线程**（裁量 10）。传
        ``None`` 解绑。取代 GUI 早期写 ``_on_state_change`` 私有属性的
        过渡 hack——``_fire`` 每次触发现读该属性，运行期重绑安全。
        """
        self._on_state_change = cb

    def _on_poller_change(self, state: FileState) -> None:
        """poller 回调（轮询线程）：只置标志 + 转发（裁量 10）。

        不在回调里持锁重载文档——UI 收到事件后自行决定处置（弹横幅/模态），
        并注意回调线程不是主线程。

        K-9 竞态复核（验收后修复）：自身提交在「atomic_write 落盘 → 基线
        更新」窗口内，poller 可能以旧基线读到本会话刚落盘的新字节，边沿
        误判 MODIFIED（实测表现为提交后 2~3 秒冒出"文件已在磁盘上被修改"
        横幅）。故收到 MODIFIED 先**短暂持锁**按当前文档基线
        :func:`check_once` 复核：

        - 磁盘 == 当前基线 → 判定为自身提交回声，忽略该次事件并复位
          poller 边沿（否则被忽略的回声会占住 MODIFIED 边沿，紧随其后
          的真实外部修改将被吞掉）；
        - 磁盘 != 当前基线 → 真实外部修改，照常转发（G2b 外部修改检测
          语义不变）。

        锁注：回调线程持 session 锁仅覆盖一次读盘比对（微秒级）；与提交
        路径串行化后复核用的 doc 哈希与磁盘内容才是同一致快照（不持锁时
        commit 在途、doc 尚未换新，复核仍会撞到旧基线而误报）。锁序恒为
        session 锁 → poller 锁（update_baseline/reset_edge），无死锁；
        close() 持锁 join 轮询线程的最坏情形是 timer 超时的有界等待。
        GONE 无"自身提交"来源，不复核（最小修复）。
        """
        if state == FileState.MODIFIED:
            with self._lock:
                unchanged = check_once(
                    self._path, self._doc.content_hash
                ) is FileState.UNCHANGED
                if unchanged:
                    self._poller.reset_edge()
                    return
            self._gone_readonly = False  # 可读且内容变化 → 解除 gone 只读态
            self._fire("external_modified")
        elif state == FileState.GONE:
            self._gone_readonly = True
            self._fire("gone")
        # UNCHANGED：poller 边沿触发不回调（只复位），无需处理

    def _fire(self, event: str) -> None:
        cb = self._on_state_change
        if cb is not None:
            try:
                cb(event)
            except Exception:
                pass  # 回调异常不得破坏 session 状态（与 poller 同策略）

    def _probe_recovered_locked(self) -> bool:
        """gone 态主动恢复探测（调用方须持锁）。返回是否可读可编辑。

        关键事实：poller 边沿触发，GONE→UNCHANGED 恢复**不回调**（只复位
        边沿），故 set_pending / commit / close 入口对 gone 态主动
        :func:`check_once`：UNCHANGED → 解除只读态 + 'recovered' 事件；
        MODIFIED → 解除只读态 + 'external_modified'（提交时基线校验会撞
        冲突，由 §7.6 流程接管）；仍 GONE → 保持只读态。
        """
        if not self._gone_readonly:
            return True
        state = check_once(self._path, self._doc.content_hash)
        if state is FileState.GONE:
            return False
        self._gone_readonly = False
        if state is FileState.MODIFIED:
            self._fire("external_modified")
        else:
            self._fire("recovered")
        return True

    # -- 内部工具 --------------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError(f"会话已关闭：{self._path}")

    def _require_known(self, item_path: str) -> None:
        if not any(it.path == item_path for it in self._doc.items):
            raise ValueError(
                f"未知条目 path {item_path!r}（{self._path}），"
                "属程序错误（§3.8，裁量 6）"
            )

    def _resolve_item(self, item_path: str) -> Any:
        self._ensure_open()
        for it in self._doc.items:
            if it.path == item_path:
                return it
        raise ValueError(
            f"未知条目 path {item_path!r}（{self._path}），"
            "属程序错误（§3.8，裁量 6）"
        )

    def _item_value(self, item_path: str) -> Any:
        for it in self._doc.items:
            if it.path == item_path:
                return it.value
        return None

    def _release(self) -> None:
        """关闭释放（§7.7）：停 poller，清暂态/last_blocked/栈/诊断/冲突现场。

        调用方须持锁。基线随 doc 引用一并交由 GC（doc 属性仍可读取最后
        状态供日志，但会话已 closed，任何变更操作抛 RuntimeError）。
        """
        self._closed = True
        self._poller.stop()
        self._pending.clear()
        self._last_blocked.clear()
        self._undo.clear()
        self._redo.clear()
        self._diagnostics = []
        self._conflict = None

    @staticmethod
    def _remove_by_identity(stack: list[UndoEntry], entry: UndoEntry) -> None:
        for i, e in enumerate(stack):
            if e is entry:
                del stack[i]
                return


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class SessionManager:
    """多文件会话管理（§3.10 / §10 U-2）：打开、索引、统一 flush / 停轮询。

    - 按 ``Path.resolve()`` 键控；重复打开同一文件返回已有 session（裁量 9，
      每文件一个 doc）；
    - 一个文件打开失败**不阻断**其他文件（§3.10），失败以 error 级诊断表达
      （文件不可读 = 扩展码 :data:`CODE_E_READ`，裁量 7）；
    - poller 不随打开自动启动：由 :meth:`start_pollers` 统一启动。
    """

    def __init__(self) -> None:
        self._sessions: dict[Path, FileSession] = {}
        self._lock = threading.Lock()
        # CLI↔GUI 接缝（G2c，任务书登记）：CLI 启动批次的完整 OpenResult
        # 列表（含失败文件，session=None + 诊断）由 cli setattr；GUI 侧
        # （GuiState）启动时读取一次以登记侧栏诊断态（U-10）。由调用方
        # 维护，open_files 不自动写入（避免覆盖语义歧义）。
        self.last_open_results: list[OpenResult] = []

    # -- 打开 -----------------------------------------------------------------

    def open_files(
        self,
        paths: list[Path | str],
        format_hint: str = "auto",
        registry: Registry | None = None,
        poll_interval: float = 2.0,
        on_state_change: Callable[[str], None] | None = None,
    ) -> list[OpenResult]:
        """批量打开文件，返回与 ``paths`` 等长的结果列表（顺序一致）。

        ``registry=None`` 时内联注册 ``[PythonAdapter, YamlAdapter]``
        （关键事实：registry.default_registry() 当前返回空注册表，3c 才
        接线；导入失败防御性跳过——裁量登记）。``on_state_change`` 透传给
        每个新建 FileSession。
        """
        if registry is None:
            registry = self._inline_registry()
        results: list[OpenResult] = []
        for raw in paths:
            results.append(
                self._open_one(Path(raw), format_hint, registry,
                               poll_interval, on_state_change)
            )
        return results

    @staticmethod
    def _inline_registry() -> Registry:
        reg = Registry()
        try:
            from ..adapters.python_adapter import PythonAdapter
            reg.register(PythonAdapter())
        except Exception:
            pass  # 防御：python 适配器不可用（依赖缺失等）不阻断 yaml
        try:
            from ..adapters.yaml_adapter import YamlAdapter
            reg.register(YamlAdapter())
        except Exception:
            pass
        return reg

    def _open_one(
        self,
        path: Path,
        format_hint: str,
        registry: Registry,
        poll_interval: float,
        on_state_change: Callable[[str], None] | None,
    ) -> OpenResult:
        resolved = path.resolve()
        with self._lock:
            existing = self._sessions.get(resolved)
        if existing is not None:
            # 裁量 9：重复打开同一文件 → 返回已有 session
            return OpenResult(path=resolved, session=existing)

        # 可读性预检（裁量 7：E-READ）；顺带取嗅探 head 字节
        try:
            head = path.read_bytes()
        except OSError as exc:
            return OpenResult(
                path=resolved, session=None,
                diagnostics=[Diagnostic(
                    severity=SEV_ERROR, code=CODE_E_READ,
                    message=f"无法读取文件 {path}：{exc}",
                )],
            )

        adapter, diags = registry.select_adapter(path, head, format_hint)
        if adapter is None:
            return OpenResult(path=resolved, session=None, diagnostics=diags)

        try:
            doc, load_diags = adapter.load(path)
        except Exception as exc:  # 契约：load 对可读输入不抛；防御性兜底
            diags = list(diags) + [Diagnostic(
                severity=SEV_ERROR, code=CODE_E_PARSE,
                message=f"加载 {path} 失败：{exc}",
            )]
            return OpenResult(path=resolved, session=None, diagnostics=diags)

        all_diags = list(diags) + list(load_diags)
        if any(d.severity == SEV_ERROR for d in all_diags):
            return OpenResult(path=resolved, session=None,
                              diagnostics=all_diags)

        session = FileSession(
            resolved, adapter, doc, all_diags,
            poll_interval=poll_interval, on_state_change=on_state_change,
        )
        with self._lock:
            # 竞态兜底：并发打开同一路径时保留先到者
            race = self._sessions.get(resolved)
            if race is not None:
                return OpenResult(path=resolved, session=race)
            self._sessions[resolved] = session
        return OpenResult(path=resolved, session=session,
                          diagnostics=all_diags)

    # -- 索引与维护 ------------------------------------------------------------

    @property
    def sessions(self) -> dict[Path, FileSession]:
        """当前全部会话快照（resolve 后路径 → session）。"""
        with self._lock:
            return dict(self._sessions)

    def get(self, path: Path | str) -> FileSession | None:
        with self._lock:
            return self._sessions.get(Path(path).resolve())

    def close_file(self, path: Path | str,
                   discard_illegal: bool = False) -> CloseResult:
        """关闭单个文件：成功才从索引移除；未决（冲突/失败/需确认）保留。"""
        resolved = Path(path).resolve()
        with self._lock:
            session = self._sessions.get(resolved)
        if session is None:
            return CloseResult(status="closed",
                               reason=f"{resolved} 未在会话中（无需关闭）。")
        result = session.close(discard_illegal=discard_illegal)
        if result.status == "closed":
            with self._lock:
                self._sessions.pop(resolved, None)
        return result

    def flush_all(self) -> dict[Path, FlushResult]:
        """退出前逐文件 flush（§7.7）；一个文件异常不阻断其他文件。"""
        out: dict[Path, FlushResult] = {}
        for resolved, session in self.sessions.items():
            out[resolved] = session.flush()
        return out

    def start_pollers(self) -> None:
        """启动全部会话的外部修改轮询（幂等）。"""
        for session in self.sessions.values():
            session.start_poller()

    def stop_all(self) -> None:
        """停止全部轮询线程（不关闭会话；退出清理用）。"""
        for session in self.sessions.values():
            session.stop_poller()
