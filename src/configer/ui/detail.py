"""详情面板（§10 U-4 / U-5 推测徽标 / U-6 编辑控件 / U-8 状态标志展示侧）。

阅读顺序（R2 层级重排，内容（值）是主角、chrome 退后）：条目头 →
**当前值**（主控件 + 就地门控反馈）→ 约束 → 说明全文 → 标记与依据。
证据原文放在**默认展开**的“依据原文”折叠区里（渐进呈现，但 required 的
警示与依据绝不默认收起）。

- 完整 path + 状态徽标（U-8 readonly 灰显 / dormant·warning 黄标，U-5
  inferred range/enum 一律带"推测"字样徽标；色调统一 theme.BADGE_TONE，
  文字即状态、颜色只是强化）；
- 当前值（:func:`build_value_view`，G2a 已按 U-6 类型映射换装编辑控件：
  bool→开关即提 / int·float·str→文本输入 400ms 防抖 / 枚举→下拉
  （inferred 允许自定义输入，YC-6）/ readonly→灰显禁改；门控红黄标就地
  反馈，编排逻辑在 ui/commit.py 与 GuiState）。门控类名仍由
  commit.marker_class 给出（.gate-error/.gate-warn，钩子不变）；R2 起
  容器不再画第二层框（theme 追加规则改为给输入控件染边），红/黄信号 =
  颜色 + 图标 + 原因文字三重；
- 范围（含 unit）与枚举候选（value +（label）+（声明）后缀 + provenance
  徽标）；
- 说明全文：python 按 Section.label（作用/影响/建议）分段 + 前导段
  （label=None）；yaml 单段（§3.3/YC-1）；CJK 正文 break-words +
  whitespace-pre-wrap，纯 mono 路径/字面量仍可任意断行；
- dormant/warning 原文依据（dormant_reason_text / warning_reason_text，
  §3.5"必须展示"）+ dormant 提示文案"当前不生效（休眠配置）"（U-8）；
- readonly 原因中文映射（logic.readonly_reason_zh，§3.5）。

**关键坑（§7.5）**：提交后 session.doc 整体重载，条目对象引用失效——
本模块每次 refresh 经 state.selected_config_item() 按 path 重取。
"""

from __future__ import annotations

from typing import Callable

from nicegui import ui

from ..model import ConfigItem
from . import commit, logic, theme
from .commit import GateFeedback
from .state import GuiState

__all__ = ["detail_view", "build_value_view"]


@ui.refreshable
def detail_view(state: GuiState) -> None:
    session = state.active_session()
    if session is None:
        return
    item = state.selected_config_item()
    if item is None:
        with ui.element("div").classes("empty-state w-full"):
            ui.icon("touch_app").classes("empty-state-icon")
            ui.label("未选中条目").classes("t-body fg-secondary")
            ui.label("在左侧列表点击条目查看说明全文与约束。").classes(
                "t-meta fg-tertiary"
            )
        return

    with ui.column().classes("w-full pane-pad no-wrap").style(theme.gap(3)):
        _header(item)
        build_value_view(state, item)
        _constraints(item)
        _description(item)
        _flags(item)


def _header(item: ConfigItem) -> None:
    with ui.column().classes("detail-section w-full no-wrap"):
        ui.label(item.path).classes("t-mono fg-primary break-all")
        with ui.row(wrap=True).classes("items-center").style(theme.gap(1)):
            tag = logic.type_tag(item)
            if tag:
                ui.label(f"类型: {tag}").classes("badge")
            for kind in logic.badge_kinds(item):
                ui.label(logic.BADGE_TEXT[kind]).classes(
                    theme.BADGE_TONE.get(kind, "badge")
                )


