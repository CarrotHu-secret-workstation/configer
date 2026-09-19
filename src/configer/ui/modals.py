"""G2b 模态层（NiceGUI）：§7.6 冲突模态 + 强制覆盖二次警示 + U-12 关闭
确认框 + §7.7 退出确认框。

**数据驱动**：本模块只按 :class:`~configer.ui.state.GuiState` 的
``pending_conflict`` / ``pending_close`` / ``pending_exit`` 字段渲染对话框
并回传抉择；全部编排（会话调用、续流、退出码）在 state 层——无头可测。

打开/关闭由 :meth:`ModalHost.sync` 统一同步（layout.refresh_all 每次
touch 后调用）：pending_* 出现 → 建对话框并 open；清空 → close。冲突模态
**非 persistent**：×/Esc/点外关闭 = 「暂不处理」→ state 侧
``pause_auto_commit()`` + 错误条常驻（§7.6）；关闭经 QDialog 'hide' 事件
捕获（按钮抉择已先行清空 pending_conflict，hide 回调据此区分主动关闭与
按钮处置，不会双触发）。

对话框一律建在 :attr:`ModalHost.root` 持久容器下（**不可**建在
refreshable 内——整体刷新会连带销毁对话框）。

视觉契约（Round 3：四框统一骨架；全部走 :mod:`configer.ui.theme` 的类与
令牌，本模块不出现一次性色值/魔法像素）
------------------------------------------------------------------------
- 统一骨架：``.dialog-card``（四框同宽：``min(34rem, 100vw - 2×space-4)``，
  窄屏不横向溢出）→ ``.dialog-head``（状态图标 + ``.t-title``，四框一致，
  此前关闭框只有标题）→ 正文 ``.dialog-body``（``overflow-wrap: break-word``：
  中文按正常断行，不逐字符硬断）→ 可选 ``.dialog-list``
  （滚动结果列表，高度上限走 ``--dialog-list-max`` 令牌）→
  ``.dialog-actions``（右对齐、可换行）。
- 强调层级：**每个对话框恰有一个主操作**（``unelevated color=primary``，
  Quasar 品牌色经主题重定向到 ``--accent`` 令牌），取"安全/建设性"的那个
  动作。破坏性但非最终确认的动作一律 ``flat color=negative``
  （``--q-negative`` → ``--status-error`` 令牌）：红字标记清楚、音量保持
  静默，绝不响过主操作。**唯一**破坏性醒目控件是强制覆盖二次确认的
  「确认覆盖」（``unelevated color=negative``）——不可逆的最后一步才允许
  转红实心。取消/返回类动作用无色的 ``flat``（构造时 ``color=None``：
  NiceGUI 默认 ``color='primary'`` 会挂上 Quasar 的 text-primary，让次级
  按钮染上强调色、抢主操作的音量）。
- 状态色从不单独承载语义：四框均"图标 + 标题/正文文字"同时在场，颜色
  只是强化；文字一律保持原文（§7.6 后果说明不软化、不改写）。
"""

from __future__ import annotations

from nicegui import ui

from . import dialogs, theme
from .state import GuiState

__all__ = ["ModalHost"]


def _dialog_head(icon: str, title: str, tone: str,
                 title_tone: str | None = None) -> None:
    """四框统一标题行：状态图标（``tone`` 语义色）+ ``.t-title`` 标题。

    图标与标题文字同时在场，状态色只是强化（不单独依赖颜色）；``tone`` /
    ``title_tone`` 取 theme 的 ``.fg-*`` 语义色类。
    """
    with ui.row(wrap=False).classes("dialog-head w-full"):
        ui.icon(icon).classes(f"icon-lg {tone}")
        ui.label(title).classes(
            "t-title" if title_tone is None else f"t-title {title_tone}")


