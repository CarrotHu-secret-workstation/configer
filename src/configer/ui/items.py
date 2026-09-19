"""条目列表面板（§10 U-3）。

- 面板头（固定）：标题 + 当前计数 / 搜索命中数；搜索框在标题之下，是次级
  工具而非最响元素（搜索框仍持久化，不随 refreshable 重建，避免输入丢焦点）；
- 滚动容器（``.pane-scroll``）建在**持久层**（面板头之下），refreshable
  只渲染列表内容：NiceGUI 的 ``<nicegui-refreshable>`` 包装是内容尺寸，
  滚动容器若随 refreshable 重建则拿不到受限高度、长列表不可达（B1）；
  容器不重建 ⇒ 滚动位置在刷新后保持；
- 组/子组折叠树：ui.expansion 嵌套；**折叠默认态（§10 交 M2 实现期定，
  裁量登记）：默认全部收起，仅自动展开包含当前选中条目的组/子组；搜索
  激活时改为平铺过滤结果（全展开）**；展开键持久化在 ``GuiState.open_groups``
  （按文件作用域，见 :func:`_group_key`），刷新重建后导航上下文不丢；
- 短名 = path 末段（logic.short_name）+ 类型标识（logic.type_tag）+
  状态徽标（U-5 推测 / U-8 灰显黄标，logic.badge_kinds），徽标色调统一取
  ``theme.BADGE_TONE``（与 detail 面板共用同一映射，文字即状态）；
- readonly 行只做一次弱化（``.list-row-quiet`` 名称转弱色）；
- 空态：无活动文件 / 加载失败 / 零条目 / 零命中均有 ``.empty-state``。

**关键坑（§7.5）**：提交后 session.doc 整体重载——本模块不缓存任何
ConfigItem 对象引用，每次 refresh 从 state 重取。
"""

from __future__ import annotations

import html as html_mod

from nicegui import ui

from ..model import ConfigItem
from . import logic, theme
from .state import GuiState

__all__ = ["build_items_panel", "items_list"]


def build_items_panel(state: GuiState) -> None:
    """持久化面板：面板头 + 搜索框 + 列表滚动容器。

    B1 修复：滚动容器（``.pane-scroll``）建在**持久层**、refreshable 只是
    它的内容——NiceGUI 的 ``<nicegui-refreshable>`` 包装是内容尺寸，滚动
    容器若建在其内部就拿不到受限高度（overflow:auto 永不出滚动）；且滚动
    容器不随列表刷新重建，滚动位置保持。
    """
    with ui.column().classes("w-full h-full pane-col no-wrap min-h-0"):
        with ui.element("div").classes("pane-head w-full pane-pad-sm"):
            with ui.column().classes("w-full no-wrap").style(theme.gap(2)):
                with ui.row(wrap=False).classes("pane-title-row w-full"):
                    ui.label("条目").classes("t-title")
                    count = ui.label("").classes("t-meta fg-tertiary truncate")
                search = (
                    ui.input(
                        placeholder="搜索：路径 / 说明 子串…",
                        value=state.search_query,
                    )
                    .props(
                        'dense outlined clearable '
                        'aria-label="搜索条目（路径 / 说明 子串）"'
                    )
                    .classes("w-full")
                )
                search.on_value_change(lambda e: _on_search(state, e.value))
                state.search_input = search  # layout 同步用（见 state.py 注释）
        with ui.element("div").classes(
            "pane-scroll pane-col grow min-h-0 w-full pane-pad-sm"
        ):
            items_list(state, count)


def _on_search(state: GuiState, value: str | None) -> None:
    if state.search_syncing:
        return
    state.search_query = value or ""
    items_list.refresh()  # 只刷列表，不 touch 全局（保焦点）


def _open_groups(state: GuiState) -> set[str]:
    """展开键集合（``GuiState.open_groups``；防御缺失时惰性补建）。"""
    keys = getattr(state, "open_groups", None)
    if keys is None:
        keys = set()
        state.open_groups = keys
    return keys


def _group_key(session, *parts: str) -> str:
    """组/子组展开键：按文件作用域，避免不同文件的同名组互相串开合态。"""
    return "::".join((str(session.doc.path), *parts))


def _remember_group(state: GuiState, key: str, opened: bool) -> None:
    """ui.expansion on_value_change 落点（**不** touch：避免重建扰动）。"""
    _open_groups(state)                 # 防御：旧测试替身缺属性时惰性补建
    state.remember_open_group(key, opened)


def _auto_expand_selection(state: GuiState) -> bool:
    """本次刷新是否因**选中变化**自动展开选中条目所在组（N5）。

    ``GuiState.select_item`` / :meth:`~GuiState.activate` 在选中/文件变化时
    清空 ``auto_expanded_item``；items_list 消费一次后写回当前选中——
    同一条目在后续刷新（提交/轮询/搜索清除）不再强制展开，用户手动收起的
    组保持收起。
    """
    selected = state.selected_item
    already = getattr(state, "auto_expanded_item", None)
    return selected is not None and selected != already


