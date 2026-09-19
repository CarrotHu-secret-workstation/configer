"""原子写测试（规范 §7.4 五步契约：内容、权限保持、临时文件清理、失败语义）。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from configer.core.writeback import TMP_MARKER, WriteError, atomic_write

IS_ROOT = os.geteuid() == 0 if hasattr(os, "geteuid") else False


def list_tmp_residue(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if TMP_MARKER in p.name]


class TestAtomicWriteBasics:
    def test_write_success_content_correct(self, tmp_path: Path):
        target = tmp_path / "config.yaml"
        target.write_bytes(b"old: 1\n")
        atomic_write(target, b"old: 2\n")
        assert target.read_bytes() == b"old: 2\n"

    def test_overwrite_is_complete_not_appended(self, tmp_path: Path):
        # rename 原子性：写后读到的必是完整新内容（更长覆盖更短、更短覆盖更长）
        target = tmp_path / "f.txt"
        target.write_bytes(b"x" * 1000)
        atomic_write(target, b"short")
        assert target.read_bytes() == b"short"
        atomic_write(target, b"y" * 5000)
        assert target.read_bytes() == b"y" * 5000

    def test_empty_payload_legal(self, tmp_path: Path):
        target = tmp_path / "empty.bin"
        target.write_bytes(b"not empty")
        atomic_write(target, b"")
        assert target.exists() and target.read_bytes() == b""

    def test_creates_new_file_when_absent(self, tmp_path: Path):
        target = tmp_path / "brand_new.yaml"
        atomic_write(target, b"a: 1\n")
        assert target.read_bytes() == b"a: 1\n"
        # 裁量：原文件不存在 → 默认 0o644 & ~umask
        cur = os.umask(0)
        os.umask(cur)
        expected = 0o644 & ~cur
        assert stat.S_IMODE(target.stat().st_mode) == expected


class TestPermissionPreservation:
    @pytest.mark.parametrize("mode", [0o600, 0o644, 0o664, 0o755, 0o400])
    def test_mode_preserved_bitwise(self, tmp_path: Path, mode: int):
        # §7.4 第 3 步：权限 stat.S_IMODE 逐位复制，除写入外不得改动属性
        target = tmp_path / "perm.yaml"
        target.write_bytes(b"v: 1\n")
        os.chmod(target, mode)
        atomic_write(target, b"v: 2\n")
        assert target.read_bytes() == b"v: 2\n"
        assert stat.S_IMODE(target.stat().st_mode) == mode


class TestTmpFileHygiene:
    def test_no_residue_after_success(self, tmp_path: Path):
        target = tmp_path / "config.yaml"
        target.write_bytes(b"a")
        atomic_write(target, b"b")
        assert list_tmp_residue(tmp_path) == []

    def test_stale_residue_cleaned_before_write(self, tmp_path: Path):
        # 上次崩溃遗留的残留临时文件在下次写入时被清理
        target = tmp_path / "config.yaml"
        target.write_bytes(b"a: 1\n")
        stale = tmp_path / f".config.yaml{TMP_MARKER}STALE"
        stale.write_bytes(b"garbage")
        atomic_write(target, b"a: 2\n")
        assert target.read_bytes() == b"a: 2\n"
        assert list_tmp_residue(tmp_path) == []

    def test_residue_from_other_files_untouched(self, tmp_path: Path):
        # 只清理与本次目标同名的残留，不碰别人的临时文件
        target = tmp_path / "mine.yaml"
        other = tmp_path / f".other.yaml{TMP_MARKER}XYZ"
        other.write_bytes(b"keep me")
        atomic_write(target, b"x")
        assert other.exists()


class TestFailureSemantics:
    @pytest.mark.skipif(IS_ROOT, reason="root 绕过目录写权限，无法构造失败")
    def test_readonly_dir_raises_and_no_partial(self, tmp_path: Path):
        target = tmp_path / "config.yaml"
        original = b"keep: me\n"
        target.write_bytes(original)
        os.chmod(tmp_path, 0o500)  # r-x：目录内不可创建文件
        try:
            with pytest.raises(WriteError) as ei:
                atomic_write(target, b"hacked: true\n")
            assert isinstance(ei.value, OSError)  # session 可按 OSError 统一捕获
            assert ei.value.__cause__ is not None  # 原始异常保留
            # 目标文件原样，无半成品、无残留
            assert target.read_bytes() == original
        finally:
            os.chmod(tmp_path, 0o700)
        assert list_tmp_residue(tmp_path) == []

    @pytest.mark.skipif(IS_ROOT, reason="root 绕过目录写权限")
    def test_failure_in_readonly_dir_leaves_no_tmp(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        target = sub / "f.yaml"
        target.write_bytes(b"1")
        os.chmod(sub, 0o500)
        try:
            with pytest.raises(WriteError):
                atomic_write(target, b"2")
        finally:
            os.chmod(sub, 0o700)
        assert list_tmp_residue(sub) == []