class ModalHost:
    """模态宿主：持久根容器 + 打开中的对话框登记表 + 同步器。"""

    def __init__(self, state: GuiState) -> None:
        self.state = state
        self.root = ui.element("div")     # 持久容器（build_gui 期创建一次）
        self._open: dict[str, ui.dialog] = {}

    def open_keys(self) -> list[str]:
        """当前打开中的对话框类型（'conflict'/'force'/'close'/'exit'）。"""
        return list(self._open)

    # -- 同步（layout.refresh_all 调用） ---------------------------------------

    def sync(self) -> None:
        """pending_* 字段 → 对话框开/关（幂等；无头构建安全）。"""
        self._sync_one("conflict", self.state.pending_conflict is not None,
                       self._build_conflict)
        self._sync_one("close", self.state.pending_close is not None,
                       self._build_close)
        self._sync_one("exit", self.state.pending_exit is not None,
                       self._build_exit)
        if self.state.pending_conflict is None:
            self._drop("force")   # 冲突已处置/撤回：二次警示一并关闭

    def _sync_one(self, key: str, wanted: bool, builder) -> None:
        if wanted and key not in self._open:
            builder()
        elif not wanted and key in self._open:
            self._drop(key)

    def _drop(self, key: str) -> None:
        dlg = self._open.pop(key, None)
        if dlg is not None:
            try:
                dlg.close()
            except Exception:
                pass    # 防御：客户端已断开等

    # -- §7.6 冲突模态（U-9） ---------------------------------------------------

    def _build_conflict(self) -> None:
        """冲突模态：默认处置 =「重新加载」（唯一主操作 + autofocus + 提示）。

        「强制覆盖」是破坏性但**非最终确认**的动作：红字标记（``flat
        color=negative``）但保持次级；它的醒目形态只属于二次警示里的
        「确认覆盖」。
        """
        req = self.state.pending_conflict
        if req is None:
            return
        texts = dialogs.conflict_dialog_texts(req)
        with self.root:
            with ui.dialog() as dlg, ui.card().classes("dialog-card"):
                _dialog_head("warning", texts["title"], "fg-warn")
                ui.label(texts["body"]).classes("dialog-body t-body fg-primary")
                ui.label(texts["reload_hint"]).classes(
                    "dialog-body t-meta fg-secondary")
                ui.label(texts["force_hint"]).classes(
                    "dialog-body t-meta fg-secondary")
                with ui.row().classes("dialog-actions w-full"):
                    if req.source == "exit":
                        # §7.7：退出 flush 撞冲突 → 第三按钮「取消退出」
                        ui.button(
                            texts["cancel_exit"],
                            color=None,   # 静默次级（不染强调色）
                            on_click=lambda: self._choose("dismiss"),
                        ).props("flat no-caps")
                    ui.button(
                        texts["force"],
                        on_click=self._build_force,
                    ).props("flat color=negative no-caps")
                    reload_btn = ui.button(
                        texts["reload"],
                        on_click=lambda: self._choose("reload"),
                    ).props("unelevated color=primary no-caps autofocus")
                    reload_btn.tooltip("默认处置：外部修改保留，暂态与撤销栈丢弃")
        dlg.on("hide", lambda *_a: self._on_conflict_hide())
        self._open["conflict"] = dlg
        dlg.open()

    def _on_conflict_hide(self) -> None:
        """×/Esc/点外关闭 = 「暂不处理」（§7.6）→ pause + 错误条常驻。

        按钮抉择路径先行清空 pending_conflict（state.resolve_conflict_choice
        → touch → sync 关闭本对话框），此时 hide 回调看到 None → 不重复处置。
        """
        if self.state.pending_conflict is not None:
            self.state.resolve_conflict_choice("dismiss")
        self._open.pop("conflict", None)

    def _build_force(self) -> None:
        """强制覆盖 → **二次警示**对话框（§7.6 硬性文案要求）。

        此处是全应用**唯一**允许破坏性醒目的地方（「确认覆盖」实心红）：
        它已是不可逆的最后一步；警示正文原文照旧（永久丢失 / 无法找回）。
        """
        req = self.state.pending_conflict
        if req is None or "force" in self._open:
            return
        with self.root:
            with ui.dialog() as dlg, ui.card().classes("dialog-card"):
                _dialog_head("gpp_bad", dialogs.FORCE_CONFIRM_TITLE,
                             "fg-error", title_tone="fg-error")
                ui.label(dialogs.force_warning_text(req.file_name)).classes(
                    "dialog-body t-body fg-error font-bold")
                ui.label("确认后本次待写内容将直接覆盖磁盘文件，会话基线随"
                         "之更新。").classes("dialog-body t-meta fg-secondary")
                with ui.row().classes("dialog-actions w-full"):
                    ui.button(
                        dialogs.FORCE_BACK_LABEL,
                        color=None,   # 静默次级（不染强调色）
                        on_click=lambda: self._drop("force"),
                    ).props("flat no-caps")
                    ui.button(
                        dialogs.FORCE_CONFIRM_LABEL,
                        on_click=lambda: self._confirm_force(),
                    ).props("unelevated color=negative no-caps")
        dlg.on("hide", lambda *_a: self._open.pop("force", None))
        self._open["force"] = dlg
        dlg.open()

    def _confirm_force(self) -> None:
        self._drop("force")
        self._choose("force")

    def _choose(self, choice: str) -> None:
        self.state.resolve_conflict_choice(choice)   # state 内 touch → sync 收尾

    # -- U-12 关闭确认框 ---------------------------------------------------------

    def _build_close(self) -> None:
        """关闭确认框：主操作 = 不丢数据的那条路（need_confirm 无路可重试 →
        「取消关闭」为安全默认；failed → 「重试」）。「放弃暂态并关闭」是
        破坏性动作：红字标记但静默，绝不响过主操作。
        """
        pc = self.state.pending_close
        if pc is None:
            return
        failed = pc.kind == "failed"
        title = dialogs.CLOSE_FAILED_TITLE if failed else dialogs.CLOSE_CONFIRM_TITLE
        with self.root:
            with ui.dialog() as dlg, ui.card().classes("dialog-card"):
                _dialog_head("error" if failed else "warning", title,
                             "fg-error" if failed else "fg-warn")
                ui.label(f"文件：{pc.file.name}").classes(
                    "dialog-body t-body fg-primary")
                for line in pc.lines:
                    ui.label(f"· {line}").classes(
                        "dialog-body t-meta fg-secondary")
                if failed:
                    ui.label("可重试关闭（暂态保留），或放弃暂态并关闭"
                             "（§7.7）。").classes(
                        "dialog-body t-meta fg-secondary")
                with ui.row().classes("dialog-actions w-full"):
                    if failed:
                        # 主操作 =「重试」（保住暂态的前提下完成关闭）；取消与
                        # 放弃均为静默次级（放弃另加红字标记）
                        ui.button(
                            dialogs.CLOSE_CANCEL_LABEL,
                            color=None,
                            on_click=lambda: self._close_choice("cancel"),
                        ).props("flat no-caps")
                        ui.button(
                            dialogs.CLOSE_RETRY_LABEL,
                            on_click=lambda: self._close_choice("retry"),
                        ).props("unelevated color=primary no-caps")
                    else:
                        # 无重试余地：安全默认「取消关闭」承担唯一主操作
                        ui.button(
                            dialogs.CLOSE_CANCEL_LABEL,
                            on_click=lambda: self._close_choice("cancel"),
                        ).props("unelevated color=primary no-caps")
                    ui.button(
                        dialogs.CLOSE_DISCARD_LABEL,
                        on_click=lambda: self._close_choice("discard"),
                    ).props("flat color=negative no-caps")
        dlg.on("hide", lambda *_a: self._on_close_hide())
        self._open["close"] = dlg
        dlg.open()

    def _close_choice(self, choice: str) -> None:
        self.state.confirm_close(choice)   # state 内 touch → sync 关闭对话框

    def _on_close_hide(self) -> None:
        """×/Esc 关闭确认框 = 「取消关闭」（同冲突模态 hide 语义，防 Esc 后
        pending_close 滞留导致 sync 不再重建、关闭入口失效）。按钮抉择路径
        先行清空 pending_close（state.confirm_close → touch → sync），hide
        回调看到 None → 不重复处置。"""
        if self.state.pending_close is not None:
            self.state.confirm_close("cancel")
        self._open.pop("close", None)

    # -- §7.7 退出确认框 ----------------------------------------------------------

    def _build_exit(self) -> None:
        """退出确认框：结果列表用 ``.dialog-list``（滚动上限走令牌，替代
        内联 max-height）。主操作 = 不丢未提交内容的「取消」（failed）或
        本来就不落盘的「退出」（blocked）；「仍要退出」红字标记但静默。
        """
        pe = self.state.pending_exit
        if pe is None:
            return
        failed = pe.kind == "failed"
        title = dialogs.EXIT_FAILED_TITLE if failed else dialogs.EXIT_BLOCKED_TITLE
        with self.root:
            with ui.dialog() as dlg, ui.card().classes("dialog-card"):
                _dialog_head("error" if failed else "info", title,
                             "fg-error" if failed else "fg-accent")
                if failed:
                    ui.label(dialogs.EXIT_FAILED_HINT).classes(
                        "dialog-body t-meta fg-error")
                with ui.scroll_area().classes("dialog-list w-full"):
                    with ui.column().classes("w-full").style(theme.gap(1)):
                        for line in pe.lines:
                            ui.label(f"· {line}").classes(
                                "dialog-body t-meta t-mono fg-primary")
                with ui.row().classes("dialog-actions w-full"):
                    if failed:
                        # 主操作 =「取消」（留在应用，未提交内容不丢）；
                        # 「仍要退出」破坏性：红字标记但静默（绝不响过主操作）
                        ui.button(
                            dialogs.EXIT_CANCEL_LABEL,
                            on_click=lambda: self._exit_choice("cancel"),
                        ).props("unelevated color=primary no-caps")
                        ui.button(
                            dialogs.EXIT_FORCE_LABEL,
                            on_click=lambda: self._exit_choice("ok"),
                        ).props("flat color=negative no-caps")
                    else:
                        # blocked：非法暂态本就不会落盘，退出是安全默认
                        ui.button(
                            dialogs.EXIT_CANCEL_LABEL,
                            color=None,   # 静默次级（不染强调色）
                            on_click=lambda: self._exit_choice("cancel"),
                        ).props("flat no-caps")
                        ui.button(
                            dialogs.EXIT_OK_LABEL,
                            on_click=lambda: self._exit_choice("ok"),
                        ).props("unelevated color=primary no-caps")
        dlg.on("hide", lambda *_a: self._on_exit_hide())
        self._open["exit"] = dlg
        dlg.open()

    def _exit_choice(self, choice: str) -> None:
        self.state.confirm_exit(choice)

    def _on_exit_hide(self) -> None:
        """×/Esc 关闭退出确认框 = 「取消」（留在应用，同冲突模态 hide 语义，
        防 pending_exit 滞留导致 sync 不再重建、退出流程卡死）。按钮抉择路径
        先行清空 pending_exit（state.confirm_exit → touch → sync），hide 回调
        看到 None → 不重复处置。"""
        if self.state.pending_exit is not None:
            self.state.confirm_exit("cancel")
        self._open.pop("exit", None)