def build_value_view(state: GuiState, item: ConfigItem) -> None:
    """U-6 类型控件映射 + 门控反馈（G2a 换装完成）。

    控件矩阵（commit.control_kind）：

    - ``readonly``：灰显只读展示 raw_literal + 禁改（U-8；原因文案在 _flags）；
    - ``bool``：开关——值变化即提交（点击类，无防抖，§7.5/A-13）；
    - ``enum``：下拉——候选选中即提交（点击类）；inferred 允许自定义输入
      （YC-6，``new_value_mode='add-unique'``），自定义值走文本类防抖路径；
      declared 仅限列表内（防御实现，金样本无）；候选带"推测"徽标与 label；
    - ``text``（int/float/str）：单行文本输入——键击 → 门控暂态即时反馈 +
      400ms 防抖提交；blur 立即 flush（§7.5 第 3 步）；有 range → 旁侧提示
      （单边界只显示存在侧，inferred 带"推测"徽标）；**不钳制**。

    红/黄标：控件包在 gate 容器内（.gate-error/.gate-warn，theme 定义），
    键击后**就地**切换类名与原因行（不整体 refresh，防输入丢焦点）；
    dormant/warning 持续黄标（U-8）。提交成功后 state.touch() → 本面板
    整体重建，按 path 重取条目刷新显示值（raw_literal 已变，§7.5）。
    """
    kind = commit.control_kind(item)
    session = state.active_session()
    file_gone = session is not None and bool(
        getattr(session, "gone_readonly", False))
    if file_gone:
        kind = "readonly"   # §7.6 gone 态：该文件全部条目转只读（不可编辑）
    with ui.column().classes("detail-section w-full no-wrap"):
        with ui.row(wrap=True).classes("items-baseline w-full").style(
                theme.gap(2)
            ):
            ui.label("当前值").classes("t-label fg-tertiary")
            hint = commit.range_hint_text(item)
            if hint and kind in ("text", "enum"):
                ui.label(hint).classes("t-meta fg-secondary")
                if item.range is not None and item.range.provenance == "inferred":
                    ui.label("推测").classes("badge badge-inferred")  # U-5 并存
        if file_gone and not item.readonly:
            with ui.row(wrap=False).classes(
                "banner banner-error w-full items-start"
            ):
                ui.icon("error").classes("icon-sm")
                ui.label(
                    "文件已被删除、移动或不可读——暂不可编辑（只读态，§7.6）；"
                    "文件恢复或点击横幅【重新加载】后解除。"
                ).classes("t-meta grow break-words")
        if kind == "readonly":
            _readonly_control(item)
        elif kind == "bool":
            _bool_control(state, item)
        elif kind == "enum":
            _enum_control(state, item)
        else:
            _text_control(state, item)


def _readonly_control(item: ConfigItem) -> None:
    """U-8：readonly 灰显禁改（只读展示，不进入提交流程，§7.5）。"""
    ui.label(item.raw_literal).classes("t-mono fg-tertiary code-box w-full")


def _gate_section(
    state: GuiState, item: ConfigItem, build_control: Callable[[], None]
) -> Callable[[GateFeedback | None], None]:
    """门控容器 = 控件 + 原因行（图标 + 文字）；红/黄 = 类 + 图标 + 文字。

    ``build_control()`` 先把控件渲染进容器，随后追加原因行；返回
    ``apply_inplace(fb)`` 在键击后就地切换标记（不触发整体 refresh，防输入
    丢焦点/光标跳动）。
    """
    fb = state.gate_feedback(item.path)
    marker, reason = commit.combine_marker(fb, item)
    container = ui.element("div").classes(
        "w-full pane-col " + commit.marker_class(marker)
    ).style(theme.gap(1))
    with container:
        build_control()
        with ui.row(wrap=False).classes("items-start w-full").style(
                theme.gap(1)) as reason_row:
            reason_icon = ui.icon("error").classes("icon-sm")
            reason_label = ui.label("").classes("t-meta grow break-words")

    def _apply(new_marker: str, new_reason: str | None) -> None:
        container.classes(remove="gate-error gate-warn")
        cls = commit.marker_class(new_marker)
        if cls:
            container.classes(add=cls)
        reason_row.visible = bool(new_reason)   # 无原因不占位
        reason_label.text = new_reason or ""
        tone = "fg-error" if new_marker == "error" else "fg-warn"
        reason_icon.name = "error" if new_marker == "error" else "warning"
        for el in (reason_icon, reason_label):
            el.classes(remove="fg-error fg-warn", add=tone)

    _apply(marker, reason)

    def apply_inplace(new_fb: GateFeedback | None) -> None:
        new_marker, new_reason = commit.combine_marker(new_fb, item)
        _apply(new_marker, new_reason)

    return apply_inplace