@ui.refreshable
def items_list(state: GuiState, count_label) -> None:
    """列表内容（refreshable）：只渲染行/空态；滚动容器在持久层（B1）。"""
    session = state.active_session()
    query = state.search_query.strip()
    tree = logic.build_group_tree(session.doc) if session is not None else []
    matched = (
        [it for g in tree for it in logic.filter_items(g.all_items(), query)]
        if query else []
    )
    _pane_count(count_label, session, query, tree, matched)

    if session is None:
        if state.active_path is None:
            _empty_state(
                "folder_open", "尚未打开文件",
                "点击侧栏 + 按钮，或经命令行 configer open <文件…> 打开。",
            )
        else:
            _empty_state(
                "error_outline", f"{state.active_path.name} 加载失败",
                "在主区查看诊断信息，或点击【重新加载】重试。",
            )
        return
    if query:
        if matched:
            for item in matched:
                _item_row(state, item, query)
        else:
            _empty_state(
                "search_off", "没有匹配的条目",
                f"未找到与“{query}”匹配的路径或说明。",
            )
        return
    if not session.doc.items:
        _empty_state(
            "inbox", "此文件没有可编辑条目",
            "文件已解析，但没有可编辑的配置项。",
        )
        return
    # N5：仅"选中刚变化"的那次刷新自动展开所在组；消费后写回当前选中，
    # 后续刷新不再把用户手动收起的组重新撑开（搜索平铺路径不消费）。
    auto_expand = _auto_expand_selection(state)
    state.auto_expanded_item = state.selected_item
    for group in tree:
        _group_expansion(state, session, group, query, auto_expand)


def _pane_count(count_label, session, query: str, tree, matched) -> None:
    """面板头计数：总数 / 搜索命中数（随 items_list 刷新，不重建元素）。"""
    if session is None:
        count_label.text = ""
    elif query:
        count_label.text = f"命中 {len(matched)} / {len(session.doc.items)} 项"
    else:
        count_label.text = f"共 {len(session.doc.items)} 项"


def _group_expansion(state: GuiState, session, group: logic.GroupNode,
                     query: str, auto_expand: bool) -> None:
    selected_here = state.selected_item in {it.path for it in group.all_items()}
    key = _group_key(session, group.name)
    with ui.expansion(
        text=f"{group.name}（{group.item_count}）",
        icon="folder",
        value=(auto_expand and selected_here) or key in _open_groups(state),
        on_value_change=lambda e, k=key: _remember_group(state, k, e.value),
    ).props("dense").classes("w-full"):
        if group.description:
            ui.label(group.description).classes(
                "t-meta fg-tertiary break-words"
            ).style(theme.pad(1, 2))
        for sub in group.subgroups:
            if sub.name is None:
                for item in sub.items:
                    _item_row(state, item, query)
                continue
            sub_selected = state.selected_item in {it.path for it in sub.items}
            sub_key = _group_key(session, group.name, sub.name)
            with ui.expansion(
                text=sub.name,
                icon="subdirectory_arrow_right",
                value=(auto_expand and sub_selected)
                or sub_key in _open_groups(state),
                on_value_change=lambda e, k=sub_key: _remember_group(state, k, e.value),
            ).props("dense").classes("w-full"):
                for item in sub.items:
                    _item_row(state, item, query)


def _empty_state(icon: str, title: str, hint: str) -> None:
    """面板空态（图标 + 标题 + 一句说明）。"""
    with ui.element("div").classes("empty-state w-full"):
        ui.icon(icon).classes("empty-state-icon")
        ui.label(title).classes("t-body fg-secondary")
        ui.label(hint).classes("t-meta fg-tertiary break-words")


def _item_row(state: GuiState, item: ConfigItem, query: str) -> None:
    selected = state.selected_item == item.path
    classes = "list-row w-full" + (" list-row-active" if selected else "")
    if item.readonly:
        classes += " list-row-quiet"  # U-8：单一弱化（名称转弱色）
    # S8：roving tabindex——仅选中行可 Tab 达（焦点环 .focusable-row），
    # 方向键仍是主路径；aria-current 让当前选中对辅助技术可见。
    # 行内不挂 role=option（本列表是折叠树，不是 listbox）。
    row = (
        ui.row()
        .classes(classes + " focusable-row")
        .props(f'tabindex="{"0" if selected else "-1"}"')
        .on("click", lambda: state.select_item(item.path))
    )
    if selected:
        row.props('aria-current="true"')
    with row:
        _highlighted_name(item, query)
        for kind in logic.badge_kinds(item):
            ui.label(logic.BADGE_TEXT[kind]).classes(
                theme.BADGE_TONE.get(kind, "badge")
            )
        tag = logic.type_tag(item)
        if tag:
            ui.label(tag).classes("badge")
    row.tooltip(item.path)


def _highlighted_name(item: ConfigItem, query: str) -> None:
    """短名 + 命中高亮（U-3）；搜索态显示完整 path 便于定位。"""
    text = item.path if query else logic.short_name(item.path)
    parts = logic.highlight_parts(text, query)
    markup = "".join(
        f"<mark>{html_mod.escape(seg)}</mark>" if hit else html_mod.escape(seg)
        for seg, hit in parts
    )
    ui.html(f'<span class="t-mono">{markup}</span>').classes(
        "list-row-name grow min-w-0 truncate"
    )