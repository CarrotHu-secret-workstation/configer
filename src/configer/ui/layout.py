"""IDE 布局（§10 U-1）与主区组装 + G2b 全局接线（快捷键/模态宿主/横幅）。

视觉全部经 :mod:`configer.ui.theme` 令牌表达（颜色/字号/间距/圆角/阴影/
动效）；本模块不出现一次性色值与魔法像素值。

布局树（组件树，M2 验收 A-12 对应）::

    .app-shell（满高 flex 列：100dvh，100vh 回退；滚动一律在面板内）
    ├─ header.app-toolbar（唯一半透明层：sticky + backdrop blur + 发丝线）
    │  ├─ .app-toolbar-group（侧栏 toggle + 品牌名）
    │  ├─ .app-toolbar-group（undo/redo）
    │  ├─ .app-toolbar-feedback（提交回显 + 提交错误条, 各自 refreshable）
    │  ├─ .app-toolbar-spacer
    │  └─ 退出按钮（.app-exit：静默次级，仅 hover/focus 转红）
    ├─ .app-banner-strip（U-9 外部修改/gone 横幅，per-file 各一行，refreshable；
    │                      role=status + aria-live=polite）
    └─ main.app-main（应用底色 + 统一内边距；面板是内容表面）
       └─ splitter(左右分割, grow)                    ← U-1 分隔条拖拽调宽
          ├─ splitter.before → 侧栏面板.pane（sidebar.build_sidebar_panel：
          │                         持久滚动容器 + sidebar_view refreshable
          │                         + 固定页脚 +；× 关闭/右键菜单 U-12）
          └─ splitter.after → .main-region（列）
             ├─ file_header_view（活动文件名/格式徽标/文档级说明/诊断按钮, U-2/U-4/U-10）
             ├─ failed_panel（加载失败诊断列表, refreshable, U-10）
             ├─ empty_panel（空态：无活动文件；全部关闭后回到此态，U-12）
             └─ editor_splitter(左右分割, grow)      ← 条目树 | 详情
                ├─ items.build_items_panel（搜索框持久 + items_list refreshable, U-3）
                └─ detail.detail_view（refreshable, U-4/U-5/U-6/U-8）

- 模态：modals.ModalHost 持久容器（refresh_all 每次 sync——数据驱动，
  pending_* 字段出现即弹、清空即关，§7.6/U-12/§7.7）。
- 快捷键（U-11）：两个 ui.keyboard 层——全局层 ignore=()（Ctrl+B/Ctrl+W，
  输入焦点内也生效）；编辑层 ignore=('input','textarea')（Ctrl+Z/
  Ctrl+Shift+Z/上下键，**文本输入焦点内不劫持**：Ctrl+Z 留给原生文本撤销，
  G2a 裁量沿用）。注册表纯函数见 dialogs.keybinding_action。
- 刷新模型：``state.touch()`` → ``ui_refresh``（本模块注入）→ 各
  refreshable 整体刷新 + 面板可见性/搜索框同步 + 模态 sync；ui.timer
  消费外部事件队列（轮询线程 → 主循环）、消费单实例转交批次
  （state.process_handoffs，U-13/U-10）并按公开 API 补绑新会话回调
  （state.ensure_state_callbacks 安全网，G2c 正规化）+ 同步 beforeunload
  守卫标志。
"""

from __future__ import annotations

from nicegui import core, ui

from . import dialogs, logic, theme
from .detail import detail_view
from .items import build_items_panel, items_list
from .modals import ModalHost
from .sidebar import build_sidebar_panel, sidebar_view
from .state import GuiState

__all__ = ["build_gui"]

# beforeunload 守卫（尽力而为，裁量登记）：仅当存在"退出会丢失"的状态
# （防抖计时中 / 未决暂态 / paused）时浏览器才弹原生离开确认；服务端
# 检测到全部客户端断开后走非交互 flush 再停服（app.py，局限见 exit
# 决策 dialogs.noninteractive_exit_code docstring）。
_BEFOREUNLOAD_JS = """
<script>
window.addEventListener("beforeunload", function (e) {
  if (document.body.dataset.configerGuard === "1") {
    e.preventDefault();
    e.returnValue = "";
  }
});
</script>
"""


