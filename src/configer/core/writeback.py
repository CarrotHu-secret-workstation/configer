"""写回引擎：原子写与文件属性（规范 §7.4）。

可测试的顺序契约（每次落盘必须按此五步）：
1. 在目标文件**同目录**创建临时文件（命名 ``.{原名}.configer-tmp` + 随机
   后缀保证唯一，用 ``tempfile.mkstemp``；创建前先清理同名残留——上次
   崩溃可能遗留）；
2. 写入适配器 save() 返回的完整字节，``fsync``；
3. 临时文件 ``chmod`` 为原文件模式（``stat.S_IMODE`` 逐位复制；原文件
   不存在或不可 stat → 默认 ``0o644 & ~umask``，裁量见 :func:`atomic_write`）；
4. ``os.rename`` 原子替换目标文件（同目录保证同文件系统）；
5. fsync 所在目录（尽力而为，OSError 忽略）。

失败语义：任何一步失败 → 尽力清理临时文件，异常包装为 :class:`WriteError`
（OSError 子类，``__cause__`` 保留原始异常）向上抛；session 层负责按
§7.5 第 5 步 / §9.2 exit 3 分流（错误条提示、暂态退回可重试）。

边界（不在本模块）：
- BOM 补回：session 层调用 ``bytesio.restore_bom`` 后把最终 payload 传入，
  本模块只写字节（§4.1）；
- token 级替换与风格重放：适配器 save()（§7.1/§7.2）；
- 提交前基线校验：poller.check_once + session（§7.6）。

除本次提交写入外，configer 不得以任何方式改动目标文件（§7.4）：原子写
只 rename 一个新文件到位，权限逐位复制自原文件；mtime 更新是写入的自然
语义，属"本次提交写入"的一部分。
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

__all__ = ["WriteError", "atomic_write", "TMP_MARKER"]

# 临时文件命名标记：前缀 ``.{原名}.configer-tmp``，mkstemp 再拼随机后缀。
# 测试与残留清理都以此标记匹配。
TMP_MARKER = ".configer-tmp"


class WriteError(OSError):
    """原子写失败（磁盘 I/O、权限、rename 等）。

    继承 OSError 以便调用方按既有 OSError 路径统一捕获；原始异常挂在
    ``__cause__`` 上，errno 等细节可从 ``__cause__`` 读取。
    """


def _default_mode() -> int:
    """原文件不存在/不可 stat 时的默认模式：``0o644 & ~umask``（裁量）。

    与 shell ``touch`` / Python ``open(..., 'w')`` 的默认创建语义一致。
    读取 umask 需临时置零再恢复（POSIX 无只读接口），窗口极短，且本
    项目写盘路径由 session 串行化，风险可接受。
    """
    cur = os.umask(0)
    os.umask(cur)
    return 0o644 & ~cur


def atomic_write(path: Path, payload: bytes) -> None:
    """按 §7.4 五步把 ``payload`` 原子写入 ``path``。

    成功返回 None；失败抛 :class:`WriteError`（临时文件已尽力清理，目标
    文件保持原样——rename 之前的任何失败都不触碰目标文件）。
    ``payload`` 允许为空字节（合法：写空文件）。
    """
    path = Path(path)
    directory = path.parent
    tmp_path: str | None = None
    fd: int | None = None
    try:
        # 步骤 0（裁量）：清理上次崩溃遗留的同名残留临时文件，尽力而为
        try:
            for stale in directory.glob(f".{path.name}{TMP_MARKER}*"):
                try:
                    stale.unlink()
                except OSError:
                    pass
        except OSError:
            pass

        # 步骤 1：同目录唯一临时文件（同目录 => rename 必为同文件系统）
        fd, tmp_path = tempfile.mkstemp(
            dir=str(directory), prefix=f".{path.name}{TMP_MARKER}"
        )

        # 步骤 2：写入全部字节 + fsync
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None

        # 步骤 3：权限逐位复制自原文件；原文件不存在 → 0o644 & ~umask
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            mode = _default_mode()
        os.chmod(tmp_path, mode)

        # 步骤 4：rename 原子替换
        os.rename(tmp_path, path)
        tmp_path = None  # 已改名到位，不再是待清理临时文件

        # 步骤 5：fsync 所在目录（尽力而为）
        try:
            dir_fd = os.open(str(directory), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except OSError as e:
        # 失败清理（尽力）+ 包装上抛，session 层分流（§7.5 第 5 步）
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise WriteError(f"原子写入 {path} 失败：{e}") from e