def _text_control(state: GuiState, item: ConfigItem) -> None:
    """int/float/str 单行文本输入：键击防抖 400ms + blur 立即 flush。

    选型裁量（登记）：用 ui.input 而非 ui.number——ui.number 有 locale/
    格式化陷阱（千分位、逗号小数点、step 钳制倾向），且 §8.2 全部数字
    边界（int 拒 3.0、float 收科学计数、中间态不提交、不钳制）核心
    parse_text_input 已实现，UI 只透传文本。
    """
    created: dict[str, object] = {}

    def build() -> None:
        created["field"] = ui.input(value=commit.text_widget_value(item)).props(
            "dense outlined"
        ).classes("t-mono w-full")

    apply_inplace = _gate_section(state, item, build)
    field = created["field"]

    def on_change(e) -> None:
        fb = state.on_text_keystroke(item.path, e.value or "")
        apply_inplace(fb)

    field.on_value_change(on_change)
    # 失焦立即提交，不等防抖（§7.5 第 3 步）
    field.on("blur", lambda *_args: state.flush_debounce_item(item.path))


def _bool_control(state: GuiState, item: ConfigItem) -> None:
    """bool 开关：值变化 → set_pending_bool → accepted 则**立即** commit。"""
    created: dict[str, object] = {}

    def build() -> None:
        created["switch"] = ui.switch(value=bool(item.value))

    apply_inplace = _gate_section(state, item, build)
    switch = created["switch"]

    def on_change(e) -> None:
        result = state.commit_click_now(item.path, checked=bool(e.value))
        if result is None:
            # 未落盘（门控/gone）：重建面板回退开关显示 + 就地标记
            apply_inplace(state.gate_feedback(item.path))
            state.touch()

    switch.on_value_change(on_change)


def _enum_control(state: GuiState, item: ConfigItem) -> None:
    """枚举下拉（YC-6）：inferred 允许自定义输入；declared 仅限列表内。

    方案裁量（登记）：NiceGUI 3.16 ui.select 原生支持 ``new_value_mode=
    'add-unique'``（隐含 with_input）——输入回车即新增选项并选中，无需
    下拉+文本框组合。候选选中 = 点击类即提；自定义输入（不在候选 key
    集）= 文本类防抖路径（回车为离散确认，仍按任务书走 400ms 防抖）。
    """
    mode = commit.enum_mode(item)
    candidate_keys: set[str] = set()
    options: dict[str, str] = {}
    for cand in item.enum_candidates:
        key = str(cand.value)
        candidate_keys.add(key)
        options[key] = logic.enum_option_text(cand)
    current = "" if item.value is None else str(item.value)
    if current not in options:
        # 防御：现值不在候选内（如 W-INFER-CONFLICT 条目）→ 增补当前值项
        options[current] = f"{current}（当前值）"

    created: dict[str, object] = {}

    def build() -> None:
        with ui.row(wrap=False).classes("items-center w-full").style(
                theme.gap(1)):
            created["select"] = ui.select(
                options,
                value=current,
                new_value_mode="add-unique" if mode == "inferred" else None,
            ).props("dense outlined").classes("t-mono grow min-w-0")
            if mode == "inferred":
                ui.label("推测").classes("badge badge-inferred")  # U-5/C-1

    apply_inplace = _gate_section(state, item, build)
    select = created["select"]

    def on_change(e) -> None:
        key = e.value
        if key is None:
            return
        if key in candidate_keys:
            # 候选选中：点击类即提（§7.5/A-13）
            result = state.commit_click_now(item.path, text=str(key))
            if result is None:
                apply_inplace(state.gate_feedback(item.path))
                state.touch()
        else:
            # inferred 自定义输入：文本类防抖路径（YC-6 允许列表外值）
            fb = state.on_text_keystroke(item.path, str(key))
            apply_inplace(fb)

    select.on_value_change(on_change)