def _safe_refresh(view) -> None:
    """refreshable 刷新（无头防御）。

    NiceGUI ``refreshable.refresh()`` 依赖运行中的事件循环
    （``core.loop``，后台任务调度）；无头构建/测试时无循环，跳过即可——
    refreshable 首次调用已完成初渲染，运行期（ui.run 内）循环必然存在。
    """
    if core.loop is None:
        return
    try:
        view.refresh()
    except AssertionError:  # 竞态兜底：loop 刚被关闭
        pass


def build_gui(state: GuiState) -> None:
    """构建 auto-index 根页面（run_gui 在 ui.run 前调用一次）。"""
    theme.install()
    ui.add_body_html(_BEFOREUNLOAD_JS)

    # 模态宿主建在页面根下（对话框挂在它下面并 teleport 到 body）：既不被
    # refreshable 整体刷新销毁（见 modals.py），也不受壳层 flex/裁切影响。
    modal_host = ModalHost(state)
    state.modal_host = modal_host   # 测试/诊断可见（open_keys）

    with ui.element("div").classes("app-shell"):
        # -- 工具栏（唯一半透明层；控件按相关性分组，顺序与既有一致） ----------
        with ui.element("header").classes("app-toolbar"):
            with ui.row().classes("app-toolbar-group"):
                # 工具栏/横幅/错误条按钮一律显式 color=None（S1）：NiceGUI 默认
                # color='primary' 会给 flat 按钮挂 Quasar 的 text-primary
                # （!important 层），压过本表 `.app-toolbar .q-btn` /
                # `.banner .q-btn` / `.feedback-chip .q-btn` 规则。
                toggle_btn = ui.button(icon="menu", color=None).props(
                    "flat dense aria-label=\"收起侧栏（Ctrl+B）\""
                ).classes("icon-btn")
                toggle_btn.tooltip("收起侧栏（Ctrl+B）")
                ui.label("configer").classes("t-title")
            with ui.row().classes("app-toolbar-group"):
                # U-7：每文件独立 undo/redo（禁用态跟随栈深度；Ctrl+Z/
                # Ctrl+Shift+Z 由下方键盘层派发同一 state.do_undo/do_redo）
                undo_btn = ui.button(icon="undo", color=None).props(
                    "flat dense aria-label=\"撤销（Ctrl+Z；文本输入焦点内留给原生撤销）\""
                ).classes("icon-btn")
                undo_btn.tooltip("撤销（Ctrl+Z；文本输入焦点内留给原生撤销）")
                undo_btn.on_click(lambda: state.do_undo())
                redo_btn = ui.button(icon="redo", color=None).props(
                    "flat dense aria-label=\"重做（Ctrl+Shift+Z）\""
                ).classes("icon-btn")
                redo_btn.tooltip("重做（Ctrl+Shift+Z）")
                redo_btn.on_click(lambda: state.do_redo())
            with ui.row().classes("app-toolbar-feedback"):
                echo_view(state)
                commit_error_view(state)
            ui.element("div").classes("app-toolbar-spacer")
            # 退出是破坏性动作：`.app-exit` 静默次级（tertiary），仅 hover/
            # focus 转 danger 色（color=None 的根因见上方工具栏注释）。
            exit_btn = ui.button(icon="logout", color=None).props(
                "flat dense aria-label=\"退出 configer（退出前 flush，§7.7）\""
            ).classes("icon-btn app-exit")
            exit_btn.tooltip("退出 configer（退出前 flush，§7.7）")
            exit_btn.on_click(lambda: state.begin_exit())

        # -- 横幅条（U-9；无横幅时零高） ---------------------------------------
        with ui.column().classes("app-banner-strip w-full no-wrap").props(
            'role="status" aria-live="polite"'
        ):
            banner_view(state)

        # -- 主区：应用底色 + 面板表面（滚动一律在面板内） ----------------------
        with ui.element("main").classes("app-main"):
            # 布局回归修复（统筹方终验）：NiceGUI/Quasar 的 horizontal=True 是
            # **上下分割**（面板竖向叠放，类 q-splitter--horizontal column，面板
            # 高度=容器高度×value%，容器 auto 高度时整体塌陷成 1px/0px）；
            # 左右分割必须用默认的 vertical（分隔线竖直、面板按 width% 分宽）。
            # 此前两处误传 horizontal=True，页头下方内容全宽竖排（存档
            # dom.html@修复前已含 2 处 q-splitter--horizontal 可证非本次回归）。
            splitter = ui.splitter(value=state.sidebar_split,
                                   limits=(0, 60)).classes("w-full grow")
            # min-height: 0 与 grow 的 flex 链由 `.app-main > .q-splitter`
            # 主题规则承担（不再内联 style，N3）

            with splitter.before:
                with ui.column().classes(
                    "pane pane-col h-full w-full items-stretch no-wrap"
                ):
                    # 滚动容器与页脚在 build_sidebar_panel 持久层（S9）；
                    # layout 侧 wrapper 不再挂 pane-scroll（B1 同因：刷新
                    # 不重建滚动容器，滚动位置保持）。
                    build_sidebar_panel(state)
            with splitter.after:
                with ui.column().classes("main-region h-full w-full no-wrap"):
                    file_header_view(state)
                    failed_panel = ui.column().classes(
                        "pane pane-pad pane-scroll w-full").style(
                        theme.gap(2))
                    with failed_panel:
                        failed_view(state)
                    empty_panel = ui.element("div").classes("empty-slot w-full")
                    with empty_panel:
                        with ui.column().classes("empty-state w-full"):
                            ui.icon("folder_open").classes("empty-state-icon")
                            ui.label("尚未打开文件").classes("t-body fg-secondary")
                            ui.label(
                                "点击侧栏“+”按钮，或经命令行 "
                                "configer open <文件…> 打开。"
                            ).classes("t-meta fg-tertiary")
                    editor_splitter = ui.splitter(value=50,
                                                  limits=(20, 80)).classes(
                        "w-full grow")
                    with editor_splitter.before:
                        with ui.column().classes(
                            "pane pane-col h-full w-full items-stretch no-wrap"
                        ):
                            build_items_panel(state)
                    with editor_splitter.after:
                        with ui.column().classes(
                            "pane pane-col pane-scroll h-full w-full "
                            "items-stretch no-wrap"
                        ):
                            detail_view(state)

    # -- 收起/展开（U-1） -----------------------------------------------------
    def apply_sidebar() -> None:
        if state.sidebar_visible:
            splitter.value = state.sidebar_split
            splitter.classes(remove="sidebar-collapsed")
            toggle_btn.icon = "menu"
            toggle_btn.tooltip("收起侧栏（Ctrl+B）")
            toggle_btn.props(add='aria-label="收起侧栏（Ctrl+B）"')
        else:
            if splitter.value:
                state.sidebar_split = float(splitter.value)
            splitter.value = 0.0
            splitter.classes(add="sidebar-collapsed")
            toggle_btn.icon = "menu_open"
            toggle_btn.tooltip("展开侧栏（Ctrl+B）")
            toggle_btn.props(add='aria-label="展开侧栏（Ctrl+B）"')

    toggle_btn.on_click(lambda: state.toggle_sidebar())

    # -- 全局刷新（state.touch 的落点） ---------------------------------------
    def sync_undo_redo() -> None:
        session = state.active_session()
        undo_btn.enabled = session is not None and session.undo_depth() > 0
        redo_btn.enabled = session is not None and session.redo_depth() > 0

    def refresh_all() -> None:
        apply_sidebar()
        for view in (sidebar_view, file_header_view, failed_view, items_list,
                     detail_view, banner_view, commit_error_view, echo_view):
            _safe_refresh(view)
        _sync_main_panels(state, failed_panel, empty_panel, editor_splitter)
        _sync_search_input(state)
        sync_undo_redo()
        modal_host.sync()

    state.ui_refresh = refresh_all
    if state.notify_fn is None:
        state.notify_fn = _ui_notify

    # -- 外部事件消费 + 会话补绑 + beforeunload 守卫（主循环 timer） ------------
    guard_flag = [None]   # 上次同步到浏览器的守卫值（变化才发 JS）

    def drain() -> None:
        state.process_handoffs()           # U-13 转交批次（含 U-10 失败诊断登记）
        if state.ensure_state_callbacks(): # 公开 API 安全网（G2c 正规化）
            refresh_all()                  # 防御：未经登记的会话出现 → 侧栏即时呈现
        state.consume_events()             # 横幅状态机（有事件时内部 touch）
        guard = state.beforeunload_guard()
        if guard != guard_flag[0]:
            guard_flag[0] = guard
            try:
                ui.run_javascript(
                    "document.body.dataset.configerGuard = "
                    f"{'1' if guard else '0'}"
                )
            except Exception:
                pass    # 防御：客户端已断开

    ui.timer(0.5, drain)
    ui.timer(0.5, lambda: state.manager.start_pollers(), once=True)
    # 文本类防抖提交轮询（§7.5 第 3 步：400ms 到期；0.1s 周期 << 400ms，
    # 到期判定由 DebounceManager 时钟负责，poll 空转不刷新界面）
    ui.timer(0.1, state.drain_debounce)

    # -- 快捷键（U-11；注册表纯函数 dialogs.keybinding_action） -----------------
    def make_key_handler(scope: str):
        def handler(e) -> None:
            if getattr(e, "action", None) != "keydown":
                return
            mods = e.modifiers
            action = dialogs.keybinding_action(
                e.key.name,
                ctrl=bool(mods.ctrl), shift=bool(mods.shift),
                alt=bool(mods.alt), meta=bool(mods.meta),
            )
            if dialogs.action_scope(action) != scope:
                return
            _dispatch_key_action(state, action)
        return handler

    # 全局层：Ctrl+B / Ctrl+W——输入焦点内也生效（与文本编辑无冲突）。
    ui.keyboard(make_key_handler("global"), ignore=(), repeating=False)
    # 编辑层：Ctrl+Z / Ctrl+Shift+Z / 上下键——文本输入焦点内**不劫持**
    #（原生文本撤销/光标移动优先，G2a 裁量沿用；按钮/树/空白焦点全局生效）。
    ui.keyboard(make_key_handler("edit"),
                ignore=list(dialogs.KEYBOARD_IGNORE_SAFE), repeating=False)

    # 首帧
    if state.active_path is None:
        entries = state.file_entries()
        if entries:
            state.active_path = entries[0].path
    refresh_all()


