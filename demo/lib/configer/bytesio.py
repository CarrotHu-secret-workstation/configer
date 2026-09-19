"""字节层编码契约（规范 §4.1「编码契约」段）。

核心在调用适配器前统一处理字节层：
- 检测编码：v1 仅 UTF-8，否则 ``E-ENCODING``；
- UTF-8 BOM 由核心剥离并记录，写回时由核心补回；**适配器见到的永远是
  BOM-less 文本**；
- 换行符（LF/CRLF）由适配器原样保留；混杂（同一文件内 LF 与 CRLF 并存，
  单一风格不告警）时逐行保留、尽力而为，并给 ``W-EOL-MIXED``。

本模块是纯函数集合，不做 I/O。
"""

from __future__ import annotations

from .model import CODE_E_ENCODING, SEV_ERROR, Diagnostic

__all__ = ["UTF8_BOM", "detect_encoding", "strip_bom", "restore_bom", "detect_mixed_eols"]

UTF8_BOM = b"\xef\xbb\xbf"


def detect_encoding(raw: bytes) -> Diagnostic | None:
    """检测编码（v1 仅 UTF-8）。合法返回 None；非法返回 E-ENCODING error 诊断。

    只尝试 UTF-8 解码——即使字节序列恰是合法 UTF-16（如带 ``\\xff\\xfe`` BOM
    的文本），v1 也一律按非 UTF-8 拒绝（§4.1、§9.2 exit 2）。
    诊断 message 含解码失败位置，满足「可定位信息」要求（§3.7）。
    """
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return Diagnostic(
            severity=SEV_ERROR,
            code=CODE_E_ENCODING,
            message=(
                f"文件不是合法 UTF-8 编码（字节 {exc.start} 处：{raw[exc.start:exc.end]!r}），"
                "v1 仅支持 UTF-8，无法打开"
            ),
            path=None,
        )
    return None


def strip_bom(raw: bytes) -> tuple[bytes, bool]:
    """剥离 UTF-8 BOM（§4.1）。返回 (BOM-less 字节, 是否曾有 BOM)。

    「是否曾有 BOM」由调用方（核心）记录，写回时经 :func:`restore_bom` 补回。
    """
    if raw.startswith(UTF8_BOM):
        return raw[len(UTF8_BOM):], True
    return raw, False


def restore_bom(data: bytes, has_bom: bool) -> bytes:
    """写回时按 load 期记录补回 UTF-8 BOM（§4.1、§7.4）。"""
    return UTF8_BOM + data if has_bom else data


def detect_mixed_eols(data: bytes) -> bool:
    """行尾风格是否混杂（§4.1）：同一文件内 LF 与 CRLF **并存**才算混杂。

    纯 LF → False；纯 CRLF → False；两者并存 → True（调用方给 W-EOL-MIXED）。
    孤立 ``\\r``（旧 Mac 风格）不在 v1 混杂定义内，不计入。
    """
    crlf = data.count(b"\r\n")
    lone_lf = data.count(b"\n") - crlf
    return crlf > 0 and lone_lf > 0
