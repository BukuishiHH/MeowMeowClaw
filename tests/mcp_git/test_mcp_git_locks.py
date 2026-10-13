"""GM3 仓库写锁单测(``meowmeowclaw_mcp_git/locks.py``, §5.5/D9/T8)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from meowmeowclaw_mcp_git.errors import ConfigError, LockTimeoutError
from meowmeowclaw_mcp_git.locks import RepoLockManager
from meowmeowclaw_mcp_git.registry import RepoEntry


@pytest.fixture
def entry(tmp_path: Path) -> RepoEntry:
    repo = tmp_path / "repo"
    repo.mkdir()
    return RepoEntry(alias="repo", path=repo, root=tmp_path)


class TestInProcessLock:
    @pytest.mark.asyncio
    async def test_writes_are_serialized(self, entry: RepoEntry):
        manager = RepoLockManager(timeout=5.0)
        order: list[str] = []

        async def worker(index: int) -> None:
            async with manager.acquire(entry):
                order.append(f"start{index}")
                await asyncio.sleep(0.05)
                order.append(f"end{index}")

        await asyncio.gather(worker(1), worker(2))

        assert order in (["start1", "end1", "start2", "end2"], ["start2", "end2", "start1", "end1"])

    @pytest.mark.asyncio
    async def test_wait_times_out(self, entry: RepoEntry):
        manager = RepoLockManager(timeout=0.05)

        async def holder() -> None:
            async with manager.acquire(entry):
                await asyncio.sleep(0.3)

        task = asyncio.create_task(holder())
        await asyncio.sleep(0.02)
        with pytest.raises(LockTimeoutError) as excinfo:
            async with manager.acquire(entry):
                pass
        await task

        assert excinfo.value.code == "lock_timeout"

    @pytest.mark.asyncio
    async def test_lock_released_after_exception(self, entry: RepoEntry):
        manager = RepoLockManager(timeout=0.5)

        with pytest.raises(RuntimeError):
            async with manager.acquire(entry):
                raise RuntimeError("boom")

        async with manager.acquire(entry):  # 不应死锁
            pass

    def test_different_repos_do_not_share_lock(self, tmp_path: Path):
        manager = RepoLockManager(timeout=0.5)
        first = RepoEntry(alias="a", path=tmp_path / "a", root=tmp_path)
        second = RepoEntry(alias="b", path=tmp_path / "b", root=tmp_path)

        assert manager.lock_path_for(first) is None == manager.lock_path_for(second) is None


class TestCrossProcessLock:
    def test_no_lock_dir_means_no_file(self, entry: RepoEntry):
        assert RepoLockManager().lock_path_for(entry) is None

    @pytest.mark.asyncio
    async def test_lock_file_created_with_private_mode(self, entry: RepoEntry, tmp_path: Path):
        lock_dir = tmp_path / "locks"
        manager = RepoLockManager(lock_dir=str(lock_dir), timeout=1.0)
        lock_path = manager.lock_path_for(entry)

        async with manager.acquire(entry):
            assert lock_path is not None and lock_path.exists()
            assert lock_path.stat().st_mode & 0o777 == 0o600

    @pytest.mark.asyncio
    async def test_two_managers_in_same_dir_conflict(self, entry: RepoEntry, tmp_path: Path):
        lock_dir = str(tmp_path / "locks")
        first = RepoLockManager(lock_dir=lock_dir, timeout=1.0)
        second = RepoLockManager(lock_dir=lock_dir, timeout=0.05)

        async with first.acquire(entry):
            with pytest.raises(LockTimeoutError):
                async with second.acquire(entry):
                    pass

        # 释放后第二个 manager 可以获取
        async with second.acquire(entry):
            pass

    @pytest.mark.asyncio
    async def test_second_manager_waits_when_configured(self, entry: RepoEntry, tmp_path: Path):
        lock_dir = str(tmp_path / "locks")
        first = RepoLockManager(lock_dir=lock_dir, timeout=1.0)
        second = RepoLockManager(lock_dir=lock_dir, timeout=1.0)
        started = time.monotonic()

        async def holder() -> None:
            async with first.acquire(entry):
                await asyncio.sleep(0.15)

        task = asyncio.create_task(holder())
        await asyncio.sleep(0.02)
        async with second.acquire(entry):
            pass
        await task

        assert time.monotonic() - started >= 0.1


class TestConfigErrors:
    def test_lock_dir_must_be_directory(self, tmp_path: Path):
        blocker = tmp_path / "blocker"
        blocker.write_text("file\n", encoding="utf-8")

        with pytest.raises(ConfigError):
            RepoLockManager(lock_dir=str(blocker))

    def test_timeout_must_be_positive(self, tmp_path: Path):
        with pytest.raises(ConfigError):
            RepoLockManager(lock_dir=str(tmp_path / "locks"), timeout=0)