def _ui_notify(message: str, kind: str = "positive") -> None:
    """state.notify_fn 默认实现（toast）。"""
    try:
        ui.notify(message, type=kind)
    except Exception:
        pass    # 防御：无客户端上下文（无头）


def _dispatch_key_action(state: GuiState, action: str | None) -> None:
    """快捷键动作派发（dialogs.keybinding_action 的宿主侧执行）。"""
    if action == "toggle_sidebar":
        state.toggle_sidebar()
    elif action == "close_file":
        if state.active_path is not None:
            state.request_close_file(state.active_path)
    elif action == "undo":
        state.do_undo()
    elif action == "redo":
        state.do_redo()
    elif action in ("nav_prev", "nav_next"):
        _nav_item(state, -1 if action == "nav_prev" else 1)


def _nav_item(state: GuiState, delta: int) -> None:
    """上下键条目导航（U-11 应当级）：树展示顺序内移动选中项。

    选中即自动展开所在组（items_list 既有行为）；尽头不环绕（裁量见
    dialogs.nav_move）。select_item 自带旧条目失焦 flush（§7.5 第 3 步）。
    """
    session = state.active_session()
    if session is None:
        return
    tree = logic.build_group_tree(session.doc)
    paths = dialogs.nav_paths(tree)
    target = dialogs.nav_move(paths, state.selected_item, delta)
    if target is not None and target != state.selected_item:
        state.select_item(target)


