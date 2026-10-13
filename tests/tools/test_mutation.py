"""工作区变更护栏单元测试(SA3, docs/SUBAGENT_DESIGN.md §6.2–§6.4).

覆盖: 进程内+跨实例互斥、锁超时、读后写版本表、原子写成功/失败清理。
"""

import asyncio

import pytest

import meowmeowclaw.tools.mutation as mutation_module
from meowmeowclaw.tools.mutation import (
    MutationLockError,
    ReadVersionTracker,
    WorkspaceMutationGuard,
    atomic_write_text,
)


@pytest.mark.asyncio
async def test_guard_serializes_two_instances_without_interleaving(tmp_path):
    lock_path = tmp_path / "subagents" / "workspace.lock"
    first_guard = WorkspaceMutationGuard(lock_path, timeout=2.0)
    second_guard = WorkspaceMutationGuard(lock_path, timeout=2.0)
    order: list[str] = []

    async def worker(name: str, guard: WorkspaceMutationGuard) -> None:
        async with guard.mutate(reason=name):
            order.append(f"start-{name}")
            await asyncio.sleep(0.02)
            order.append(f"end-{name}")

    await asyncio.gather(worker("a", first_guard), worker("b", second_guard))

    # 不出现 start-a/start-b/end-a/end-b 的交错
    assert len(order) == 4
    assert order[0].split("-")[0] == "start"
    assert order[1] == order[0].replace("start", "end")
    assert order[2].split("-")[0] == "start"
    assert order[3] == order[2].replace("start", "end")


@pytest.mark.asyncio
async def test_guard_times_out_when_lock_is_held(tmp_path):
    lock_path = tmp_path / "workspace.lock"
    holder = WorkspaceMutationGuard(lock_path, timeout=1.0)
    waiter = WorkspaceMutationGuard(lock_path, timeout=0.05)
    release = asyncio.Event()

    async def hold() -> None:
        async with holder.mutate(reason="hold"):
            await release.wait()

    task = asyncio.create_task(hold())
    await asyncio.sleep(0)  # 让 holder 先拿到锁
    await asyncio.sleep(0.01)

    with pytest.raises(MutationLockError):
        async with waiter.mutate(reason="waiter"):
            pass

    release.set()
    await task


def test_read_version_tracker_records_text_absent_and_clear():
    tracker = ReadVersionTracker()
    path = "/tmp/example.txt"

    assert tracker.is_tracked(path) is False
    assert tracker.expected(path) is ReadVersionTracker._UNSET  # noqa: SLF001

    tracker.record_read(path, "hello")
    assert tracker.is_tracked(path) is True
    assert tracker.expected(path) == ReadVersionTracker.digest_text("hello")

    tracker.record_absent(path)
    assert tracker.expected(path) is None

    tracker.clear()
    assert tracker.is_tracked(path) is False


def test_atomic_write_creates_and_replaces_atomically(tmp_path):
    target = tmp_path / "dir" / "file.txt"

    atomic_write_text(target, "v1")
    assert target.read_text(encoding="utf-8") == "v1"

    atomic_write_text(target, "v2")
    assert target.read_text(encoding="utf-8") == "v2"
    # 临时文件已清理
    assert list(target.parent.glob(".meow-*.tmp")) == []


def test_atomic_write_failure_keeps_old_content_and_cleans_temp(tmp_path, monkeypatch):
    target = tmp_path / "file.txt"
    target.write_text("old", encoding="utf-8")

    def _broken_replace(src, dst):
        raise OSError("replace boom")

    monkeypatch.setattr(mutation_module.os, "replace", _broken_replace)

    with pytest.raises(OSError):
        atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".meow-*.tmp")) == []
