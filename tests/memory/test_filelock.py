"""M7: FileLock / async_file_lock 单元测试."""

import pytest

from meowmeowclaw.memory.errors import MemoryStoreError
from meowmeowclaw.memory.filelock import (
    CROSS_PROCESS_LOCK_AVAILABLE,
    FileLock,
    async_file_lock,
)

pytestmark = pytest.mark.skipif(
    not CROSS_PROCESS_LOCK_AVAILABLE, reason="当前平台不支持跨进程文件锁"
)


class TestFileLock:
    def test_contend_then_reacquire(self, tmp_path):
        path = tmp_path / "x.lock"
        first = FileLock(path, timeout=0.05)
        first.acquire()
        try:
            with pytest.raises(MemoryStoreError):
                FileLock(path, timeout=0.05).acquire()
        finally:
            first.release()

        third = FileLock(path, timeout=0.5)
        third.acquire()
        third.release()

    def test_context_manager(self, tmp_path):
        lock = FileLock(tmp_path / "c.lock")

        with lock:
            assert lock._fd is not None  # noqa: SLF001

        assert lock._fd is None  # noqa: SLF001

    def test_release_without_acquire_is_noop(self, tmp_path):
        FileLock(tmp_path / "n.lock").release()

    @pytest.mark.asyncio
    async def test_async_file_lock(self, tmp_path):
        async with async_file_lock(tmp_path / "a.lock", timeout=0.5):
            pass