@ui.refreshable
def banner_view(state: GuiState) -> None:
    """U-9 外部事件横幅（§7.6）：per-file 各一行（多文件区分，裁量登记）。

    - "文件已在磁盘上被修改"：不自动重载、不自动覆盖；提供【重新加载】
      手动入口（= resolve_conflict('reload')，撤销栈清空有 toast 提示）；
    - "文件已被删除、移动或不可读"：**区别文案** + 全部控件只读态
      （detail 层按 session.gone_readonly 渲染）+ 侧栏标识；【重新加载】
      即 §7.6 的"用户重载"恢复入口；
    - 'recovered' 事件撤横幅（state.consume_events，恢复提示走 toast）。
    """
    for key, text in state.banner_lines():
        name = key.name if key is not None else "某文件"
        gone = text.startswith(dialogs.BANNER_GONE)
        tone = "banner-error" if gone else "banner-warn"
        with ui.row().classes(f"banner {tone} w-full items-center no-wrap"):
            # 颜色只是强化：图标 + 文案 + 边框同时区分 error（gone）与 warn
            ui.icon("error" if gone else "warning").classes("icon-sm")
            ui.label(dialogs.banner_row_text(name, text)).classes("grow t-label")
            if key is not None:
                ui.button(
                    "重新加载", color=None, on_click=_reload_file(state, key)
                ).props("flat dense size=sm no-caps")
            dismiss = ui.button(icon="close", color=None).props(
                "flat dense size=sm aria-label=\"关闭提示\"")
            dismiss.on("click", _dismiss_banner(state, key))


