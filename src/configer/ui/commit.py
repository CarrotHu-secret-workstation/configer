"""编辑提交编排（§7.5 第 3 步 / §10 U-6 / U-7）——**纯 python，零 nicegui 依赖**。

内容：

- :class:`DebounceManager`：文本类控件（int/float/str/枚举自定义输入）的
  400ms 防抖状态机。计时用**可注入时钟**（毫秒），测试用假时钟推进、
  无 sleep；点击类控件（bool/枚举候选选中）**不经防抖**，直接走
  ``set_pending_* → commit_pending`` 即时提交路径（§7.5 v0.4 决议，A-13）。
- 提交/门控结果 → UI 动作映射纯函数：:func:`commit_action` /
  :func:`pending_feedback`（§7.5 第 2/5 步分级反馈）。
- 控件选型纯函数：:func:`control_kind` / :func:`enum_mode` /
  :func:`range_hint_text` / :func:`marker_style`（U-6/U-8 控件矩阵与
  红黄标样式，无头可测）。

冲突/文件消失（conflict/gone）的模态处置属 G2b：本模块只产出动作
kind，宿主（GuiState）经 ``on_conflict`` / ``on_gone`` 回调占位分流。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Literal

if TYPE_CHECKING:  # 仅类型检查期导入，运行期零依赖核心/模型
    from ..core.session import CommitResult, PendingResult
    from ..model import ConfigItem

__all__ = [
    "DEBOUNCE_MS",
    "DebounceManager",
    "CommitAction",
    "GateFeedback",
    "commit_action",
    "pending_feedback",
    "control_kind",
    "enum_mode",
    "range_hint_text",
    "marker_style",
    "marker_class",
    "combine_marker",
    "text_widget_value",
]

# §7.5 第 3 步：文本输入控件最后键击后 400ms 防抖提交（v0.4 决议，A-13）。
DEBOUNCE_MS = 400


def _system_clock_ms() -> float:
    """默认时钟：单调毫秒（运行期用；测试注入假时钟）。"""
    return time.monotonic() * 1000.0


class DebounceManager:
    """每条目独立的 400ms 防抖计时状态机（§7.5 / A-13）。

    语义：

    - :meth:`keystroke`：记录/覆盖该条目的待提交文本并重置计时——窗口内
      连续键击合并为**最后一次值**、到期恰产出**一次**提交事件；
    - :meth:`poll`：到期条目产出 ``(item_path, text)`` 事件（取出即清除，
      每条目恰一次）；由 UI 层周期 timer 调用（间隔 << 400ms）；
    - :meth:`flush` / :meth:`flush_all`：失焦立即提交，不等防抖——控件
      blur、切换活动文件、切换选中条目、关闭文件均视为失焦（§7.5 第 3 步）；
    - :meth:`cancel`：丢弃计时中的值不提交（如条目转 readonly 的防御）；
    - 多条目独立计时互不串扰（各自 key）。

    点击类控件（bool / 枚举候选选中）**不进本状态机**：值变化即提交。
    """

    def __init__(
        self,
        clock: Callable[[], float] | None = None,
        debounce_ms: float = DEBOUNCE_MS,
    ) -> None:
        self._clock = clock if clock is not None else _system_clock_ms
        self._debounce_ms = debounce_ms
        # item_path -> (到期时刻 ms, 最后键入文本)；dict 保持插入序（poll 输出稳定）
        self._pending: dict[str, tuple[float, str]] = {}

    # -- 查询 -----------------------------------------------------------------

    def pending_items(self) -> list[str]:
        """计时中的条目 path（插入序）。"""
        return list(self._pending)

    def is_pending(self, item_path: str) -> bool:
        return item_path in self._pending

    # -- 输入 -----------------------------------------------------------------

    def keystroke(self, item_path: str, text: str, now: float | None = None) -> None:
        """文本键击：重置该条目计时（合并窗口内连续键击为最后值）。"""
        t = self._clock() if now is None else now
        self._pending[item_path] = (t + self._debounce_ms, text)

    # -- 产出 -----------------------------------------------------------------

    def poll(self, now: float | None = None) -> list[tuple[str, str]]:
        """取出全部**已到期**条目 (item_path, text)；未到期者继续计时。"""
        t = self._clock() if now is None else now
        due = [
            (path, text)
            for path, (due_at, text) in self._pending.items()
            if due_at <= t
        ]
        for path, _ in due:
            del self._pending[path]
        return due

    def flush(self, item_path: str) -> tuple[str, str] | None:
        """失焦：立即产出该条目计时中的值（无计时 → None）。"""
        entry = self._pending.pop(item_path, None)
        if entry is None:
            return None
        return (item_path, entry[1])

    def flush_all(self) -> list[tuple[str, str]]:
        """失焦全部条目（切换/关闭活动文件、退出 flush 前置，§7.5 第 3 步）。"""
        out = [(path, text) for path, (_due, text) in self._pending.items()]
        self._pending.clear()
        return out

    def cancel(self, item_path: str) -> None:
        """丢弃计时中的值（不提交）。"""
        self._pending.pop(item_path, None)


# ---------------------------------------------------------------------------
# 提交/门控结果 → UI 动作映射（§7.5 第 2/5 步）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommitAction:
    """CommitResult.status 的 UI 动作描述。

    ``kind`` 取值：

    - ``'refresh'``：committed / reloaded / noop → 按 path 重取条目刷新控件
      并清除门控标记（提交成功后 doc 整体重载，raw_literal 已变，§7.5）；
    - ``'error'``：failed → 错误条文案 + 可重试（暂态保留，重试 = 再次
      commit_pending）；
    - ``'conflict'``：§7.6 基线冲突 → G2b 冲突模态（本块经宿主 on_conflict
      回调占位分流，未接线时降级为错误条文案）；
    - ``'gone'``：文件不可读 → G2b 钩子占位（同上降级）；
    - ``'silent'``：paused（冲突未决暂停自动提交）→ 静默不打扰。
    """

    kind: Literal["refresh", "error", "conflict", "gone", "silent"]
    message: str | None = None
    retry_path: str | None = None  # 'error' 时可重试的条目 path


def commit_action(result: "CommitResult") -> CommitAction:
    """CommitResult.status → UI 动作（宿主按 kind 分流执行）。"""
    status = result.status
    if status in ("committed", "reloaded", "noop"):
        return CommitAction(kind="refresh")
    if status == "failed":
        reason = result.reason or "未知原因"
        return CommitAction(
            kind="error",
            message=f"提交失败：{reason}（可重试）",
            retry_path=result.path,
        )
    if status == "conflict":
        return CommitAction(
            kind="conflict",
            message=result.reason or "文件已在磁盘上被修改，提交被基线校验阻止。",
        )
    if status == "gone":
        return CommitAction(
            kind="gone",
            message=result.reason or "文件当前不可读，无法提交。",
        )
    # paused：静默（冲突模态未决期间一律拒绝提交类操作，不打扰）
    return CommitAction(kind="silent")


@dataclass(frozen=True)
class GateFeedback:
    """PendingResult.level 的控件门控标记描述（U-6 即时反馈）。

    ``marker`` 取值：

    - ``'none'``：ok → 无标记（清除旧标记）；
    - ``'warn'``：warn（inferred 违规）→ 黄标 + reason，照常提交（I-6）；
    - ``'error'``：block / invalid → 红标 + reason，不提交（§8.2 硬校验）；
    - ``'keep'``：intermediate（``-``、空串等中间态）→ **无骚扰**：不设新
      标记也**不清除**旧标记（裁量登记：中间态是暂态过渡，清除旧红/黄标
      会随键入闪烁；确定性结果（ok/invalid/block）出现时再统一更新）。
    """

    marker: Literal["none", "warn", "error", "keep"]
    reason: str | None = None


def pending_feedback(result: "PendingResult") -> GateFeedback:
    """PendingResult.level → 控件标记状态。"""
    level = result.level
    if level == "ok":
        return GateFeedback(marker="none")
    if level == "warn":
        return GateFeedback(marker="warn", reason=result.reason)
    if level == "intermediate":
        return GateFeedback(marker="keep")
    # block / invalid → 红标 + 原因
    return GateFeedback(marker="error", reason=result.reason)


# ---------------------------------------------------------------------------
# 控件选型（U-6 类型控件映射，纯函数便于无头断言）
# ---------------------------------------------------------------------------

ControlKind = Literal["bool", "text", "enum", "readonly"]


def control_kind(item: "ConfigItem") -> ControlKind:
    """条目 → 控件类型（U-6 映射矩阵）。

    - readonly（或 type=None 的防御情形——金样本中 type=None 仅出现在只读
      容器条目）→ ``'readonly'``：灰显禁改只读展示（U-8）；
    - enum_candidates 非空 → ``'enum'``：下拉框（inferred 仍允许自定义
      输入，YC-6；见 :func:`enum_mode`）；
    - bool → ``'bool'``：开关（点击类即提）；
    - int/float/str → ``'text'``：文本输入（防抖路径；核心 parse 已做全部
      §8.2 数字边界——int 拒 3.0、float 收科学计数、中间态不提交）。
    """
    if item.readonly or item.type is None:
        return "readonly"
    if item.enum_candidates:
        return "enum"
    if item.type == "bool":
        return "bool"
    return "text"


def enum_mode(item: "ConfigItem") -> Literal["inferred", "declared"] | None:
    """枚举下拉模式：``'inferred'`` 允许列表外自定义输入（YC-6）；
    ``'declared'`` 仅限列表内（§8.2 硬校验，防御实现——金样本无 declared）。

    混合 provenance（防御）：任一候选为 declared → 按 declared 收紧。
    无候选 → None。
    """
    if not item.enum_candidates:
        return None
    if all(c.provenance == "inferred" for c in item.enum_candidates):
        return "inferred"
    return "declared"


def range_hint_text(item: "ConfigItem") -> str | None:
    """数字输入框旁的范围提示文本（U-6：有 range 时显示）。

    形如 ``范围 [0.05, 0.3] m``；单边界只显示存在侧（复用 logic.range_text
    的 ≥/≤ 形式）。无 range → None。**不钳制**：提示不伴随任何自动收敛。
    inferred range 的"推测"徽标由 UI 层与提示并存展示（U-5，本函数不带）。
    """
    from . import logic  # 局部导入避免环（logic 不依赖本模块，仅为整洁）

    text = logic.range_text(item.range)
    if text is None:
        return None
    return f"范围 {text}"


def text_widget_value(item: "ConfigItem") -> str:
    """文本输入控件的初始显示值（裁量登记）。

    显示**解析后的值**而非 raw_literal 原文：str 条目去引号（parse 契约
    接受裸文本，含空串）、int/float 规范化十进制（``100.`` → ``100.0``；
    落盘字面量风格由 §7.2 重放负责，与控件显示无关）。
    """
    if item.type == "str":
        return item.value if isinstance(item.value, str) else ""
    if item.value is None:
        return item.raw_literal
    return str(item.value)


def marker_style(marker: str) -> str:
    """门控标记 → 控件内联样式（红/黄标，U-6；与"推测"徽标并存）。

    - ``'error'``：红边框 + 浅红底（block/invalid，红标含原因文本）；
    - ``'warn'``：黄边框 + 浅黄底（inferred 违规 / dormant / warning 持续黄标）；
    - 其他（'none'/'keep'/未知）：空串（不改样式）。
    """
    if marker == "error":
        return "border-color: #dc2626 !important; background: #fef2f2;"
    if marker == "warn":
        return "border-color: #d97706 !important; background: #fffbeb;"
    return ""


def marker_class(marker: str) -> str:
    """门控标记 → 容器 CSS 类名（layout._CSS 定义 .gate-error/.gate-warn）。

    控件就地刷新用 ``classes(add=…, remove=…)`` 切换（不整体重建，防输入
    丢焦点）；'none'/'keep'/未知 → 空串（无标记类）。
    """
    return {"error": "gate-error", "warn": "gate-warn"}.get(marker, "")


def combine_marker(gate: "GateFeedback | None", item: "ConfigItem") -> tuple[str, str | None]:
    """门控标记 + 条目状态标志 → 控件**有效标记** (marker, reason)。

    优先级（U-6/U-8）：

    - 门控红标（block/invalid）压倒一切 → ``('error', 门控原因)``；
    - 门控黄标（inferred 违规）或 dormant/warning（**持续黄标**，U-8）
      → ``('warn', 门控原因或 None)``——dormant/warning 的提示原文由详情
      面板 _flags 段展示（裁量登记：控件旁侧 reason 标签只显示门控原因，
      避免同一文案双写）；
    - 其余 → ``('none', None)``。
    """
    if gate is not None and gate.marker == "error":
        return "error", gate.reason
    if gate is not None and gate.marker == "warn":
        return "warn", gate.reason
    if item.dormant or item.warning:
        return "warn", None
    return "none", None
