"""适配器注册表与格式嗅探（规范 §4.2）。

规则（normative）：
- 注册表为**有序列表** ``[PythonAdapter, YamlAdapter]``（v1 固定顺序；新适配器
  追加）。当前第一阶段注册表为空，第二阶段注册真实适配器。
- ``--format`` 显式指定（'python' / 'yaml'）时按注册名**直接选定**，选定
  适配器 load 失败 → exit 2，**不得**回退其他适配器（§4.2、§9.1）；显式名
  未注册 → E-FORMAT。
- ``auto``：先按扩展名匹配 ``extensions``，命中唯一则用该适配器（内容嗅探
  **确认**由调用方负责：确认失败 → E-PARSE，不换适配器，§4.2）；扩展名未
  命中则按注册顺序逐个 ``detect`` 内容嗅探；全部失败 → E-FORMAT，exit 2。
"""

from __future__ import annotations

from pathlib import Path

from .adapters.base import Adapter
from .model import CODE_E_FORMAT, SEV_ERROR, Diagnostic

__all__ = ["Registry", "default_registry"]


class Registry:
    """有序适配器注册表（§4.2）。"""

    def __init__(self, adapters: list[Adapter] | None = None) -> None:
        self._adapters: list[Adapter] = list(adapters) if adapters else []

    @property
    def adapters(self) -> tuple[Adapter, ...]:
        """按注册顺序返回适配器（只读视图）。"""
        return tuple(self._adapters)

    def register(self, adapter: Adapter) -> None:
        """追加注册（保持有序；新适配器追加在尾部，§4.2）。

        注册名重复属程序错误（会让显式 --format 选定产生歧义），抛 ValueError。
        """
        if self.get(adapter.name) is not None:
            raise ValueError(f"适配器注册名重复：{adapter.name!r}")
        self._adapters.append(adapter)

    def get(self, name: str) -> Adapter | None:
        """按注册名查找（显式 --format 选定用，§4.2）。"""
        for adapter in self._adapters:
            if adapter.name == name:
                return adapter
        return None

    def select_adapter(
        self,
        path: Path,
        head: bytes,
        format_hint: str = "auto",
    ) -> tuple[Adapter | None, list[Diagnostic]]:
        """选定处理 ``path`` 的适配器（§4.2 嗅探流程）。

        返回 (adapter | None, diagnostics)。选定失败时 adapter 为 None 且
        diagnostics 含一条 E-FORMAT error（调用方按 §9.2 以 exit 2 处理）。

        - ``format_hint`` 非 'auto'：按注册名直接选定（不做扩展名匹配、不做
          detect）；未注册 → E-FORMAT。
        - ``format_hint == 'auto'``：扩展名匹配（大小写不敏感）命中唯一 →
          直接返回（后续内容嗅探**确认**由调用方负责，确认失败 → E-PARSE，
          不换适配器）；未命中（或歧义多命中，v1 不应出现）→ 按注册顺序逐个
          ``detect``；全部失败 → E-FORMAT。

        ``detect`` 按契约不得抛异常（§4.1）；本方法仍做防御性捕获，异常视同
        False 继续尝试后续适配器，避免单个坏适配器崩掉嗅探流程。
        """
        diagnostics: list[Diagnostic] = []

        if format_hint != "auto":
            adapter = self.get(format_hint)
            if adapter is None:
                registered = ", ".join(a.name for a in self._adapters) or "（空）"
                diagnostics.append(
                    Diagnostic(
                        severity=SEV_ERROR,
                        code=CODE_E_FORMAT,
                        message=(
                            f"显式指定的格式 {format_hint!r} 没有已注册的适配器"
                            f"（当前注册表：{registered}），无法打开 {path}"
                        ),
                        path=None,
                    )
                )
                return None, diagnostics
            return adapter, diagnostics

        # auto 第一步：扩展名匹配（命中唯一 → 选定；嗅探确认由调用方负责）
        suffix = path.suffix.lower()
        if suffix:
            matched = [
                a for a in self._adapters
                if suffix in tuple(ext.lower() for ext in a.extensions)
            ]
            if len(matched) == 1:
                return matched[0], diagnostics
            # len(matched) > 1：扩展名歧义（v1 注册表不应出现），保守起见
            # 退回内容嗅探按序判定；len == 0：未命中，同样走内容嗅探。

        # auto 第二步：按注册顺序逐个 detect 内容嗅探
        for adapter in self._adapters:
            try:
                if adapter.detect(path, head):
                    return adapter, diagnostics
            except Exception:
                # detect 不得抛异常（§4.1）；防御性视同 False。
                continue

        # 全部失败 → E-FORMAT
        diagnostics.append(
            Diagnostic(
                severity=SEV_ERROR,
                code=CODE_E_FORMAT,
                message=(
                    f"无法识别文件格式：{path} 的扩展名未匹配任何适配器，"
                    "内容嗅探也全部失败"
                ),
                path=None,
            )
        )
        return None, diagnostics


def default_registry() -> Registry:
    """返回默认注册表：已按 v1 固定顺序注册 ``[PythonAdapter, YamlAdapter]``
    （§4.2）。每次调用新建实例（无共享可变状态）。

    适配器 import 为**函数内懒 import** 且逐个防御性跳过：某个适配器模块
    瞬态不可用（如并行开发中的语法错误、依赖缺失）不阻断另一个注册，
    也不破坏调用方的 import 链（裁量登记，chunk 3c）。
    """
    registry = Registry()
    try:
        from .adapters.python_adapter import PythonAdapter

        registry.register(PythonAdapter())
    except Exception:  # pragma: no cover - 防御：瞬态不可用不阻断 yaml
        pass
    try:
        from .adapters.yaml_adapter import YamlAdapter

        registry.register(YamlAdapter())
    except Exception:  # pragma: no cover - 防御：瞬态不可用不阻断 python
        pass
    return registry