def _reload_file(state: GuiState, path):
    def _handler() -> None:
        state.reload_file(path)
    return _handler


def _reload_failed(state: GuiState, path):
    """U-10 失败文件【重新加载】（G2c）：重试打开（open_more 语义）。"""
    def _handler() -> None:
        state.reload_failed(path)
    return _handler


def _dismiss_banner(state: GuiState, key):
    def _handler() -> None:
        state.dismiss_banner(key)
    return _handler


@ui.refreshable
def commit_error_view(state: GuiState) -> None:
    """提交失败/冲突暂停错误条（§7.5 第 5 步 / §7.6 / U-7）。

    - failed：文案 + 【重试】（暂态保留，重试 = 再次 commit_pending）；
    - 冲突模态被关闭（×/Esc）：**常驻**"自动提交已暂停"提示 +
      【处理冲突】恢复入口（重开 §7.6 模态；reload/force 成功即恢复
      自动提交，核心契约），常驻期间**不可手动消除**（§7.6）。
    """
    if not state.commit_error:
        return
    paused_bar = state.commit_error_conflict is not None
    with ui.row().classes(
        "feedback-chip chip-error items-center no-wrap"
    ).props('role="status" aria-live="polite"'):
        ui.icon("error").classes("icon-sm")
        msg = ui.label(state.commit_error).classes("feedback-text t-label")
        msg.tooltip(state.commit_error)   # 窄屏截断时全文仍可达
        if state.commit_error_retry:
            ui.button("重试", color=None, on_click=_retry_commit(state)).props(
                "flat dense size=sm no-caps"
            )
        if paused_bar:
            ui.button(
                dialogs.PAUSED_RESUME_LABEL, color=None,
                on_click=_reopen_conflict(state)
            ).props("flat dense size=sm no-caps")
        else:
            dismiss = ui.button(icon="close", color=None).props(
                "flat dense size=sm aria-label=\"关闭错误提示\"")
            dismiss.on("click", _dismiss_commit_error(state))


def _reopen_conflict(state: GuiState):
    def _handler() -> None:
        if state.commit_error_conflict is not None:
            state.reopen_conflict(state.commit_error_conflict)
    return _handler


def _retry_commit(state: GuiState):
    def _handler() -> None:
        state.retry_commit()   # 结果经 apply_commit_result 分流 + touch
    return _handler


def _dismiss_commit_error(state: GuiState):
    def _handler() -> None:
        state.commit_error = None
        state.commit_error_retry = None
        state.touch()
    return _handler


@ui.refreshable
def echo_view(state: GuiState) -> None:
    """"最近提交回显"（U-7 可选增强，已实现）：`短名: 旧 → 新` 短暂展示。

    裁量登记：不做起时消隐 timer——回显在下一次提交/切换文件时被更新或
    清除（state.last_commit_echo），保持头部安静。
    """
    if not state.last_commit_echo:
        return
    ui.label(state.last_commit_echo).classes(
        "t-meta t-mono fg-tertiary truncate echo-text"
    ).props('role="status" aria-live="polite"')