def _constraints(item: ConfigItem) -> None:
    rng_text = logic.range_text(item.range)
    if not rng_text and not item.enum_candidates:
        return
    with ui.column().classes("detail-section w-full no-wrap"):
        ui.label("约束").classes("t-label fg-tertiary")
        if rng_text:
            with ui.row(wrap=True).classes("items-center").style(theme.gap(1)):
                ui.label(f"范围: {rng_text}").classes("t-meta fg-secondary")
                if item.range is not None and item.range.provenance == "inferred":
                    ui.label("推测").classes("badge badge-inferred")  # U-5/C-1
        if item.enum_candidates:
            with ui.row(wrap=True).classes("items-center").style(theme.gap(1)):
                ui.label("枚举候选:").classes("t-meta fg-secondary")
                if any(
                    c.provenance == "inferred" for c in item.enum_candidates
                ):
                    ui.label("推测").classes("badge badge-inferred")  # U-5/C-1
            for cand in item.enum_candidates:
                suffix = "" if cand.provenance != "declared" else "（声明）"
                ui.label(f"· {logic.enum_option_text(cand)}{suffix}").classes(
                    "t-meta fg-secondary break-words"
                ).style("padding-left: " + theme.space(3))


def _description(item: ConfigItem) -> None:
    if not item.description:
        return
    with ui.column().classes("detail-section w-full no-wrap"):
        ui.label("说明").classes("t-label fg-tertiary")
        for sec in item.description:
            if sec.label:
                ui.label(sec.label).classes("t-label fg-secondary").style(
                    "padding-top: " + theme.space(1))
            ui.label(sec.text).classes(
                "t-body fg-primary whitespace-pre-wrap break-words"
            )


def _flags(item: ConfigItem) -> None:
    """U-8/§3.5：状态标志的原文依据（悬停/详情面板必须展示）。

    依据原文放在“依据原文”折叠区内但**默认展开**——渐进呈现不牺牲可见性：
    required 警示（休眠提示 / 警告标记）始终在折叠区标题之外单独成行。
    """
    has_flags = bool(
        item.readonly
        or item.dormant
        or (item.warning and item.warning_reason_text)
    )
    if not has_flags:
        return
    with ui.column().classes("detail-section w-full no-wrap"):
        ui.label("标记与依据").classes("t-label fg-tertiary")
        if item.readonly:
            reason = logic.readonly_reason_zh(item.readonly_reason)
            with ui.row(wrap=True).classes("items-center").style(theme.gap(1)):
                ui.icon("visibility_off").classes("icon-sm fg-tertiary")
                ui.label(f"只读原因: {reason}").classes("t-meta fg-secondary")
        if item.dormant:
            with ui.row(wrap=False).classes(
                "banner banner-warn w-full items-center"
            ):
                ui.icon("bedtime").classes("icon-sm")
                ui.label(logic.DORMANT_HINT).classes("t-label")
            if item.dormant_reason_text:
                _evidence(item.dormant_reason_text)
        if item.warning and item.warning_reason_text:
            with ui.row(wrap=False).classes(
                "banner banner-warn w-full items-center"
            ):
                ui.icon("warning").classes("icon-sm")
                ui.label("警告标记原文:").classes("t-label")
            _evidence(item.warning_reason_text)


def _evidence(text: str) -> None:
    """标记依据原文（默认展开的折叠区；与警示行同列、层级更低）。"""
    with ui.expansion(
        text="依据原文", icon="notes", value=True
    ).props("dense").classes("w-full"):
        ui.label(text).classes("t-meta fg-secondary code-box w-full")