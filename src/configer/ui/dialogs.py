"""G2b 纯决策层：**零 nicegui 依赖**，全部可无头单元测试。

内容（对应规范条目）：

- U-9 横幅状态机：:func:`banner_for_event` / :func:`apply_external_event`
  （'external_modified' / 'gone' / 'recovered' 三事件 → per-file 横幅文案，
  §7.6 两横幅文案**必须可区分**）；
- §7.6 冲突模态：:class:`ConflictRequest` + 文案纯函数
  （:func:`conflict_dialog_texts` / :func:`force_warning_text`——强制覆盖
  二次警示**必须明示"永久丢失外部修改且无法通过撤销找回"**）；
- U-12 关闭文件：:func:`close_plan`（CloseResult → UI 动作分流）；
- §7.7 退出 flush：:func:`flush_exit_plan`（flush_all 结果 → 决策树动作）、
  :func:`blocked_lines_of`（非法暂态清单，"UI 应当列出"）、
  :func:`noninteractive_exit_code`（断连/进程退出安全网，无法交互时的
  降级决策，局限登记见函数 docstring）；
- U-11 快捷键注册表：:func:`keybinding_action` / :func:`action_scope`
  （键 → 动作名 → 派发层；文本输入焦点不劫持编辑类快捷键，G2a 裁量沿用）。

UI 组件层（ui/modals.py / layout.py / sidebar.py）只按本模块产出的动作与
文案渲染、派发；:class:`~configer.ui.state.GuiState` 负责会话调用编排。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # 仅类型检查期导入，运行期零依赖
    from ..core.session import CloseResult, FlushResult
    from .logic import GroupNode

__all__ = [
    "BANNER_MODIFIED",
    "BANNER_GONE",
    "BANNER_READONLY_SUFFIX",
    "BANNER_RECOVERED",
    "CONFLICT_TITLE",
    "CONFLICT_RELOAD_LABEL",
    "CONFLICT_FORCE_LABEL",
    "CONFLICT_CANCEL_EXIT_LABEL",
    "FORCE_CONFIRM_TITLE",
    "FORCE_WARNING",
    "FORCE_CONFIRM_LABEL",
    "FORCE_BACK_LABEL",
    "RELOAD_DONE_NOTIFY",
    "PAUSED_ERROR_TEXT",
    "PAUSED_RESUME_LABEL",
    "CLOSE_CONFIRM_TITLE",
    "CLOSE_DISCARD_LABEL",
    "CLOSE_CANCEL_LABEL",
    "CLOSE_FAILED_TITLE",
    "CLOSE_RETRY_LABEL",
    "EXIT_FAILED_TITLE",
    "EXIT_FORCE_LABEL",
    "EXIT_CANCEL_LABEL",
    "EXIT_BLOCKED_TITLE",
    "EXIT_OK_LABEL",
    "ConflictRequest",
    "CloseAction",
    "ExitPlan",
    "banner_for_event",
    "apply_external_event",
    "banner_row_text",
    "conflict_dialog_texts",
    "force_warning_text",
    "close_item_lines",
    "close_plan",
    "blocked_lines_of",
    "flush_exit_plan",
    "noninteractive_exit_code",
    "keybinding_action",
    "action_scope",
    "GLOBAL_ACTIONS",
    "EDIT_ACTIONS",
    "KEYBOARD_IGNORE_SAFE",
    "nav_paths",
    "nav_move",
]

# ---------------------------------------------------------------------------
# U-9 横幅文案（§7.6：两类横幅文案必须区别；全简体中文）
# ---------------------------------------------------------------------------

BANNER_MODIFIED = "文件已在磁盘上被修改"
BANNER_GONE = "文件已被删除、移动或不可读"
BANNER_READONLY_SUFFIX = "（该文件全部条目已转为只读态）"
BANNER_RECOVERED = "文件已恢复可读，只读态已解除"

# ---------------------------------------------------------------------------
# §7.6 冲突模态 / 强制覆盖二次警示文案
# ---------------------------------------------------------------------------

CONFLICT_TITLE = "提交冲突：文件已被外部修改"
CONFLICT_BODY = "磁盘内容与会话基线不一致，本次写入已被基线校验阻止。请选择处置方式："
CONFLICT_RELOAD_LABEL = "重新加载"
CONFLICT_FORCE_LABEL = "强制覆盖"
CONFLICT_CANCEL_EXIT_LABEL = "取消退出"
CONFLICT_RELOAD_HINT = (
    "重新加载（默认）：丢弃本文件未提交暂态，按磁盘当前内容重载并建立新基线"
    "（外部修改保留）；该文件 undo/redo 栈同时清空。"
)
CONFLICT_FORCE_HINT = "强制覆盖：以本次待写内容覆盖磁盘文件（须二次确认）。"

FORCE_CONFIRM_TITLE = "强制覆盖：二次警示"
# 硬性要求（§7.6）：必须明示"永久丢失外部修改"且"无法通过撤销找回"。
FORCE_WARNING = (
    "强制覆盖将永久丢失磁盘上的外部修改，且被覆盖的外部内容不在本文件"
    "撤销栈内，无法通过撤销找回。"
)
FORCE_CONFIRM_LABEL = "确认覆盖"
FORCE_BACK_LABEL = "返回"

RELOAD_DONE_NOTIFY = "已按磁盘内容重新加载，undo/redo 栈已清空。"

PAUSED_ERROR_TEXT = (
    "自动提交已暂停（冲突未决，暂态已保留）：请点击「处理冲突」选择"
    "「重新加载」或「强制覆盖」后恢复。"
)
PAUSED_RESUME_LABEL = "处理冲突"

# ---------------------------------------------------------------------------
# U-12 关闭文件确认框文案
# ---------------------------------------------------------------------------

CLOSE_CONFIRM_TITLE = "关闭文件：存在无法落盘的未提交暂态"
CLOSE_DISCARD_LABEL = "放弃暂态并关闭"
CLOSE_CANCEL_LABEL = "取消关闭"
CLOSE_FAILED_TITLE = "关闭文件：触发的提交失败（已阻止关闭）"
CLOSE_RETRY_LABEL = "重试"

# ---------------------------------------------------------------------------
# §7.7 退出确认框文案
# ---------------------------------------------------------------------------

EXIT_FAILED_TITLE = "退出 flush：以下文件提交失败"
EXIT_FAILED_HINT = "「仍要退出」将放弃这些文件的未提交暂态，退出码为 3。"
EXIT_FORCE_LABEL = "仍要退出"
EXIT_CANCEL_LABEL = "取消"
EXIT_BLOCKED_TITLE = "退出确认：以下非法输入本就不会落盘，退出即丢弃"
EXIT_OK_LABEL = "退出"


# ---------------------------------------------------------------------------
# U-9 横幅状态机（纯函数）
# ---------------------------------------------------------------------------


def banner_for_event(event: str) -> str | None:
    """外部事件 → 横幅文案（§7.6 两类横幅文案区别；未知事件 → None）。"""
    if event == "external_modified":
        return BANNER_MODIFIED
    if event == "gone":
        return BANNER_GONE + BANNER_READONLY_SUFFIX
    if event == "recovered":
        return BANNER_RECOVERED
    return None


def apply_external_event(
    banners: dict[Any, str], key: Any, event: str
) -> dict[Any, str]:
    """横幅状态机：返回**新**映射（不改入参），key = 文件 Path（None=全局）。

    - ``'external_modified'`` → 置"文件已在磁盘上被修改"横幅（不自动重载、
      不自动覆盖，§7.6；多文件各自横幅，按 key 区分）；
    - ``'gone'`` → 置**区别文案**"文件已被删除、移动或不可读（只读态）"；
    - ``'recovered'`` → **撤**该文件横幅（只读态解除由 session 侧完成；
      恢复提示由 state 层以 toast 给出，不占横幅，裁量登记）；
    - 未知事件 → 原样返回（防御）。
    """
    out = dict(banners)
    if event == "recovered":
        out.pop(key, None)
        return out
    text = banner_for_event(event)
    if text is not None:
        out[key] = text
    return out


def banner_row_text(name: str, text: str) -> str:
    """横幅单行展示文本：``文件名：横幅文案``（多文件各自一行区分）。"""
    return f"{name}：{text}"


# ---------------------------------------------------------------------------
# §7.6 冲突模态请求与文案
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConflictRequest:
    """一次待用户抉择的冲突（GuiState.pending_conflict 的数据形状）。

    ``source`` 决定模态按钮集与抉择后的续流：

    - ``'commit'``：提交/undo/redo 撞冲突（§7.5 第 5 步）——关闭模态 =
      ``pause_auto_commit()`` + 顶部错误条常驻；
    - ``'close'``：关闭文件触发的提交撞冲突（§7.7）——抉择完成前不关闭；
      处置完成后**不自动重试关闭**（用户再点关闭入口，裁量登记）；
    - ``'exit'``：退出 flush 撞冲突（§7.7）——模态含第三按钮「取消退出」；
      抉择完成后继续退出决策树（重新 flush + plan）。
    """

    file: Any                    # 文件 Path（resolve 后）
    file_name: str
    reason: str | None
    source: Literal["commit", "close", "exit"]
    item_path: str | None = None


def conflict_dialog_texts(req: ConflictRequest) -> dict[str, str]:
    """冲突模态全部文案（纯函数便于无头断言，含 A-11 关键语义）。"""
    reason = req.reason or CONFLICT_BODY
    body = f"{req.file_name}：{reason}"
    if req.item_path:
        body += f"（涉及条目：{req.item_path}）"
    return {
        "title": CONFLICT_TITLE,
        "body": body,
        "reload_hint": CONFLICT_RELOAD_HINT,
        "force_hint": CONFLICT_FORCE_HINT,
        "reload": CONFLICT_RELOAD_LABEL,
        "force": CONFLICT_FORCE_LABEL,
        "cancel_exit": CONFLICT_CANCEL_EXIT_LABEL,
    }


def force_warning_text(file_name: str) -> str:
    """强制覆盖二次警示全文（§7.6 硬性要求）。

    必须明示：将**永久丢失**磁盘上的外部修改，且被覆盖内容不在撤销栈内、
    **无法通过撤销找回**（FORCE_WARNING 常量即含两个关键句）。
    """
    return f"{file_name}：{FORCE_WARNING}"


# ---------------------------------------------------------------------------
# U-12 关闭文件分流（CloseResult → UI 动作）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CloseAction:
    """CloseResult 的 UI 动作描述。

    ``kind`` 取值：

    - ``'removed'``：已关闭 → 侧栏移除 + 活动文件切换到相邻/空态；
    - ``'confirm_discard'``：need_confirm（gone 且有暂态）→ 确认框
      【放弃暂态并关闭】/【取消关闭】；
    - ``'conflict_modal'``：撞基线冲突 → §7.6 模态，未决前不关闭；处置
      完成后不自动重试关闭（裁量登记，用户再点关闭入口）；
    - ``'failed_dialog'``：提交失败 → 错误条 + 确认框【重试】/
      【放弃暂态并关闭】/【取消关闭】。
    """

    kind: Literal["removed", "confirm_discard", "conflict_modal", "failed_dialog"]
    lines: tuple[str, ...] = ()


def close_item_lines(items: list[tuple[str, str]]) -> tuple[str, ...]:
    """CloseResult.items → 展示行 ``条目：原因``。"""
    return tuple(f"{item}：{reason}" for item, reason in items)


def close_plan(result: "CloseResult") -> CloseAction:
    """CloseResult.status → UI 动作（§7.7 关闭分流）。"""
    if result.status == "closed":
        return CloseAction(kind="removed")
    if result.status == "need_confirm":
        return CloseAction(kind="confirm_discard",
                           lines=close_item_lines(result.items))
    if result.status == "conflict":
        return CloseAction(kind="conflict_modal",
                           lines=close_item_lines(result.items))
    return CloseAction(kind="failed_dialog",
                       lines=close_item_lines(result.items))


# ---------------------------------------------------------------------------
# §7.7 退出 flush 决策树（纯函数，无头全测）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExitPlan:
    """flush_all 结果的退出决策（:func:`flush_exit_plan` 产出）。

    ``action`` 取值：

    - ``'exit_clean'``：全部 clean → exit_code=0，停服退出；
    - ``'confirm_blocked'``：全 clean 但存在 block 级非法暂态（从未进入
      pending，本就不会落盘）→ 确认框**列出条目**（§7.7 "UI 应当列出"），
      确认后 exit_code 仍 0（非法输入不属落盘失败，裁量登记）；
    - ``'show_conflict'``：有 conflict（**paused 同路径**：其暂态因冲突未决
      滞留，裁量登记）→ §7.6 模态（重新加载/强制覆盖/取消退出）；抉择后
      重新 flush + plan（其余文件可能仍未决）；
    - ``'ask_failed'``：有 failed → 错误条 + 确认框【仍要退出】（该文件
      abandon()，exit_code=3）/【取消】（留在应用）。
    """

    action: Literal["exit_clean", "confirm_blocked", "show_conflict", "ask_failed"]
    exit_code: int = 0
    conflict_file: Any = None          # show_conflict：待抉择文件
    conflict_reason: str | None = None
    failed: tuple[tuple[Any, tuple[str, ...]], ...] = ()   # ask_failed：文件→失败行
    blocked: tuple[str, ...] = ()      # confirm_blocked：条目清单行


def blocked_lines_of(blocked: dict[Any, dict[str, str]]) -> tuple[str, ...]:
    """last_blocked_items 快照 → 展示行 ``文件名 · 条目：原因``（稳定序）。"""
    out: list[str] = []
    for path, items in blocked.items():
        name = getattr(path, "name", str(path))
        for item, reason in items.items():
            out.append(f"{name} · {item}：{reason}")
    return tuple(out)


def flush_exit_plan(
    results: dict[Any, "FlushResult"],
    blocked: dict[Any, dict[str, str]] | None = None,
) -> ExitPlan:
    """退出 flush 决策树（§7.7 / A-15）。优先级：conflict > failed > blocked > clean。"""
    conflicts = [
        (p, r) for p, r in results.items() if r.status in ("conflict", "paused")
    ]
    failed = [(p, r) for p, r in results.items() if r.status == "failed"]
    if conflicts:
        first_path, first = conflicts[0]
        reason: str | None = None
        if first.failures:
            reason = first.failures[0][1]
        return ExitPlan(
            action="show_conflict",
            conflict_file=first_path,
            conflict_reason=reason,
        )
    if failed:
        return ExitPlan(
            action="ask_failed",
            exit_code=3,
            failed=tuple(
                (p, tuple(f"{item}：{why}" for item, why in r.failures))
                for p, r in failed
            ),
        )
    lines = blocked_lines_of(blocked or {})
    if lines:
        return ExitPlan(action="confirm_blocked", exit_code=0, blocked=lines)
    return ExitPlan(action="exit_clean", exit_code=0)


def noninteractive_exit_code(results: dict[Any, "FlushResult"]) -> int:
    """非交互退出路径的降级决策（断连自动停服 / on_shutdown 安全网）。

    **局限登记**：此路径触发时已无可交互客户端（浏览器标签已关 / Ctrl+C），
    §7.6/§7.7 的冲突模态与确认框无法弹出——conflict / failed / paused 一律
    视同用户「仍要退出」（放弃暂态，exit_code=3）；全 clean → 0。
    """
    if all(r.status == "clean" for r in results.values()):
        return 0
    return 3


# ---------------------------------------------------------------------------
# U-11 快捷键注册表（键 → 动作 → 派发层）
# ---------------------------------------------------------------------------

# 全局层动作：即使焦点在文本输入框内也生效（不劫持任何输入语义——
# Ctrl+B/Ctrl+W 与文本编辑无冲突）。由 ignore=() 的 ui.keyboard 派发。
GLOBAL_ACTIONS = ("toggle_sidebar", "close_file")
# 编辑层动作：焦点在文本输入框/多行框内**不劫持**（Ctrl+Z 留给原生文本
# 撤销，G2a 裁量沿用；上下键留给光标移动）。由 ignore=KEYBOARD_IGNORE_SAFE
# 的 ui.keyboard 派发——按钮/树/空白焦点时全局生效。
EDIT_ACTIONS = ("undo", "redo", "nav_prev", "nav_next")
# 编辑层 ui.keyboard 的 ignore 元素标签（NiceGUI 3.16 ignore 参数取值域：
# 'input'/'select'/'button'/'textarea'；不排除 button——工具栏按钮焦点时
# 快捷键应全局生效；select 的自定义输入焦点在内部 input 上，已被覆盖）。
KEYBOARD_IGNORE_SAFE = ("input", "textarea")


def keybinding_action(
    key: str | None,
    *,
    ctrl: bool = False,
    shift: bool = False,
    alt: bool = False,
    meta: bool = False,
) -> str | None:
    """物理键 → 动作名（U-11 注册表；未登记组合 → None）。

    - Ctrl+B → ``toggle_sidebar``（U-1）；
    - Ctrl+W / Ctrl+Shift+W → ``close_file``（U-12；浏览器保留 Ctrl+W 关
      标签页且**无法被页面拦截**——浏览器模式下该键到不了页面属预期降级，
      Ctrl+Shift+W 为 native 模式备用组合，侧栏 × 按钮为兜底入口，裁量
      登记）；
    - Ctrl+Z → ``undo``；Ctrl+Shift+Z → ``redo``（§7.7）；
    - ArrowUp/ArrowDown（无修饰）→ ``nav_prev``/``nav_next``（应当级：
      条目导航；输入框焦点内不派发，见 EDIT_ACTIONS）；
    - alt/meta 参与的组合一律忽略（让位系统/浏览器）。
    """
    if key is None or alt or meta:
        return None
    k = key.lower() if len(key) == 1 else key
    if ctrl:
        if k == "b" and not shift:
            return "toggle_sidebar"
        if k == "w":  # Ctrl+W 与 Ctrl+Shift+W 同动作（见 docstring 降级登记）
            return "close_file"
        if k == "z":
            return "redo" if shift else "undo"
        return None
    if shift:
        return None
    if k == "ArrowUp":
        return "nav_prev"
    if k == "ArrowDown":
        return "nav_next"
    return None


def action_scope(action: str | None) -> str | None:
    """动作 → 派发层：``'global'``（输入焦点内也生效）/ ``'edit'``（输入
    焦点内不劫持）/ None（未登记）。两个 ui.keyboard 实例据此去重。"""
    if action in GLOBAL_ACTIONS:
        return "global"
    if action in EDIT_ACTIONS:
        return "edit"
    return None


# ---------------------------------------------------------------------------
# 上下键条目导航（应当级，U-11）
# ---------------------------------------------------------------------------


def nav_paths(tree: list["GroupNode"]) -> list[str]:
    """组树 → 展示顺序的条目 path 序列（含折叠组内条目——选中即自动展开，
    items_list 已有该行为）。"""
    out: list[str] = []
    for group in tree:
        for sub in group.subgroups:
            for item in sub.items:
                out.append(item.path)
    return out


def nav_move(paths: list[str], current: str | None, delta: int) -> str | None:
    """上/下移动选中条目：尽头**不环绕**（裁量登记）；无选中 → 首/末条。"""
    if not paths:
        return None
    if current is None or current not in paths:
        return paths[0] if delta > 0 else paths[-1]
    idx = paths.index(current) + delta
    idx = max(0, min(len(paths) - 1, idx))
    return paths[idx]