@ui.refreshable
def file_header_view(state: GuiState) -> None:
    """主区文件头：文件名 + 格式徽标 + 文档级说明 + 诊断入口（U-10）。"""
    session = state.active_session()
    if session is None:
        return
    doc = session.doc
    diags = list(session.diagnostics)
    level = logic.diagnostics_level(diags)
    # N6：flex-wrap: wrap 由主题 .file-header 提供（此前同时挂 no-wrap
    # 工具类，被未分层主题规则压掉，是死类——移除）。
    with ui.row().classes("file-header w-full items-center"):
        ui.label(doc.path.name).classes("t-title truncate")
        ui.badge(doc.format).props("outline color=grey-7")
        if getattr(session, "gone_readonly", False):
            ui.badge("只读（文件不可读）").props("color=red")   # §7.6 gone 态
        if doc.description:
            with ui.element("div").classes("truncate grow"):
                first = doc.description.strip().splitlines()[0]
                lbl = ui.label(first).classes("t-meta fg-secondary truncate")
                lbl.tooltip(doc.description)  # 模块 docstring 全文悬停可见
        else:
            ui.space()
        if diags:
            # S7：诊断入口用状态令牌（原先 color=red/amber 是 Quasar 硬编码，
            # 且 .text-amber #ffc107 在白底仅 1.63:1）；图标 3:1 由
            # --status-error / --status-warn 保证。
            tone = "fg-error" if level == "error" else "fg-warn"
            tip = f"加载诊断 {len(diags)} 条（点击查看）"
            ui.button(
                icon="report", color=None, on_click=_toggle_diagnostics(state)
            ).props(f"flat dense aria-label=\"{tip}\"").classes(
                f"icon-btn {tone}"
            ).tooltip(tip)
    if doc.description:
        # S13：开合态持久化——与条目树组展开共用 state.open_groups（键带
        # 文件作用域，同一 "path::" 命名空间，关闭文件时随 N5 清理一并
        # 移除）；refreshable 重建后不再每次收起。
        doc_key = _file_doc_key(doc)
        with ui.expansion(
            text="文件说明（模块 docstring）", icon="info",
            value=doc_key in state.open_groups,
            on_value_change=lambda e, k=doc_key: state.remember_open_group(
                k, e.value),
        ).props("dense").classes("pane pane-pad-sm file-doc w-full"):
            ui.label(doc.description).classes(
                "t-meta fg-secondary whitespace-pre-wrap"
            )


def _file_doc_key(doc) -> str:
    """文件说明折叠区展开键（path::__file_doc__，见 layout 说明与 state）。"""
    return f"{doc.path}::__file_doc__"


def _toggle_diagnostics(state: GuiState):
    def _handler() -> None:
        state.show_diagnostics = not state.show_diagnostics
        state.touch()
    return _handler


@ui.refreshable
def failed_view(state: GuiState) -> None:
    """加载失败文件的主区：诊断列表 code+message（U-10，不白屏）。

    也承载"加载成功但用户点开诊断按钮"的展示。失败态另提供【重新加载】
    入口（G2c：启动批次/转交中的失败文件可就地重试，U-10）。
    """
    if state.active_path is None:
        return
    session = state.active_session()
    if session is not None and not state.show_diagnostics:
        return
    if session is None:
        diags = state.failed_diagnostics(state.active_path)
        with ui.row().classes("w-full items-center").style(theme.gap(3)):
            ui.label(f"{state.active_path.name} 加载失败").classes(
                "t-body fg-error font-bold"
            )
            ui.button(
                "重新加载", icon="refresh",
                on_click=_reload_failed(state, state.active_path),
            ).props("flat dense size=sm no-caps").tooltip(
                "重新尝试打开该文件（成功后转为正常编辑态）"
            )
    else:
        diags = list(session.diagnostics)
        ui.label(f"{session.doc.path.name} 加载诊断").classes(
            "t-body fg-secondary font-bold"
        )
    for severity, code, message, path in logic.diagnostic_rows(diags):
        color = {
            "error": "fg-error", "warning": "fg-warn",
        }.get(severity, "fg-secondary")
        with ui.row(wrap=False).classes("items-baseline w-full").style(
                theme.gap(2)
            ):
            ui.badge(code).props("outline color=grey-8").classes("shrink-0")
            ui.label(message).classes(f"t-meta {color} break-all")
            if path:
                ui.label(f"@ {path}").classes(
                    "t-meta fg-tertiary t-mono shrink-0"
                )


def _sync_main_panels(state: GuiState, failed_panel, empty_panel,
                      editor_splitter) -> None:
    """按活动文件状态切换主区面板可见性（避免结构重建）。"""
    if state.active_path is None:
        failed_panel.set_visibility(False)
        editor_splitter.set_visibility(False)
        empty_panel.set_visibility(True)
        return
    empty_panel.set_visibility(False)
    session = state.active_session()
    if session is None:
        failed_panel.set_visibility(True)
        editor_splitter.set_visibility(False)
    else:
        failed_panel.set_visibility(bool(state.show_diagnostics))
        editor_splitter.set_visibility(True)


def _sync_search_input(state: GuiState) -> None:
    """活动文件切换后把搜索框同步回 state.search_query（防触发过滤回环）。"""
    el = state.search_input
    if el is None:
        return
    if getattr(el, "value", None) != state.search_query:
        state.search_syncing = True
        try:
            el.value = state.search_query  # type: ignore[attr-defined]
        finally:
            state.search_syncing = False
