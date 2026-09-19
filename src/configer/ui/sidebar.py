"""多文件侧栏（§10 U-2 + U-12 关闭入口）。

每项：诊断状态图标（error 红 / warning 黄 / 正常灰）+ 文件名（悬停
tooltip 完整路径）+ 格式徽标（python/yaml）；活动文件高亮；点击切换主区；
加载失败文件（U-10）同样列出，点击后主区显示诊断列表（layout 负责）。

R2 行布局：名称占满可用宽度（flex-1 + 截断），格式徽标与 × 固定行尾——
不再把文件名压成 ``co…``。未加载文件只做**一次**弱化（``.list-row-quiet``，
名称转弱色），替代旧的行/图标/名称三层透明度叠加。活动文件用同一强调色
表达，但不依赖颜色：强调底色 + 前置强调条 + 字重（``.list-row-active``）。
列表自身滚动，底部 "+" 固定在页脚：页脚由 :func:`build_sidebar_panel`
建在滚动容器之外（持久层），长列表下不随滚动消失（S9）。

无障碍：列表 ``role="listbox"``、行内**选项内容** ``role="option"`` +
``aria-selected``（选择对辅助技术可见）；roving tabindex（仅活动文件可
Tab 达，S8）；仅图标按钮（+ / ×）均带 aria-label 且保留 tooltip。

U-12 关闭入口（三处，编排全在 state.request_close_file）：
- 悬停 ×：行尾小按钮（group-hover 显现；保底低透明度常显，裁量登记）；
- 右键菜单：ui.context_menu（NiceGUI 3.16 原生支持，无需降级）；
- Ctrl+W：layout 键盘层派发（浏览器保留键降级登记见 dialogs.keybinding_action）。

gone 态（§7.6）：文件不可读 → link_off 图标 + 置灰 + tooltip 提示只读态。

"+"按钮：zenity 系统文件选择器为主路径（后端 subprocess 拉起，多选 +
适配器扩展名过滤；服务只绑 127.0.0.1、浏览器与后端同机，故后端能弹桌面
对话框并拿到真实绝对路径）。zenity 缺失或无图形环境（DISPLAY /
WAYLAND_DISPLAY 均未设，如远程 SSH）时回退 textarea 路径输入对话框
（拖拽入窗为后续增强，G1 裁量沿用）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from nicegui import ui

from ..registry import default_registry
from . import theme
from .state import FileEntry, GuiState

__all__ = ["build_sidebar_panel", "sidebar_view"]

# 诊断级别 → (Material icon, 文字色调 class；取 theme 语义色，深色同令牌)
LEVEL_ICON: dict[str, tuple[str, str]] = {
    "error": ("error", "fg-error"),
    "warning": ("warning", "fg-warn"),
    "ok": ("description", "fg-tertiary"),
}


@ui.refreshable
def sidebar_view(state: GuiState) -> None:
    """侧栏文件列表（ui.refreshable：state.touch() 时整体重建）。

    只渲染列表内容；滚动容器与固定页脚（"+"）由
    :func:`build_sidebar_panel` 建在持久层（S9）——页脚不再随刷新/滚动
    消失，滚动位置也不因重建重置。
    """
    entries = state.file_entries()
    if not entries:
        with ui.element("div").classes("empty-state w-full"):
            ui.icon("folder_open").classes("empty-state-icon")
            ui.label("尚未打开文件").classes("t-body fg-secondary")
            ui.label("点击下方 + 打开文件。").classes("t-meta fg-tertiary")
        return
    with ui.column().classes("w-full no-wrap").style(theme.gap(1)).props(
        'role="listbox" aria-label="已打开文件"'
    ):
        for entry in entries:
            _file_row(state, entry)


def build_sidebar_panel(state: GuiState) -> None:
    """持久化侧栏外壳：滚动列表（refreshable 内容）+ 固定页脚（"+"）。

    S9：页脚建在滚动容器**之外**（列表自身滚动，页脚恒在底部）；滚动容器
    同样在持久层（B1 同因：NiceGUI 的 refreshable 包装是内容尺寸，滚动
    容器若随刷新重建就拿不到受限高度）。
    """
    with ui.column().classes("w-full h-full pane-col no-wrap min-h-0"):
        with ui.element("div").classes(
            "pane-scroll pane-col grow min-h-0 w-full pane-pad-sm"
        ):
            sidebar_view(state)
        with ui.element("div").classes("pane-foot w-full pane-pad-sm"):
            open_btn = ui.button(
                icon="add", on_click=lambda: _open_files(state)
            ).props('flat dense aria-label="打开文件…"')
            open_btn.classes("icon-btn")
            open_btn.tooltip("打开文件…")


def _file_row(state: GuiState, entry: FileEntry) -> None:
    active = state.active_path == entry.path
    classes = "list-row group" + (" list-row-active" if active else "")
    if not entry.loaded:
        classes += " list-row-quiet"   # 单一弱化：未加载文件名称转弱色
    # S8：role=option 只落在**选项内容**（main）上——× 按钮是其兄弟节点而
    # 非 option 子节点（此前 option 内嵌交互控件，ARIA 非法）；roving
    # tabindex：仅活动文件可 Tab 达（.focusable-row 焦点环），条目树方向键
    # 仍是主路径，不新增连续 Tab 站。
    row = ui.row().classes(classes)
    with row:
        # 点击区（名称/图标）切换活动文件；× 按钮为其兄弟节点——结构隔离
        # 防止点击冒泡双触发（NiceGUI 无原生 stopPropagation，裁量登记）。
        main = (
            ui.row(wrap=False)
            .classes("grow items-center min-w-0 cursor-pointer focusable-row")
            .style(theme.gap(2))
            .props(
                f'role="option" aria-selected="{"true" if active else "false"}" '
                f'tabindex="{"0" if active else "-1"}"'
            )
            .on("click", lambda: state.activate(entry.path))
        )
        with main:
            if entry.gone:
                # §7.6 gone 态：侧栏标识（图标 + 提示）
                ui.icon("link_off").classes("fg-error icon-sm")
            icon, color = LEVEL_ICON.get(entry.level, LEVEL_ICON["ok"])
            ui.icon(icon).classes(f"{color} icon-sm")
            ui.label(entry.name).classes("list-row-name t-body grow truncate")
            if entry.fmt:
                ui.label(entry.fmt).classes("badge t-meta")
        close_btn = ui.button(icon="close").props(
            'flat dense size=xs round aria-label="关闭文件（Ctrl+W）"'
        ).classes(
            "shrink-0 opacity-30 group-hover:opacity-100 hover:opacity-100 "
            "focus-visible:opacity-100"
        )
        close_btn.tooltip("关闭文件（Ctrl+W）")
        close_btn.on_click(lambda: state.request_close_file(entry.path))
        with ui.context_menu():
            ui.menu_item(
                "关闭文件", lambda: state.request_close_file(entry.path)
            )
    tip = str(entry.path)
    if entry.gone:
        tip += "\n文件已被删除、移动或不可读——只读态（§7.6）"
    row.tooltip(tip)  # U-2：悬停显示完整路径


# -- "+"入口：zenity 系统文件选择器（主路径） --------------------------------
#
# 输出分隔符用换行：文件名可含空格/竖线等字符，而 zenity 输出的绝对路径
# 本身不含换行，换行是最安全的分隔符。起始目录记忆为模块级可变状态
# （简单优先：不跨进程持久化，重开应用回到默认目录）。
_PICKER_SEPARATOR = "\n"
# communicate 兜底超时（秒）：zenity 挂死（窗口失联等）不让协程永久滞留
_PICKER_TIMEOUT: float = 600.0
_last_pick_dir: Path | None = None   # 上次选择所在目录（下次 zenity 起步处）


def _picker_available() -> bool:
    """zenity 系统文件选择器是否可用（False → 调用方回退 textarea 对话框）。

    缺 zenity 二进制（远程/容器环境）或无图形环境（DISPLAY 与
    WAYLAND_DISPLAY 均未设，如 SSH 无 X 转发）都判不可用——后端弹不出
    桌面对话框，回退手写路径不退化。
    """
    if shutil.which("zenity") is None:
        return False
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _config_file_filters() -> list[str]:
    """zenity ``--file-filter`` 参数值列表（``名称 | 模式…``）。

    扩展名从适配器注册表聚合（:func:`configer.registry.default_registry`
    为唯一来源，新适配器注册后过滤器自动跟进）；另加「所有文件」兜底——
    auto 模式还有内容嗅探路径（§4.2），不应禁止选择任意文件。聚合结果
    为空（空注册表）时只返回「所有文件」，不出无模式的 ``配置文件 | ``。
    """
    exts = sorted({
        ext.lower()
        for adapter in default_registry().adapters
        for ext in adapter.extensions
    })
    if not exts:
        return ["所有文件 | *"]   # 空注册表防御：不产出无模式过滤器
    patterns = " ".join(f"*{ext}" for ext in exts)
    return [f"配置文件 | {patterns}", "所有文件 | *"]


def _picker_args(start_dir: Path | None) -> list[str]:
    """zenity 命令行参数（多选 + 换行分隔 + 过滤器 + 可选起始目录）。"""
    args = [
        "zenity", "--file-selection", "--multiple",
        f"--separator={_PICKER_SEPARATOR}",
        "--title=打开文件",
    ]
    args.extend(f"--file-filter={f}" for f in _config_file_filters())
    if start_dir is not None:
        args.append(f"--filename={start_dir}/")   # 尾斜杠 = 目录起步
    return args


def _parse_picker_output(stdout: str) -> list[Path]:
    """拆 zenity stdout 为路径列表（按换行分隔符；去空行/空白）。"""
    return [Path(line.strip()) for line in stdout.splitlines() if line.strip()]


def _open_paths(state: GuiState, paths: list[Path]) -> None:
    """打开一批路径并回执（zenity 主路径与 textarea 回退共用收口，U-2）。

    经 :meth:`GuiState.open_more` 落地（CLI 批次 / 单实例转交 / 失败重载
    同一收口点）；成功 → toast + 激活首个成功者，失败 → 逐文件 negative
    toast（诊断首条）。通知走 :meth:`GuiState.notify`（layout 注入
    ui.notify；无头测试不注入 → 静默）。
    """
    results = state.open_more(paths)
    ok = [r for r in results if r.session is not None]
    bad = [r for r in results if r.session is None]
    for r in bad:
        first = r.diagnostics[0].message if r.diagnostics else "未知错误"
        state.notify(f"{r.path.name} 加载失败：{first}", "negative")
    if ok:
        state.notify(f"已打开 {len(ok)} 个文件", "positive")
        state.activate(ok[0].path)


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    """kill 子进程并 await 回收（超时兜底用，防僵尸进程）。

    kill 在进程恰好已退出的竞态下抛 ProcessLookupError（OSError 子类），
    吞掉即可；wait 同样不让异常外抛——超时路径已决定放弃该子进程，兜底
    分支自身不能再向调用方抛异常。
    """
    try:
        proc.kill()
    except OSError:
        pass
    try:
        await proc.wait()
    except OSError:
        pass


async def _open_via_picker(state: GuiState) -> None:
    """主路径：弹 zenity 系统文件选择器（异步子进程，不阻塞事件循环）。

    退出码 0 → stdout 按分隔符拆路径走 :func:`_open_paths`；退出码 1
    （用户取消）或输出为空 → 静默；其他退出码 / 启动失败（which 检查后
    二进制又被移走等瞬态）→ negative toast；communicate 限时
    :data:`_PICKER_TIMEOUT`（10 分钟），超时 → kill + 回收 + negative
    toast。选择成功后记住所选目录，下次 ``--filename`` 从那里起步。
    """
    global _last_pick_dir
    try:
        proc = await asyncio.create_subprocess_exec(
            *_picker_args(_last_pick_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        state.notify("无法启动系统文件选择器（zenity）。", "negative")
        return
    try:
        stdout, _err = await asyncio.wait_for(
            proc.communicate(), timeout=_PICKER_TIMEOUT
        )
    except asyncio.TimeoutError:
        await _kill_and_reap(proc)
        state.notify("文件选择器无响应，已关闭。", "negative")
        return
    if proc.returncode == 1:
        return   # 用户取消：静默结束
    if proc.returncode != 0:
        state.notify(
            f"文件选择器异常退出（码 {proc.returncode}）。", "negative")
        return
    paths = _parse_picker_output(os.fsdecode(stdout))
    if not paths:
        return   # 空输出视同取消：静默
    _last_pick_dir = paths[0].parent
    _open_paths(state, paths)


async def _open_files(state: GuiState) -> None:
    """"+"按钮入口：zenity 可用走系统文件选择器，否则回退 textarea 对话框。

    NiceGUI 对返回 coroutine 的同步 handler 会隐式 await（3.16 契约），
    按钮的 ``on_click=lambda: _open_files(state)`` 写法成立。
    """
    if _picker_available():
        await _open_via_picker(state)
    else:
        _open_dialog(state)


def _parse_text_paths(raw: str) -> list[Path]:
    """textarea 回退输入解析：逐行 strip、跳过空行/空白行、``~`` 展开。

    提为模块级纯函数以便直测（与 :func:`_parse_picker_output` 同风格）；
    空输入返回空列表，调用方（回退对话框的 _confirm）据此早退、不触发
    :func:`_open_paths`。
    """
    return [
        Path(line.strip()).expanduser()
        for line in raw.splitlines()
        if line.strip()
    ]


def _open_dialog(state: GuiState) -> None:
    """U-2 "+"入口回退方案：路径输入对话框（zenity 不可用时，见模块 docstring）。"""
    with ui.dialog() as dialog, ui.card().classes("dialog-card"):
        with ui.row(wrap=False).classes("dialog-head w-full"):
            ui.icon("folder_open").classes("icon-lg fg-secondary shrink-0")
            ui.label("打开文件").classes("t-title")
        ta = ui.textarea(
            placeholder="文件路径，每行一个（支持 ~ 与相对路径）"
        ).classes("w-full")
        with ui.row().classes("dialog-actions w-full"):
            # 取消/返回类 == 静默次级（color=None，与 modals 四框同一约定）
            ui.button("取消", color=None, on_click=dialog.close).props(
                "flat no-caps")

            def _confirm() -> None:
                raw = ta.value or ""
                dialog.close()
                paths = _parse_text_paths(raw)
                if not paths:
                    return
                _open_paths(state, paths)

            ui.button("打开", on_click=_confirm).props(
                "unelevated color=primary no-caps"
            )
    dialog.open()