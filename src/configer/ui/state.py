"""GUI 会话状态（无 UI 框架依赖，可无头测试）。

:class:`GuiState` 是 UI 层的单一事实源：活动文件、选中条目、搜索词、侧栏
可见性、加载失败文件登记、外部事件队列（线程安全）。UI 组件（layout /
sidebar / items / detail）只经此对象读写状态；状态变更后调用 :meth:`touch`
触发界面刷新（由 layout 注入 ``ui_refresh`` 回调，本模块不 import nicegui）。

线程模型（裁量登记，见 app.py docstring）：``FileSession.on_state_change``
可能来自轮询线程 → 只投递 :data:`GuiState.events`（``queue.Queue``，线程
安全）；``ui.timer`` 在主循环 drain 并刷新。

**关键坑（§7.5 提交后重载）**：commit/undo/redo/reload 后 ``session.doc``
整体替换，旧 ConfigItem 对象引用失效——UI 只存 ``selected_item``（path 字
符串），渲染时一律按 path 从当前 doc 重取（:meth:`selected_config_item`）。
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..core.session import CommitResult, FileSession, OpenResult, PendingResult, SessionManager
from ..model import ConfigItem, Diagnostic
from .commit import CommitAction, DebounceManager, GateFeedback, commit_action, pending_feedback
from .dialogs import (
    BANNER_RECOVERED,
    PAUSED_ERROR_TEXT,
    RELOAD_DONE_NOTIFY,
    ConflictRequest,
    ExitPlan,
    apply_external_event,
    close_plan,
    flush_exit_plan,
    noninteractive_exit_code,
)
from .logic import diagnostics_level

__all__ = ["FileEntry", "CloseConfirm", "ExitConfirm", "GuiState"]

# 外部事件（on_state_change）合法取值（core/poller 边沿触发）。
EXTERNAL_EVENTS = ("external_modified", "gone", "recovered")


@dataclass
class FileEntry:
    """侧栏一个文件项的展示模型（U-2）。"""

    path: Path
    name: str
    fmt: str | None            # 'python'/'yaml'；加载失败可能为 None
    level: str                 # 'error'|'warning'|'ok'（logic.diagnostics_level）
    loaded: bool               # False = 加载失败（U-10）
    diagnostics: list[Diagnostic] = field(default_factory=list)
    gone: bool = False         # G2b：文件不可读只读态（§7.6，侧栏标识）


@dataclass
class CloseConfirm:
    """U-12 关闭确认框的待决数据（modals 层据此渲染）。

    ``kind='need_confirm'``：gone 且有暂态 → 【放弃暂态并关闭】/【取消关闭】；
    ``kind='failed'``：关闭触发的提交失败 → 【重试】/【放弃暂态并关闭】/【取消关闭】。
    """

    file: Path
    kind: str                  # 'need_confirm' | 'failed'
    lines: tuple[str, ...] = ()


@dataclass
class ExitConfirm:
    """§7.7 退出确认框的待决数据。

    ``kind='failed'``：提交失败 → 【仍要退出】（abandon + exit 3）/【取消】；
    ``kind='blocked'``：全 clean 但有 block 级非法暂态 → 列出条目 +
    【退出】/【取消】（退出码仍 0，裁量见 dialogs.ExitPlan）。
    """

    kind: str                  # 'failed' | 'blocked'
    lines: tuple[str, ...] = ()
    files: tuple[Path, ...] = ()   # kind='failed'：仍要退出时 abandon 的文件


class GuiState:
    """UI 状态容器 + G2 钩子宿主。"""

    def __init__(self, manager: SessionManager, poll_interval: float = 2.0) -> None:
        self.manager = manager
        self.poll_interval = poll_interval

        # 文件登记：打开顺序 + 失败项（manager.sessions 只含成功者）。
        self._open_order: list[Path] = []
        self._failed: dict[Path, list[Diagnostic]] = {}
        # 已绑定 per-path 回调的会话登记（path → session 对象；G2c 公开 API
        # 正规化——ensure_state_callbacks 据此补绑，不再读私有属性）。
        self._bound: dict[Path, FileSession] = {}
        self._lock = threading.Lock()   # 保护上面三者与 events

        # U-10 接缝：CLI 启动批次的完整 OpenResult（含失败文件）由 cli
        # setattr 到 manager.last_open_results；GUI 启动即登记（失败文件进
        # 侧栏诊断态）。防御 getattr：manager 无该属性（旧测试替身）时跳过。
        startup = getattr(manager, "last_open_results", None)
        if startup:
            self.register_results(list(startup))

        self.active_path: Path | None = None
        self.selected_item: str | None = None   # 活动文件内条目 path
        self.search_query: str = ""
        # 条目树展开态持久化（R2，纯新增属性）：组/子组键集合。items 面板
        # 重建（refreshable）时把它作为 ui.expansion 初始值、并在用户开合时
        # 更新，导航上下文不再随刷新丢失；键由 items 侧按文件作用域生成，
        # 本类只存不解释。R3：layout 的"文件说明"折叠区复用同一集合
        # （键 = path::__file_doc__，见 layout.file_header_view）。
        self.open_groups: set[str] = set()
        # R3/N5：最近一次"选中即自动展开所在组"的条目 path——同一条目在
        # 后续刷新重建时不再强制展开（用户手动收起的组保持收起）；选中变化
        # 或切换/关闭文件时清空（select_item / activate / _after_closed）。
        self.auto_expanded_item: str | None = None
        self.sidebar_visible: bool = True
        self.sidebar_split: float = 20.0        # 收起前记住的百分比宽度
        self.show_diagnostics: bool = False     # 主区诊断面板开关（U-10）

        # 外部事件队列（轮询线程 → ui.timer 主循环消费；G2 接横幅/模态）。
        self.events: queue.Queue[tuple[Path, str]] = queue.Queue()
        # U-13 单实例转交批次队列（cli 服务器线程 enqueue → 主循环 drain；
        # 元素 = open_files 完整 OpenResult 列表，含失败文件诊断态）。
        self.handoff_queue: queue.Queue[list[OpenResult]] = queue.Queue()

        # 退出码（run_gui 返回值；G2 退出 flush 时可能置 3）。
        self.exit_code: int = 0

        # 界面刷新回调，由 layout.build_gui 注入（ui.refreshable 集合刷新）。
        self.ui_refresh: Callable[[], None] | None = None

        # 外部事件横幅文本（G1 单文本占位；G2b 起用 per-file banners，
        # banner_text 保留为兼容聚合视图，只读派生）。
        self.banner_text: str = ""
        # U-9 横幅状态机（§7.6）：文件 Path → 横幅文案（None 键 = 全局兜底，
        # 仅当会话未绑定 path 回调时出现）。纯层见 ui/dialogs.apply_external_event。
        self.banners: dict[Path | None, str] = {}
        # 搜索框元素引用与同步防抖旗标（items.build_items_panel 写入；
        # 类型 Any，避免本模块 import nicegui——保持无头可测）。
        self.search_input: object | None = None
        self.search_syncing: bool = False

        # -- G2a：编辑提交编排（§7.5，纯层见 ui/commit.py） ----------------------
        # 文本类控件防抖状态机（400ms；layout ui.timer 周期 drain_debounce）。
        self.debounce = DebounceManager()
        # 门控标记（U-6 即时反馈）：活动文件内 item_path → GateFeedback。
        # 控件构建时读取，键击/提交后就地更新（不整体 refresh，防输入丢焦点）。
        self.gate_markers: dict[str, GateFeedback] = {}
        # 提交失败错误条（§7.5 第 5 步）：文案 + 可重试条目 path。
        self.commit_error: str | None = None
        self.commit_error_retry: str | None = None
        # "最近提交回显"（U-7 可选增强，已实现）：`path: 旧 → 新`，
        # 下次提交/切换条目时更新或清除。
        self.last_commit_echo: str | None = None
        # 冲突/gone 处置钩子：G2b 默认接线到本类的模态编排方法；测试可
        # 覆盖或置 None（置 None 时 apply_commit_result 降级为错误条文案）。
        self.on_conflict: Callable[[CommitResult], None] | None = (
            self._handle_conflict_result
        )
        self.on_gone: Callable[[CommitResult], None] | None = (
            self._handle_gone_result
        )

        # -- G2b：横幅/模态/关闭/退出编排（§7.6/§7.7，U-9/U-12） --------------
        # 待决冲突模态（modals.sync_modals 据此开/关对话框）。
        self.pending_conflict: ConflictRequest | None = None
        # 错误条"处理冲突"入口：paused 文件（关闭模态后常驻错误条）。
        self.commit_error_conflict: Path | None = None
        # 待决关闭确认框（U-12 need_confirm / failed 分流）。
        self.pending_close: CloseConfirm | None = None
        # 待决退出确认框（§7.7 failed / blocked 分流）。
        self.pending_exit: ExitConfirm | None = None
        # 退出流程标志：exiting=决策树进行中；exit_finalized=已定退出码并
        # 请求停服（on_app_shutdown 安全网据此幂等跳过）。
        self.exiting: bool = False
        self.exit_finalized: bool = False
        self.last_exit_plan: ExitPlan | None = None   # 测试/诊断用快照
        # UI 注入钩子：toast 通知（ui.notify）与停服（app.shutdown）。
        # 无头测试不注入 → 静默。
        self.notify_fn: Callable[[str, str], None] | None = None
        self.request_stop: Callable[[], None] | None = None
        # 模态宿主（layout.build_gui 写入；测试/诊断用，类型 Any 避免
        # 本模块 import nicegui）。
        self.modal_host: object | None = None

    # -- 刷新 -------------------------------------------------------------

    def touch(self) -> None:
        """状态变更后请求界面刷新（无 UI 上下文时静默，便于无头测试）。"""
        if self.ui_refresh is not None:
            self.ui_refresh()

    # -- 文件登记 -----------------------------------------------------------

    def register_results(self, results: list[OpenResult]) -> list[OpenResult]:
        """登记 open_files 结果：成功者入打开顺序表，失败者入 failed 表。

        G2c 正规化：对所有成功会话经**公开 API** ``set_on_state_change``
        绑定 per-path 回调（CLI 启动批次 / 单实例转交 / "+"入口共用本
        收口点；回调线程语义见 make_state_callback）。返回列表原样透传
        （调用方按需 notify）。线程安全。
        """
        with self._lock:
            for r in results:
                if r.path not in self._open_order and r.path not in self._failed:
                    self._open_order.append(r.path)
                if r.session is None:
                    self._failed[r.path] = list(r.diagnostics)
                else:
                    self._failed.pop(r.path, None)
        for r in results:
            if r.session is not None:
                self._bind_state_callback(r.path, r.session)
        return results

    def _bind_state_callback(self, path: Path, session: FileSession) -> None:
        """给单个会话绑定 per-path 事件回调（公开 API；幂等登记）。"""
        session.set_on_state_change(self.make_state_callback(path))
        with self._lock:
            self._bound[path] = session

    def file_entries(self) -> list[FileEntry]:
        """侧栏展示模型（U-2）：打开顺序，含加载失败者。"""
        with self._lock:
            order = list(self._open_order)
            failed = dict(self._failed)
        sessions = self.manager.sessions
        # 防御：CLI 直接操作过 manager（未经 register_results）也能显示
        for p in sessions:
            if p not in order and p not in failed:
                order.append(p)

        entries: list[FileEntry] = []
        for p in order:
            if p in failed:
                entries.append(FileEntry(
                    path=p, name=p.name, fmt=None, loaded=False,
                    level=diagnostics_level(failed[p]),
                    diagnostics=failed[p],
                ))
                continue
            session = sessions.get(p)
            if session is None:
                continue  # 已被关闭（G2b U-12）：从侧栏消失
            diags = list(session.diagnostics)
            entries.append(FileEntry(
                path=p, name=p.name, fmt=session.doc.format, loaded=True,
                level=diagnostics_level(diags), diagnostics=diags,
                gone=bool(getattr(session, "gone_readonly", False)),
            ))
        return entries

    def failed_diagnostics(self, path: Path) -> list[Diagnostic]:
        with self._lock:
            return list(self._failed.get(path, []))

    # -- 活动文件 / 选中条目 --------------------------------------------------

    def active_session(self) -> FileSession | None:
        if self.active_path is None:
            return None
        return self.manager.get(self.active_path)

    def activate(self, path: Path) -> None:
        """切换活动文件（U-2）。切换前先失焦 flush 防抖暂态（§7.5 第 3 步）。"""
        if path != self.active_path:
            self.flush_debounce_all()      # 视为失焦：计时中的值立即提交
            self.gate_markers.clear()      # 门控标记按活动文件隔离
            self.last_commit_echo = None
        self.active_path = path
        self.selected_item = None
        self.auto_expanded_item = None     # N5：新文件的选中尚未自动展开
        self.search_query = ""
        self.show_diagnostics = False
        self.touch()

    def select_item(self, item_path: str | None) -> None:
        if item_path != self.selected_item and self.selected_item is not None:
            # 切换选中条目 = 旧条目控件失焦（§7.5 第 3 步）
            self.flush_debounce_item(self.selected_item)
        if item_path != self.selected_item:
            # N5：选中变化 → 允许 items 面板**一次**自动展开所在组；
            # 同一条目后续刷新不再强制展开（手动收起的组保持收起）。
            self.auto_expanded_item = None
        self.selected_item = item_path
        self.touch()

    def remember_open_group(self, key: str, opened: bool) -> None:
        """展开态落点（items 组/子组与 layout 文件说明折叠区共用；不 touch）。"""
        if opened:
            self.open_groups.add(key)
        else:
            self.open_groups.discard(key)

    def selected_config_item(self) -> ConfigItem | None:
        """按 path 从**当前** doc 重取条目（关键坑：提交后 doc 整体重载）。"""
        session = self.active_session()
        if session is None or self.selected_item is None:
            return None
        for it in session.doc.items:
            if it.path == self.selected_item:
                return it
        return None  # gone：条目在重载后消失（G2 处理 gone 态）

    def set_search(self, query: str) -> None:
        self.search_query = query
        self.touch()

    def toggle_sidebar(self) -> None:
        """U-1 侧栏收起/展开（按钮入口；Ctrl+B 快捷键 G2 注册后调此方法）。"""
        self.sidebar_visible = not self.sidebar_visible
        self.touch()

    # -- 打开更多文件（"+"按钮 / 单实例转交） --------------------------------

    def open_more(self, paths: list[Path | str]) -> list[OpenResult]:
        """会话内新开文件（U-2 "+"入口；U-13 转交/失败重载复用）。

        G2c 正规化：批量 open 后由 :meth:`register_results` 统一经公开
        API 绑 per-path 回调（此前逐文件传参的过渡做法移除）；打开成功
        者启动 poller（幂等）。重复打开同一文件 → 已有 session（幂等）。
        """
        resolved = [Path(p).resolve() for p in paths]
        results = self.manager.open_files(
            resolved, poll_interval=self.poll_interval,
        )
        self.register_results(results)
        for r in results:
            if r.session is not None:
                r.session.start_poller()
        return results

    def ensure_state_callbacks(self) -> int:
        """为 manager 中未经本状态登记的会话补绑 per-path 回调（返回补绑数）。

        **公开 API 安全网（G2c 正规化，取代 adopt_sessions 私有属性
        hack）**：正常路径（CLI 启动批次 / 单实例转交 / "+"入口）都已在
        :meth:`register_results` 收口绑定；本方法防御未来新增的
        manager 直开路径——按 ``path → session`` 对象同一性判定，仅对
        未登记（或关闭后同路径重建）的会话经 ``set_on_state_change`` 重绑，
        不再读/写私有属性。layout drain timer 周期调用。
        """
        adopted = 0
        for path, session in self.manager.sessions.items():
            with self._lock:
                known = self._bound.get(path)
            if known is not session:
                self._bind_state_callback(path, session)
                adopted += 1
        return adopted

    def make_state_callback(self, path: Path | None = None) -> Callable[[str], None]:
        """构造 on_state_change 回调（可能来自轮询线程——只入队不碰 UI）。

        正常路径经 :meth:`register_results` 绑定时都带文件 Path（per-file
        横幅/只读态按文件区分，U-9）；``path=None`` 形态保留给测试与直接
        使用 open_files 批量回调契约的调用方（消费侧按事件文本刷新全部
        侧栏项即可）。
        """
        def _cb(event: str) -> None:
            self.events.put((path, event))
        return _cb

    def drain_events(self) -> list[tuple[Path | None, str]]:
        """ui.timer（主循环）调用：取出全部未决外部事件。"""
        out: list[tuple[Path | None, str]] = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out

    # -- G2b：U-9 横幅状态机（事件消费 → per-file 横幅） ----------------------

    def notify(self, message: str, kind: str = "positive") -> None:
        """toast 通知（layout 注入 ui.notify；无头静默）。"""
        if self.notify_fn is not None:
            self.notify_fn(message, kind)

    def consume_events(self) -> list[tuple[Path | None, str]]:
        """drain 外部事件并推进横幅状态机（ui.timer 主循环调用）。

        - 'external_modified' → 置"文件已在磁盘上被修改"横幅（不自动重载、
          不自动覆盖，§7.6）；
        - 'gone' → 置区别文案"文件已被删除、移动或不可读"横幅（只读态由
          session 侧 gone_readonly 承载，UI 刷新后控件全部转只读）；
        - 'recovered' → 撤该文件横幅 + toast 提示（只读态解除由 session
          完成，裁量：恢复提示用 toast 不占横幅）。
        """
        events = self.drain_events()
        if not events:
            return []
        for path, ev in events:
            self.banners = apply_external_event(self.banners, path, ev)
            if ev == "recovered":
                self.notify(BANNER_RECOVERED, "positive")
        self.touch()
        return events

    def dismiss_banner(self, key: Path | None) -> None:
        """手动关闭一条横幅（× 按钮）。

        裁量登记：'external_modified' 横幅手动关闭后不自动重现——poller
        边沿触发，下一次外部修改会再次入队事件并重挂横幅；基线冲突仍由
        提交前校验兜底（§7.6），关闭横幅不产生数据风险。
        """
        if key in self.banners:
            self.banners.pop(key)
            self.touch()

    def banner_lines(self) -> list[tuple[Path | None, str]]:
        """横幅展示行（稳定序：None 全局键在前，其余按路径字符串）。"""
        def sort_key(k: Path | None) -> str:
            return "" if k is None else str(k)
        return [(k, self.banners[k]) for k in sorted(self.banners, key=sort_key)]

    # -- G2a：编辑提交编排（§7.5；纯层 ui/commit.py，本节只做宿主接线） ----------

    def apply_pending(self, item_path: str, result: PendingResult) -> GateFeedback:
        """门控结果 → 标记状态存储（U-6 即时反馈）。

        ``intermediate`` → ``keep``：不写存储（保持旧标记，无骚扰，裁量见
        commit.GateFeedback docstring）；ok → 清除该 path 标记。
        """
        fb = pending_feedback(result)
        if fb.marker == "keep":
            pass
        elif fb.marker == "none":
            self.gate_markers.pop(item_path, None)
        else:
            self.gate_markers[item_path] = fb
        return fb

    def gate_feedback(self, item_path: str) -> GateFeedback | None:
        """控件构建/就地刷新时读取该 path 的当前门控标记。"""
        return self.gate_markers.get(item_path)

    def on_text_keystroke(self, item_path: str, text: str) -> GateFeedback | None:
        """文本类键击（§7.5 第 1–2 步）：立即进门控暂态 + 重置防抖计时。

        不 touch()——标记由控件闭包就地更新，防整体重建导致输入丢焦点。
        返回本次门控反馈（'keep' 时返回存储中的旧标记或 None，便于就地渲染）。
        """
        session = self.active_session()
        if session is None:
            return None
        try:
            result = session.set_pending_text(item_path, text)
        except ValueError:
            # 防御：条目在重载后消失（gone）——丢弃计时与标记
            self.debounce.cancel(item_path)
            self.gate_markers.pop(item_path, None)
            return None
        fb = self.apply_pending(item_path, result)
        self.debounce.keystroke(item_path, text)
        if fb.marker == "keep":
            return self.gate_markers.get(item_path)
        return fb

    def commit_text_now(self, item_path: str, text: str) -> CommitResult | None:
        """防抖到期 / 失焦 flush：进入暂态（若尚未）并提交。

        门控未通过（无暂态）→ 不提交、保留红标（§7.5：非法暂态不进提交，
        也避免 noop-refresh 误清标记）。
        """
        session = self.active_session()
        if session is None:
            return None
        try:
            if not session.has_pending(item_path):
                result = session.set_pending_text(item_path, text)
                self.apply_pending(item_path, result)
            if not session.has_pending(item_path):
                return None  # block/invalid/intermediate：不落盘（A-9）
            return self._commit(session, item_path)
        except ValueError:
            self.debounce.cancel(item_path)
            self.gate_markers.pop(item_path, None)
            return None

    def commit_click_now(self, item_path: str, *, checked: bool | None = None,
                         text: str | None = None) -> CommitResult | None:
        """点击类即时提交（bool 开关 / 枚举候选选中，§7.5 无防抖，A-13）。

        ``checked`` 非 None → set_pending_bool；否则以 ``text`` 走
        set_pending_text（枚举选中值/自定义输入确认）。
        """
        session = self.active_session()
        if session is None:
            return None
        try:
            if checked is not None:
                result = session.set_pending_bool(item_path, checked)
            else:
                result = session.set_pending_text(item_path, text or "")
            self.apply_pending(item_path, result)
            if not result.accepted:
                self.touch()  # 重建控件回退显示值（未落盘）
                return None
            return self._commit(session, item_path)
        except ValueError:
            self.gate_markers.pop(item_path, None)
            self.touch()
            return None

    def _commit(self, session: FileSession, item_path: str) -> CommitResult:
        result = session.commit_pending(item_path)
        self.apply_commit_result(result)
        return result

    def apply_commit_result(self, result: CommitResult) -> CommitAction:
        """CommitResult → UI 动作分流（commit.commit_action 映射，§7.5 第 5 步）。"""
        action = commit_action(result)
        path = result.path
        if action.kind == "refresh":
            if path is not None:
                self.gate_markers.pop(path, None)
            self.commit_error = None
            self.commit_error_retry = None
            if result.status == "committed":
                # U-7 可选增强（已实现）：最近提交回显 `path: 旧 → 新`
                short = path.rsplit(".", 1)[-1] if path else ""
                self.last_commit_echo = (
                    f"{short}: {result.old_value} → {result.new_value}"
                )
            self.touch()  # 提交后 doc 整体重载：按 path 重取刷新控件
        elif action.kind == "error":
            self.commit_error = action.message
            self.commit_error_retry = action.retry_path
            self.touch()
        elif action.kind == "conflict":
            if self.on_conflict is not None:
                self.on_conflict(result)   # G2b：§7.6 冲突模态
            else:
                # 未接线降级：错误条提示（不提供重试——重试仍会被基线校验挡）
                self.commit_error = f"提交被阻止：{action.message}"
                self.commit_error_retry = None
                self.touch()
        elif action.kind == "gone":
            if self.on_gone is not None:
                self.on_gone(result)       # G2b：文件不可读处置
            else:
                self.commit_error = f"提交被阻止：{action.message}"
                self.commit_error_retry = None
                self.touch()
        # silent（paused）：静默不打扰
        return action

    def retry_commit(self) -> CommitResult | None:
        """错误条"重试"（§7.5 第 5 步：failed 暂态保留可重试）。"""
        session = self.active_session()
        path = self.commit_error_retry
        if session is None or path is None:
            return None
        try:
            return self._commit(session, path)
        except ValueError:
            self.commit_error = None
            self.commit_error_retry = None
            return None

    def drain_debounce(self) -> None:
        """layout ui.timer（<<400ms 周期）调用：到期条目提交（恰一次）。"""
        due = self.debounce.poll()
        for item_path, text in due:
            self.commit_text_now(item_path, text)

    def flush_debounce_item(self, item_path: str) -> None:
        """单条目失焦 flush（控件 blur / 切换选中条目，§7.5 第 3 步）。"""
        entry = self.debounce.flush(item_path)
        if entry is not None:
            self.commit_text_now(*entry)

    def flush_debounce_all(self) -> None:
        """全部失焦 flush（切换/关闭活动文件、退出前；G2b 关闭文件复用）。"""
        for item_path, text in self.debounce.flush_all():
            self.commit_text_now(item_path, text)

    def do_undo(self) -> CommitResult | None:
        """工具栏撤销（U-7；Ctrl+Z 快捷键 G2b 统一注册后调此方法）。

        裁量登记：撤销前先 flush 防抖暂态——键入值随即成为最新 undo 条目，
        本次 do_undo 撤销的正是它（与真实 UI"点按钮先 blur→flush 再 undo"
        的顺序一致，语义统一为"撤销最近一次已提交编辑"，§7.7）。
        """
        session = self.active_session()
        if session is None:
            return None
        self.flush_debounce_all()   # 防抖中的暂态先落盘，避免撤销后被覆盖
        result = session.undo()
        self.apply_commit_result(result)
        return result

    def do_redo(self) -> CommitResult | None:
        """工具栏重做（U-7；Ctrl+Shift+Z 快捷键 G2b 注册后调此方法）。"""
        session = self.active_session()
        if session is None:
            return None
        self.flush_debounce_all()
        result = session.redo()
        self.apply_commit_result(result)
        return result

    # -- G2b：§7.6 冲突模态编排（U-9） ----------------------------------------

    def _handle_conflict_result(self, result: CommitResult) -> None:
        """on_conflict 默认接线：提交/undo/redo 撞冲突 → 挂起冲突模态请求。

        模态由 modals.sync_modals 在 state.touch() 后开启（数据驱动，无头
        安全）；文件 = 当前活动文件（提交类操作只作用于活动会话）。
        """
        path = self.active_path
        if path is None:
            return
        self.pending_conflict = ConflictRequest(
            file=path,
            file_name=path.name,
            reason=result.reason,
            source="commit",
            item_path=result.path,   # CommitResult.path = 条目 path
        )
        self.commit_error = None     # 模态接管，降级错误条撤回
        self.commit_error_retry = None
        self.touch()

    def _handle_gone_result(self, result: CommitResult) -> None:
        """on_gone 默认接线：提交尝试撞 gone → 挂"文件不可读"横幅（§7.6）。

        只读态本身由 session.gone_readonly 承载（核心已置位）；横幅经
        状态机与 poller 事件同路径（幂等：同键覆盖）。
        """
        path = self.active_path
        if path is not None:
            self.banners = apply_external_event(self.banners, path, "gone")
        self.touch()

    def resolve_conflict_choice(self, choice: str) -> CommitResult | None:
        """冲突模态抉择（§7.6）：``'reload'`` / ``'force'`` / ``'dismiss'``。

        - ``'reload'``（默认焦点）→ session.resolve_conflict('reload') →
          成功后撤该文件横幅、控件按 path 重取刷新（apply_commit_result 的
          refresh 分支）、toast 提示 **undo/redo 栈已清空**；
        - ``'force'`` → 调用前 UI 层必须已弹**二次警示**（文案含"永久丢失
          外部修改、无法通过撤销找回"，见 dialogs.FORCE_WARNING）→
          session.resolve_conflict('force')；
        - ``'dismiss'``（×/Esc 关闭模态）→ pause_auto_commit() + 顶部错误条
          **常驻**（"自动提交已暂停"+「处理冲突」恢复入口）+ 暂态保留；
          source='exit' 时视同「取消退出」（留在应用）。

        抉择完成后按 source 续流：'exit' → 重新 flush + 决策树；'close' →
        **不自动重试关闭**（用户再点关闭入口，裁量登记）；'commit' → 无续流。
        """
        req = self.pending_conflict
        if req is None:
            return None
        session = self.manager.get(req.file)
        if session is None:
            self.pending_conflict = None
            self.exiting = False
            self.touch()
            return None

        if choice == "dismiss":
            session.pause_auto_commit()
            self.pending_conflict = None
            self.commit_error = f"{req.file_name}：{PAUSED_ERROR_TEXT}"
            self.commit_error_retry = None
            self.commit_error_conflict = req.file   # 错误条「处理冲突」入口
            if req.source == "exit":
                self.exiting = False                # 取消退出 = 留在应用
            self.touch()
            return None

        result = session.resolve_conflict(choice)   # 'reload' | 'force'
        self.pending_conflict = None
        if result.status in ("reloaded", "committed"):
            self.banners.pop(req.file, None)        # 撤横幅（基线已同步磁盘）
            self.commit_error_conflict = None
            if result.status == "reloaded":
                self.gate_markers.clear()
                self.notify(RELOAD_DONE_NOTIFY, "positive")
            else:
                self.notify("已强制覆盖磁盘文件（外部修改已丢失）。", "warning")
        self.apply_commit_result(result)            # refresh/error 分流 + touch
        if req.source == "exit" and result.status in ("reloaded", "committed"):
            self.continue_exit()
        elif req.source == "close" and result.status in ("reloaded", "committed"):
            self.notify("冲突已处理；如需关闭该文件，请再次点击关闭。", "info")
        return result

    def reopen_conflict(self, path: Path) -> None:
        """paused 错误条「处理冲突」入口：重新挂起冲突模态（§7.6 恢复路径）。

        resolve_conflict('reload'/'force') 成功即隐含 resume（核心契约），
        故无需单独调 resume_auto_commit。
        """
        session = self.manager.get(path)
        if session is None:
            self.commit_error = None
            self.commit_error_conflict = None
            self.touch()
            return
        self.pending_conflict = ConflictRequest(
            file=path, file_name=path.name, reason=None, source="commit",
        )
        self.commit_error = None
        self.commit_error_retry = None
        self.commit_error_conflict = None
        self.touch()

    def reload_file(self, path: Path) -> CommitResult | None:
        """横幅【重新加载】手动入口（U-9/§7.6）：resolve_conflict('reload')。

        - 'external_modified' 横幅 → 手动重载 = 用户显式动作（规范禁止的是
          **自动**重载/覆盖）；暂态丢弃、undo/redo 清空（toast 提示）；
        - 'gone' 横幅 → §7.6 "用户重载"恢复路径：重载成功即解除只读态并
          撤横幅；仍不可读 → gone 事件重挂横幅（apply_commit_result 分流）。
        """
        session = self.manager.get(path)
        if session is None:
            return None
        result = session.resolve_conflict("reload")
        if result.status == "reloaded":
            self.banners.pop(path, None)
            self.commit_error = None
            self.commit_error_retry = None
            self.commit_error_conflict = None
            if self.active_path == path:
                self.gate_markers.clear()
            self.notify(RELOAD_DONE_NOTIFY, "positive")
        self.apply_commit_result(result)   # refresh / gone 分流 + touch
        return result

    # -- G2b：U-12 关闭文件编排（§7.7） ----------------------------------------

    def request_close_file(self, path: Path | None) -> "object | None":
        """关闭文件入口（悬停 × / 右键菜单 / Ctrl+W，U-12）。

        流程：**先 flush 防抖暂态**（关闭视为失焦，§7.5 第 3 步；防抖 key =
        item_path 且只绑定活动文件——关闭非活动文件时其防抖条目已在最近一次
        activate 切换时 flush 过，G2a 钩子清单结论）→ session.close() →
        dialogs.close_plan 分流。返回 CloseResult（测试断言用）。
        """
        if path is None:
            return None
        if path == self.active_path:
            self.flush_debounce_all()
        result = self.manager.close_file(path)
        action = close_plan(result)
        if action.kind == "removed":
            self._after_closed(path)
        elif action.kind == "confirm_discard":
            self.pending_close = CloseConfirm(
                file=path, kind="need_confirm", lines=action.lines)
            self.touch()
        elif action.kind == "conflict_modal":
            item = result.items[0][0] if result.items else None
            self.pending_conflict = ConflictRequest(
                file=path, file_name=path.name,
                reason=result.reason, source="close", item_path=item,
            )
            self.touch()
        else:  # failed_dialog：错误条 + 确认框【重试】/【放弃暂态并关闭】
            self.commit_error = f"{path.name}：关闭触发的提交失败，已阻止关闭。"
            self.commit_error_retry = None
            self.pending_close = CloseConfirm(
                file=path, kind="failed", lines=action.lines)
            self.touch()
        return result

    def confirm_close(self, choice: str) -> None:
        """关闭确认框抉择：``'discard'``（放弃暂态并关闭）/ ``'retry'`` /
        ``'cancel'``（取消关闭）。"""
        pc = self.pending_close
        if pc is None:
            return
        self.pending_close = None
        if choice == "cancel":
            self.touch()
            return
        if choice == "retry":
            self.request_close_file(pc.file)
            return
        result = self.manager.close_file(pc.file, discard_illegal=True)
        if result.status == "closed":
            self._after_closed(pc.file)
        else:
            self.touch()  # 防御：discard 路径核心契约必 'closed'

    def _after_closed(self, path: Path) -> None:
        """关闭成功收尾（§7.7）：侧栏移除 + 活动文件切换到相邻/空态。

        资源释放（基线/暂态/undo/redo/诊断/poller）由核心 close() 完成；
        本方法只清 UI 侧登记。相邻选择裁量：优先原位置的**下一个**文件
        （移除后落在原下标者），否则最后一个；全部关闭 → 主区空态，
        应用不退出（"+"可再开，U-12）。
        """
        with self._lock:
            idx = self._open_order.index(path) if path in self._open_order else None
            self._open_order = [p for p in self._open_order if p != path]
            self._failed.pop(path, None)
            self._bound.pop(path, None)   # G2c：补绑登记一并清除
        self.banners.pop(path, None)
        # N5：清理该文件的展开键（组/子组/文件说明——键前缀约定见
        # items._group_key 与 layout.file_header_view），集合不随打开-关闭
        # 循环增长；同路径重开回到默认折叠态。
        prefixes = tuple(f"{p}::" for p in {path, path.resolve()})
        self.open_groups = {
            k for k in self.open_groups if not k.startswith(prefixes)
        }
        self.debounce.flush_all()  # 防御性清空（关闭前已 flush，此处不提交）
        if self.active_path == path:
            entries = self.file_entries()
            neighbor: Path | None = None
            if entries:
                if idx is not None and idx < len(entries):
                    neighbor = entries[idx].path
                else:
                    neighbor = entries[-1].path
            self.active_path = neighbor
            self.selected_item = None
            self.auto_expanded_item = None
            self.gate_markers.clear()
            self.last_commit_echo = None
        self.touch()

    # -- G2b：§7.7 退出 flush 决策树 -------------------------------------------

    def begin_exit(self) -> ExitPlan | None:
        """交互式退出流程入口（header「退出」按钮）。

        flush 防抖暂态 → manager.flush_all() → dialogs.flush_exit_plan →
        分流：clean 停服退出（exit 0）/ blocked 列出条目确认 / conflict 弹
        §7.6 模态（含「取消退出」）/ failed 错误条 + 确认框（仍要退出 =
        abandon + exit 3 / 取消 = 留在应用）。
        """
        if self.exit_finalized:
            return self.last_exit_plan
        self.exiting = True
        self.flush_debounce_all()
        results = self.manager.flush_all()
        blocked: dict[Path, dict[str, str]] = {}
        for p, s in self.manager.sessions.items():
            last = s.last_blocked_items()
            if last:
                blocked[p] = last
        plan = flush_exit_plan(results, blocked)
        self.last_exit_plan = plan
        if plan.action == "exit_clean":
            self._finalize_exit(0)
        elif plan.action == "confirm_blocked":
            self.pending_exit = ExitConfirm(kind="blocked", lines=plan.blocked)
            self.touch()
        elif plan.action == "show_conflict":
            self.pending_conflict = ConflictRequest(
                file=plan.conflict_file,
                file_name=getattr(plan.conflict_file, "name",
                                  str(plan.conflict_file)),
                reason=plan.conflict_reason,
                source="exit",
            )
            self.touch()
        else:  # ask_failed
            lines = tuple(
                f"{getattr(p, 'name', str(p))} · {line}"
                for p, file_lines in plan.failed for line in file_lines
            )
            self.pending_exit = ExitConfirm(
                kind="failed", lines=lines,
                files=tuple(p for p, _ in plan.failed),
            )
            self.commit_error = "退出 flush：存在提交失败的文件（详见确认框）。"
            self.commit_error_retry = None
            self.touch()
        return plan

    def continue_exit(self) -> ExitPlan | None:
        """冲突抉择完成后的退出续流：重新 flush + 决策树（§7.7）。"""
        if not self.exiting:
            return None
        return self.begin_exit()

    def confirm_exit(self, choice: str) -> None:
        """退出确认框抉择：``'ok'``（blocked→退出 / failed→仍要退出）或
        ``'cancel'``（取消 = 留在应用，中止退出流程）。"""
        pe = self.pending_exit
        if pe is None:
            return
        self.pending_exit = None
        if choice == "cancel":
            self.exiting = False
            self.commit_error = None
            self.touch()
            return
        if pe.kind == "failed":
            for p in pe.files:
                session = self.manager.get(p)
                if session is not None:
                    session.abandon()   # 放弃该文件未提交暂态（§7.7）
            self._finalize_exit(3)
        else:
            self._finalize_exit(0)      # blocked 非法输入本就不落盘（裁量登记）

    def exit_noninteractive(self) -> int:
        """非交互退出（浏览器全部断连自动停服 / on_shutdown 安全网）。

        **局限登记**：触发时已无可交互客户端，冲突模态/确认框无法弹出——
        flush 后非全 clean 一律视同「仍要退出」（failed 文件 abandon，
        exit_code=3，dialogs.noninteractive_exit_code）；全 clean → 0。

        可能从非主线程调用（断连监视 threading.Timer / uvicorn shutdown
        钩子）——先摘除 ui_refresh，防 flush 提交的 touch 跨线程刷 UI。
        """
        if self.exit_finalized:
            return self.exit_code
        self.ui_refresh = None
        self.flush_debounce_all()
        results = self.manager.flush_all()
        code = noninteractive_exit_code(results)
        if code == 3:
            for p, r in results.items():
                if r.status != "clean":
                    session = self.manager.get(p)
                    if session is not None:
                        try:
                            session.abandon()
                        except RuntimeError:
                            pass    # 防御：会话已关闭
        self._finalize_exit(code)
        return code

    def _finalize_exit(self, code: int) -> None:
        """定退出码并请求停服（app.py 注入 request_stop=app.shutdown）。"""
        self.exit_code = code
        self.exit_finalized = True
        self.exiting = False
        self.pending_conflict = None
        self.pending_exit = None
        self.pending_close = None
        if self.request_stop is not None:
            try:
                self.request_stop()
            except Exception:
                pass    # 防御：服务已停（ui.run 返回后的安全网路径）

    def beforeunload_guard(self) -> bool:
        """浏览器 beforeunload 提示开关（尽力而为，裁量登记见 app.py）。

        True = 存在"退出会丢失"的状态：会话未决暂态（提交失败/冲突暂停
        保留者）或 paused 文件。全 clean → False（不打扰刷新）。

        裁量：**不查防抖计时器**——键击即进会话暂态（on_text_keystroke），
        合法值已被 pending_items 覆盖；计时中的**非法**文本本就不会落盘
        （§7.7 退出即丢弃），不构成"丢失"，不应武装守卫。
        """
        for session in self.manager.sessions.values():
            try:
                if session.pending_items() or session.paused:
                    return True
            except RuntimeError:
                continue    # 防御：会话刚关闭
        return False

    # -- G2c：U-13 单实例转交 + U-10 失败重载 ---------------------------------

    def enqueue_handoff(self, results: list[OpenResult]) -> None:
        """线程安全转交投递（cli 单实例服务器线程 → GUI）。

        cli 在自己的监听线程里完成 ``manager.open_files`` 后把完整
        OpenResult 列表（含失败文件诊断态）投递进来；主循环 drain timer
        经 :meth:`process_handoffs` 消费（激活/聚焦/侧栏登记都在主线程）。
        """
        self.handoff_queue.put(list(results))

    def process_handoffs(self) -> int:
        """layout drain timer（主循环）调用：消费全部未决转交批次。"""
        applied = 0
        while True:
            try:
                results = self.handoff_queue.get_nowait()
            except queue.Empty:
                return applied
            self._apply_handoff_results(results, self._known_paths())
            applied += 1

    def on_handoff_received(self, paths: list[Path | str]) -> None:
        """U-13 单实例转交钩子（GUI 线程直呼形态；跨线程见 enqueue_handoff）。

        §9.4/U-13 语义：未打开的加入侧栏并**激活**、已打开的**仅聚焦**；
        打开失败的文件以诊断态进侧栏（U-10）。经 :meth:`open_more` 幂等。
        """
        known = self._known_paths()
        results = self.open_more(paths)
        self._apply_handoff_results(results, known)

    def _known_paths(self) -> set[Path]:
        """GUI 已登记（侧栏可见）的路径快照——转交"新增 vs 已开"判定基准。"""
        with self._lock:
            return set(self._open_order)

    def _apply_handoff_results(
        self, results: list[OpenResult], known: set[Path]
    ) -> None:
        """转交批次落地（§9.4）：register + poller + 激活/聚焦抉择。

        - 首个**新**成功文件 → 激活（"未打开的加入并激活"）；
        - 只有新失败文件 → 激活该失败项（主区呈现诊断列表，U-10）；
        - 全部已打开 → 仅聚焦清单第一个（"已打开的仅聚焦"，不重建）。
        """
        self.register_results(results)
        for r in results:
            if r.session is not None:
                r.session.start_poller()   # cli 保底已启；此处幂等
        fresh = [r.path for r in results if r.path not in known]
        target: Path | None = None
        fresh_open = [p for p in fresh if self.manager.get(p) is not None]
        if fresh_open:
            target = fresh_open[0]
        elif fresh:
            target = fresh[0]
        elif results:
            target = results[0].path
        if target is not None:
            self.activate(target)
        else:
            self.touch()   # 纯失败登记批次也要刷新侧栏

    def reload_failed(self, path: Path) -> OpenResult | None:
        """U-10 失败文件「重新加载」入口：重新尝试打开（open_more 语义）。

        成功 → register_results 已把它移出 failed 表（侧栏诊断态解除）并
        激活；仍失败 → failed 表诊断以本次结果覆盖（登记最新诊断）。
        """
        results = self.open_more([path])
        result = results[0] if results else None
        if result is not None and result.session is not None:
            self.activate(result.path)
        return result

    def on_app_shutdown(self) -> None:
        """退出安全网（§7.7/§9.2）：app.on_shutdown（Ctrl+C / 进程停服）时
        补一次非交互 flush——交互式流程已 finalize 则幂等跳过。"""
        if not self.exit_finalized:
            self.exit_noninteractive()
        return None
