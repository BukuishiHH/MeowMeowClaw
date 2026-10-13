"""工作区变更护栏(SA3, 见 docs/SUBAGENT_DESIGN.md §6).

包含三件事:
- :class:`WorkspaceMutationGuard`: workspace 级变更锁(进程内 asyncio.Lock +
  跨进程 ``memory/filelock.py``), 覆盖主/子代理的 write/exec;
- :class:`ReadVersionTracker`: 子代理私有的 "读后写" 版本表(sha256), 用于 OCC 冲突检测;
- :func:`atomic_write_text`: 临时文件 + fsync + ``os.replace`` 原子替换。
"""

import asyncio
import hashlib
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional, Union

from meowmeowclaw.memory.errors import MemoryStoreError
from meowmeowclaw.memory.filelock import async_file_lock

logger = logging.getLogger(__name__)


class MutationLockError(RuntimeError):
    """获取工作区变更锁失败(通常是等待超时)."""


class WorkspaceMutationGuard:
    """workspace 级变更锁; 一个进程内同一 workspace 共享一个实例.

    覆盖 write_file / exec 等所有会改变工作区的工具调用; read 类工具不经过这里。
    """

    def __init__(
        self,
        lock_path: Union[str, Path],
        *,
        timeout: float = 90.0,
        poll_interval: float = 0.02,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout 必须 > 0")
        self.lock_path = Path(lock_path)
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self._local_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"<WorkspaceMutationGuard lock={str(self.lock_path)!r} timeout={self.timeout}>"

    @asynccontextmanager
    async def mutate(self, *, reason: str = "", tool: str = "") -> AsyncIterator[None]:
        """获取进程内 + 跨进程变更锁; 阻塞超过 timeout 抛 :class:`MutationLockError`."""
        try:
            await asyncio.wait_for(self._local_lock.acquire(), timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            raise MutationLockError(
                f"工作区变更锁等待超时({self.timeout:g}s): reason={reason or '-'} tool={tool or '-'}"
            ) from exc

        try:
            try:
                async with async_file_lock(self.lock_path, timeout=self.timeout):
                    yield
            except MemoryStoreError as exc:
                raise MutationLockError(
                    f"工作区变更锁获取失败: reason={reason or '-'} tool={tool or '-'} ({exc})"
                ) from exc
        finally:
            self._local_lock.release()


class ReadVersionTracker:
    """子代理私有的读后写版本表.

    - ``record_read(path, content)`` 记录当前内容 sha256; 文件不存在时用 ``record_absent``;
    - ``is_tracked(path)`` 区分 "未读过" 与 "读到的就是不存在";
    - OCC 比较由 WriteFileTool 在变更锁内完成。
    """

    _UNSET = object()

    def __init__(self) -> None:
        self._versions: dict[str, Optional[str]] = {}

    def __repr__(self) -> str:
        return f"<ReadVersionTracker tracked={len(self._versions)}>"

    @staticmethod
    def digest_text(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def digest_bytes(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def record_read(self, path: str, content: str) -> None:
        self._versions[path] = self.digest_text(content)

    def record_absent(self, path: str) -> None:
        self._versions[path] = None

    def is_tracked(self, path: str) -> bool:
        return path in self._versions

    def expected(self, path: str):
        """返回记录的 sha256 或 None(读时不存在); 从未记录返回 ``_UNSET``."""
        return self._versions.get(path, self._UNSET)

    def clear(self) -> None:
        self._versions.clear()


def atomic_write_text(
    path: Union[str, Path],
    content: str,
    *,
    encoding: str = "utf-8",
    fsync: bool = True,
) -> None:
    """同目录临时文件 + fsync + os.replace 原子写文本.

    - 目标不存在时创建父目录;
    - 目标已存在时尽量保留原权限位;
    - 任何异常都会清理临时文件, 不改动目标文件。
    """
    target = os.path.abspath(os.fspath(path))
    parent = os.path.dirname(target) or os.curdir
    os.makedirs(parent, exist_ok=True)

    original_mode: Optional[int] = None
    try:
        stat = os.stat(target, follow_symlinks=False)
        original_mode = stat.st_mode & 0o777
    except OSError:
        original_mode = None

    fd, temp_path = tempfile.mkstemp(prefix=".meow-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        if original_mode is not None:
            try:
                os.chmod(temp_path, original_mode)
            except OSError:  # 权限保留失败不影响内容写入
                logger.warning("保留原文件权限失败: %s", target)
        os.replace(temp_path, target)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


__all__ = [
    "MutationLockError",
    "ReadVersionTracker",
    "WorkspaceMutationGuard",
    "atomic_write_text",
]
