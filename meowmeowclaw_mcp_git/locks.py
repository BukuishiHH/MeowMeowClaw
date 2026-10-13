"""仓库写锁(§5.5/D9/T8): 每 repo 一把进程内 ``asyncio.Lock`` + 可选跨进程文件锁。

- 写操作(stage/unstage/restore/commit/branch/stash)在锁内执行, 只读操作不取锁;
- ``--lock-dir`` 配置后叠加跨进程 advisory 锁(POSIX ``flock`` / Windows ``msvcrt``,
  与 ``meowmeowclaw/memory/filelock.py`` 同思路), 锁文件按 repo realpath 哈希命名、权限 0600;
- 获取超时(默认 10s)抛 :class:`LockTimeoutError`(``lock_timeout``), 不让请求无限挂起;
- MeowMeowClaw 子代理的 workspace 锁文件名是 ``workspace.lock``, 与本实现的按仓库哈希命名
  不同; v1 只保证“同一 --lock-dir 的多个 MCP Server 进程互斥”, 与主项目锁完全打通留给 §12。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from .errors import ConfigError, LockTimeoutError
from .registry import RepoEntry

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
_POLL_INTERVAL_SECONDS = 0.02


class _FileLock:
    """跨进程 advisory 文件锁(同步; 由调用方放进线程池)."""

    def __init__(self, path: Path, *, timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> None:
        self.path = path
        self.timeout = float(timeout)
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise ConfigError(f"无法创建跨进程锁文件: {self.path} ({exc})") from exc
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:  # pragma: no cover - Windows
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - 平台既不支持 flock 也不支持 msvcrt
                    logger.warning("当前平台不支持跨进程文件锁, 仅使用进程内锁")
                self._fd = fd
                return
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockTimeoutError(f"等待跨进程仓库锁超时(>{self.timeout:g}s): {self.path}") from None
                time.sleep(_POLL_INTERVAL_SECONDS)

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:  # pragma: no cover - 释放失败不阻塞主流程
            logger.warning("释放跨进程仓库锁失败: %s", self.path)
        finally:
            os.close(fd)


class RepoLockManager:
    """每仓库写锁: 进程内 asyncio.Lock + 可选跨进程锁."""

    def __init__(self, *, lock_dir: str = "", timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> None:
        if timeout <= 0:
            raise ConfigError("锁超时时间必须 > 0")
        self.lock_dir = str(lock_dir or "").strip()
        self.timeout = float(timeout)
        if self.lock_dir:
            try:
                Path(self.lock_dir).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ConfigError(f"无法创建 --lock-dir: {self.lock_dir} ({exc})") from exc
            if not os.access(self.lock_dir, os.W_OK):
                raise ConfigError(f"--lock-dir 不可写: {self.lock_dir}")
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_path_for(self, entry: RepoEntry) -> Optional[Path]:
        """跨进程锁文件路径(未配置 --lock-dir 时为 None), 供测试/排障使用."""
        if not self.lock_dir:
            return None
        digest = hashlib.sha1(os.fsencode(str(entry.path))).hexdigest()[:16]
        return Path(self.lock_dir) / f"{digest}.mcp-git.lock"

    @asynccontextmanager
    async def acquire(self, entry: RepoEntry) -> AsyncIterator[None]:
        """在仓库锁内执行写操作; 获取失败抛 ``lock_timeout``."""
        key = str(entry.path)
        local_lock = self._locks.get(key)
        if local_lock is None:
            local_lock = asyncio.Lock()
            self._locks[key] = local_lock
        try:
            await asyncio.wait_for(local_lock.acquire(), timeout=self.timeout)
        except asyncio.TimeoutError:
            raise LockTimeoutError(
                f"等待仓库写锁超时(>{self.timeout:g}s): {entry.alias}"
            ) from None

        file_lock: Optional[_FileLock] = None
        try:
            lock_path = self.lock_path_for(entry)
            if lock_path is not None:
                file_lock = _FileLock(lock_path, timeout=self.timeout)
                await asyncio.to_thread(file_lock.acquire)
            yield
        finally:
            if file_lock is not None:
                await asyncio.to_thread(file_lock.release)
            local_lock.release()
