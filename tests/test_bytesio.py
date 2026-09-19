"""configer.bytesio 单元测试（规范 §4.1 编码契约）。"""

from __future__ import annotations

import pytest

from configer.bytesio import (
    UTF8_BOM,
    detect_encoding,
    detect_mixed_eols,
    restore_bom,
    strip_bom,
)
from configer.model import CODE_E_ENCODING, SEV_ERROR


# ---------------------------------------------------------------------------
# BOM 剥离 / 补回（§4.1：适配器只见 BOM-less 文本，写回由核心补回）
# ---------------------------------------------------------------------------

def test_strip_bom_present() -> None:
    raw = UTF8_BOM + "键: 1\n".encode("utf-8")
    data, had_bom = strip_bom(raw)
    assert had_bom is True
    assert data == "键: 1\n".encode("utf-8")
    assert not data.startswith(UTF8_BOM)


def test_strip_bom_absent() -> None:
    raw = "key: 1\n".encode("utf-8")
    data, had_bom = strip_bom(raw)
    assert had_bom is False
    assert data == raw


def test_bom_roundtrip_identity() -> None:
    # 有 BOM：strip → restore 逐字节还原
    raw = UTF8_BOM + b"KICK_POWER_MIN = 1.0\n"
    data, had_bom = strip_bom(raw)
    assert restore_bom(data, had_bom) == raw
    # 无 BOM：同样还原
    raw2 = b"step_interval: 100. \n"
    data2, had_bom2 = strip_bom(raw2)
    assert restore_bom(data2, had_bom2) == raw2


def test_restore_bom_explicit() -> None:
    assert restore_bom(b"abc", True) == UTF8_BOM + b"abc"
    assert restore_bom(b"abc", False) == b"abc"


def test_strip_bom_only_strips_one_bom() -> None:
    # 双 BOM 属病态输入：只剥一层，内层作为内容（内容随后 UTF-8 检测仍合法，
    # BOM 字符 U+FEFF 是合法 UTF-8 码点——保真原则：不额外改动内容）
    raw = UTF8_BOM + UTF8_BOM + b"x = 1\n"
    data, had_bom = strip_bom(raw)
    assert had_bom is True
    assert data == UTF8_BOM + b"x = 1\n"


# ---------------------------------------------------------------------------
# 编码检测（§4.1：v1 仅 UTF-8，否则 E-ENCODING）
# ---------------------------------------------------------------------------

def test_detect_encoding_valid_utf8() -> None:
    assert detect_encoding("配置: 值\n".encode("utf-8")) is None
    assert detect_encoding(b"") is None  # 空字节串是合法 UTF-8（0 字节文件，§4.2）
    assert detect_encoding(b"plain ascii\n") is None


def test_detect_encoding_utf16_bytes_rejected() -> None:
    # b'\xff\xfe\x00abc'：UTF-16-LE 形态字节。detect 只认 UTF-8，即使内容
    # "其实是" UTF-16 也按非 UTF-8 拒绝（不得误判为合法）。
    raw = b"\xff\xfe\x00abc"
    diag = detect_encoding(raw)
    assert diag is not None
    assert diag.severity == SEV_ERROR == "error"
    assert diag.code == CODE_E_ENCODING == "E-ENCODING"
    assert diag.path is None
    assert "UTF-8" in diag.message  # message 含可定位信息（§3.7）


def test_detect_encoding_invalid_continuation() -> None:
    assert detect_encoding(b"\xc3\x28") is not None  # 非法 UTF-8 续字节
    assert detect_encoding(b"abc\x80def") is not None  # 孤立续字节
    assert detect_encoding(b"\xff") is not None


def test_detect_encoding_message_has_position() -> None:
    diag = detect_encoding(b"ok\n\xff\xfe")
    assert diag is not None
    assert "3" in diag.message  # 失败字节位置可定位


# ---------------------------------------------------------------------------
# 行尾混杂检测（§4.1：LF 与 CRLF 并存才算混杂；单一风格不告警）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"a\nb\n", False),          # 纯 LF
        (b"a\r\nb\r\n", False),      # 纯 CRLF
        (b"a\nb\r\n", True),         # 混杂
        (b"a\r\nb\n", True),         # 混杂（反序）
        (b"", False),                # 空文件
        (b"no eol at all", False),   # 无换行
        (b"\n", False),
        (b"\r\n", False),
        (b"a\rb\r", False),          # 孤立 CR：不在 v1 混杂定义内
        (b"a\r\n\r\nb\n\r\n", True),
    ],
)
def test_detect_mixed_eols(data: bytes, expected: bool) -> None:
    assert detect_mixed_eols(data) is expected
