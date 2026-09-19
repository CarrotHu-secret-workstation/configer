"""Adapter 接口（规范 §4.1，normative；稳定接口，变更须升版本号）。

接口伪代码照抄 §4.1，docstring 为规范性语义：

- detect：**不得抛异常**；
- load：对任何可读输入**不得抛异常**，失败一律以 error 级 Diagnostic 表达；
  ConfigDoc.original_bytes / content_hash 由适配器负责填充；适配器见到的
  永远是 BOM-less 文本（编码检测与 BOM 剥离/补回由核心统一处理，§4.1）；
- save：逐条应用 EditOp（token 级替换 + 字面量风格重放，§7.1/§7.2），返回
  新文件完整字节；**不落盘**（写盘由核心的原子写负责，§7.4）；edits 引用
  未知 path → **抛异常**（程序错误，§3.8）。

换行符（LF/CRLF）由适配器原样保留；混杂风格逐行保留、尽力而为（§4.1）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from ..model import ConfigDoc, Diagnostic, EditOp

__all__ = ["Adapter"]


@runtime_checkable
class Adapter(Protocol):
    """一种文件格式的加载器 + 写回器（§0.3、§4.1）。

    新适配器接入 = 实现三方法 + 注册 + 提供注释画像（§5）+ 金样本往返测试；
    核心代码零改动（§4.2）。
    """

    name: str
    """注册名："python" | "yaml"（§4.1）。显式 --format 按此名选定（§4.2）。"""

    extensions: tuple[str, ...]
    """扩展名表：(".py",) / (".yaml", ".yml")。auto 嗅探第一步按此匹配（§4.2）。"""

    def detect(self, path: Path, head: bytes) -> bool:
        """是否由本适配器处理。扩展名优先，内容嗅探兜底。**不得抛异常**。

        head 为核心读入的文件头部字节（BOM-less 嗅探由调用方保证语义，
        实现应容忍任意字节）。
        """
        ...

    def load(self, path: Path) -> tuple[ConfigDoc, list[Diagnostic]]:
        """解析文件为 ConfigDoc。任何可读输入都**不得抛异常**：
        失败一律以 error 级 Diagnostic 表达（如 E-PARSE / E-ENCODING）。
        ConfigDoc.original_bytes / content_hash 由适配器负责填充。
        适配器见到的永远是 BOM-less 文本（§4.1 编码契约由核心预处理）。
        """
        ...

    def save(self, doc: ConfigDoc, edits: Sequence[EditOp]) -> bytes:
        """对 doc（必须是本适配器 load() 的产物，locator 有效）逐条应用
        EditOp：token 级替换 + 字面量风格重放（§7.1/§7.2），返回新文件
        完整字节。**不落盘**（写盘由核心的原子写负责，§7.4）。
        edits 引用未知 path → **抛异常**（程序错误，§3.8）。
        """
        ...
