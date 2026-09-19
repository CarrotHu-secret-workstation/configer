"""语义模型不变量检查（规范 §3.9）。

§3.9 定义 I-1..I-7。本模块承载其中可在纯数据层判定的部分：

- **I-1 路径唯一**：同一 ConfigDoc 内 ``path`` 不得重复——本函数必查。
- **I-2 值与字面量一致**：``value`` 必须等于按格式解析 ``raw_literal`` 的
  结果。核心**不得**解析任何具体格式（§2 职责边界 2），故通过可选回调
  ``parse_literal(raw_literal, type)`` 开放给适配器层：适配器在往返测试中
  注入自己的字面量解析函数；未提供回调则跳过 I-2。
- **I-3 恒等往返 / I-4 外科手术边界 / I-5 未触碰条目字节不变**：属写回
  保真性质，无法在纯数据层判定，由**写回测试**（§7.3 T1/T2/T4，金样本
  矩阵）保证——第三阶段随 writeback 与两适配器的往返测试落地。
- I-6 推测永拦截（不拦截）/ I-7 徽标一致：分别属提交门控（§7.5，
  validation/session）与 UI（§10 U-5），不在本函数范围。

返回违例消息列表（空列表 = 全部通过），消息含不变量编号与可定位信息，
便于测试断言与调试输出。
"""

from __future__ import annotations

from typing import Any, Callable

from ..model import ConfigDoc

__all__ = ["check_doc_invariants"]


def check_doc_invariants(
    doc: ConfigDoc,
    parse_literal: Callable[[str, str | None], Any] | None = None,
) -> list[str]:
    """检查 doc 的数据层不变量（I-1 必查；I-2 经回调开放；见模块 docstring）。

    参数：
    - ``doc``：待检查的 ConfigDoc；
    - ``parse_literal``：可选回调 ``(raw_literal, type) -> 解析值``，由适配器
      提供（核心不得解析具体格式，§2）。提供时对每个 ``type`` 非 None 的条目
      执行 I-2 比对；未提供则跳过 I-2。

    I-2 比对采用「值相等且类型同一」的严格判定：防止 Python 中
    ``True == 1``、``1 == 1.0`` 这类跨类型相等掩盖 bool/int/float 混淆。

    返回违例消息列表（空 = 通过）。
    """
    violations: list[str] = []

    # I-1 路径唯一
    seen: dict[str, int] = {}
    for item in doc.items:
        if item.path in seen:
            seen[item.path] += 1
        else:
            seen[item.path] = 1
    for path, count in seen.items():
        if count > 1:
            violations.append(
                f"I-1 路径唯一违例：path {path!r} 在 doc（{doc.path}）中出现 {count} 次"
            )

    # I-2 值与字面量一致（仅当适配器注入 parse_literal 回调时）
    if parse_literal is not None:
        for item in doc.items:
            if item.type is None:
                # 只读且类型未知（如 tuple 表，§3.1）：value 为 None，
                # raw_literal 保留全文，不参与 I-2。
                continue
            parsed = parse_literal(item.raw_literal, item.type)
            if type(parsed) is not type(item.value) or parsed != item.value:
                violations.append(
                    f"I-2 值与字面量不一致：path {item.path!r} 的 value="
                    f"{item.value!r}（{type(item.value).__name__}），而 raw_literal="
                    f"{item.raw_literal!r} 按 type={item.type!r} 解析为 "
                    f"{parsed!r}（{type(parsed).__name__}）"
                )

    return violations
