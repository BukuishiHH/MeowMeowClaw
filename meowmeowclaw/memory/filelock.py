"""跨进程 advisory 文件锁.

- POSIX: ``fcntl.flock(LOCK_EX | LOCK_NB)`` 轮询 + 超时;
- Windows: ``msvcrt.locking`` 尽力而为;
- 平台不支持时退化为无操作(调用方仍有进程内 asyncio.Lock 兜底)。

用途: CLI 与 QQ 服务可能同时访问同一个 ``memory_dir``; 会话级文件锁保证
append/archive/purge 这些"读-改-写"操作跨进程串行(D8/M7)。
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional, Union

from .errors import MemoryStoreError

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

CROSS_PROCESS_LOCK_AVAILABLE = fcntl is not None or msvcrt is not None


class FileLock:
    """同步 advisory 文件锁(占位文件 + flock/locking)."""

    def __init__(
        self,
        path: Union[str, Path],
        *,
        timeout: float = 5.0,
        poll_interval: float = 0.02,
    ) -> None:
        self.path = Path(path)
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self._fd: Optional[int] = None

    def __repr__(self) -> str:
        return f"<FileLock path={str(self.path)!r} locked={self._fd is not None}>"

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise MemoryStoreError(f"打开锁文件失败: {self.path} ({exc!r})") from exc

        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:  # pragma: no cover - Windows
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                self._fd = fd
                return
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise MemoryStoreError(f"获取文件锁超时: {self.path}") from None
                time.sleep(self.poll_interval)

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    logger.warning("释放 Windows 文件锁失败: %s", self.path)
        finally:
            os.close(fd)

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


@asynccontextmanager
async def async_file_lock(
    path: Union[str, Path], *, timeout: float = 5.0
) -> AsyncIterator[None]:
    """异步上下文: 阻塞获取放在线程池中, 不卡事件循环."""
    lock = FileLock(path, timeout=timeout)
    await asyncio.to_thread(lock.acquire)
    try:
        yield
    finally:
        await asyncio.to_thread(lock.release)
