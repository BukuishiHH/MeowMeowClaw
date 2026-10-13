"""WriteFileTool/ReadFileTool 的 SA3 竞态护栏测试.

覆盖 docs/SUBAGENT_DESIGN.md §6.2–§6.4:
- 主代理(无 guard/tracker)保持旧覆盖语义;
- 子代理读后写 OCC(未读拒绝/读后可写/stale 冲突/read absent);
- 变更锁 + 原子替换; 锁超时转工具错误;
- subagents/ 运行时目录对文件工具拒绝。
"""

import asyncio

import pytest

from meowmeowclaw.tools.filesystem import ReadFileTool, WriteFileTool
from meowmeowclaw.tools.mutation import ReadVersionTracker, WorkspaceMutationGuard


def make_tools(tmp_path, *, guard=None, tracker=None):
    workspace = tmp_path / "ws"
    memory = workspace / "memory"
    return (
        ReadFileTool(workspace, memory, version_tracker=tracker),
        WriteFileTool(workspace, memory, mutation_guard=guard, version_tracker=tracker),
        workspace,
        memory,
    )


class TestMainAgentLegacyBehavior:
    @pytest.mark.asyncio
    async def test_write_without_guard_overwrites_existing_file(self, tmp_path):
        _, writer, workspace, _ = make_tools(tmp_path)
        target = workspace / "a.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("old", encoding="utf-8")

        result = await writer.execute(file_path="a.txt", content="new")

        assert "[成功]" in result
        assert target.read_text(encoding="utf-8") == "new"

    @pytest.mark.asyncio
    async def test_write_with_guard_is_atomic_and_cleans_temp(self, tmp_path):
        workspace = tmp_path / "ws"
        memory = tmp_path / "memory"
        guard = WorkspaceMutationGuard(tmp_path / "workspace.lock", timeout=1.0)
        writer = WriteFileTool(workspace, memory, mutation_guard=guard)

        result = await writer.execute(file_path="dir/a.txt", content="v1")

        assert "[成功]" in result
        assert (workspace / "dir" / "a.txt").read_text(encoding="utf-8") == "v1"
        assert list((workspace / "dir").glob(".meow-*.tmp")) == []


class TestSubagentOCC:
    @pytest.mark.asyncio
    async def test_unread_existing_file_write_is_rejected(self, tmp_path):
        tracker = ReadVersionTracker()
        _, writer, workspace, _ = make_tools(tmp_path, tracker=tracker)
        target = workspace / "a.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("old", encoding="utf-8")

        result = await writer.execute(file_path="a.txt", content="new")

        assert "未读取" in result or "read_file" in result
        assert target.read_text(encoding="utf-8") == "old"

    @pytest.mark.asyncio
    async def test_read_then_write_succeeds(self, tmp_path):
        tracker = ReadVersionTracker()
        reader, writer, workspace, _ = make_tools(tmp_path, tracker=tracker)
        target = workspace / "a.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("v1", encoding="utf-8")

        assert (await reader.execute(file_path="a.txt")) == "v1"
        result = await writer.execute(file_path="a.txt", content="v2")

        assert "[成功]" in result
        assert target.read_text(encoding="utf-8") == "v2"

    @pytest.mark.asyncio
    async def test_stale_write_is_rejected_after_external_change(self, tmp_path):
        tracker = ReadVersionTracker()
        reader, writer, workspace, _ = make_tools(tmp_path, tracker=tracker)
        target = workspace / "a.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("v1", encoding="utf-8")
        await reader.execute(file_path="a.txt")

        # 外部会话/进程在 read 之后写入 v2
        target.write_text("v2", encoding="utf-8")

        result = await writer.execute(file_path="a.txt", content="v3")

        assert "并发冲突" in result
        assert target.read_text(encoding="utf-8") == "v2"

    @pytest.mark.asyncio
    async def test_read_absent_then_create_succeeds(self, tmp_path):
        tracker = ReadVersionTracker()
        reader, writer, workspace, _ = make_tools(tmp_path, tracker=tracker)

        assert "文件不存在" in await reader.execute(file_path="new.txt")
        result = await writer.execute(file_path="new.txt", content="created")

        assert "[成功]" in result
        assert (workspace / "new.txt").read_text(encoding="utf-8") == "created"

    @pytest.mark.asyncio
    async def test_read_absent_then_external_create_is_conflict(self, tmp_path):
        tracker = ReadVersionTracker()
        reader, writer, workspace, _ = make_tools(tmp_path, tracker=tracker)
        await reader.execute(file_path="new.txt")  # 记录 ABSENT

        target = workspace / "new.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("external", encoding="utf-8")

        result = await writer.execute(file_path="new.txt", content="mine")

        assert "并发冲突" in result
        assert target.read_text(encoding="utf-8") == "external"

    @pytest.mark.asyncio
    async def test_own_write_updates_tracker_for_next_write(self, tmp_path):
        tracker = ReadVersionTracker()
        reader, writer, workspace, _ = make_tools(tmp_path, tracker=tracker)
        target = workspace / "a.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("v1", encoding="utf-8")
        await reader.execute(file_path="a.txt")

        assert "[成功]" in await writer.execute(file_path="a.txt", content="v2")
        assert "[成功]" in await writer.execute(file_path="a.txt", content="v3")
        assert target.read_text(encoding="utf-8") == "v3"

    def test_for_subagent_clone_keeps_guard_but_gets_new_tracker(self, tmp_path):
        guard = WorkspaceMutationGuard(tmp_path / "workspace.lock")
        base = WriteFileTool(tmp_path / "ws", tmp_path / "memory", mutation_guard=guard)
        tracker = ReadVersionTracker()

        clone = base.for_subagent(version_tracker=tracker)

        assert clone is not base
        assert clone._mutation_guard is guard  # noqa: SLF001
        assert clone._version_tracker is tracker  # noqa: SLF001


class TestLockTimeoutAndRuntimeDirs:
    @pytest.mark.asyncio
    async def test_write_lock_timeout_returns_tool_error(self, tmp_path):
        lock_path = tmp_path / "workspace.lock"
        holder_guard = WorkspaceMutationGuard(lock_path, timeout=1.0)
        waiter_guard = WorkspaceMutationGuard(lock_path, timeout=0.05)
        _, writer, workspace, _ = make_tools(tmp_path, guard=waiter_guard)
        release = asyncio.Event()

        async def hold() -> None:
            async with holder_guard.mutate(reason="hold"):
                await release.wait()

        task = asyncio.create_task(hold())
        await asyncio.sleep(0.01)

        result = await writer.execute(file_path="a.txt", content="x")

        assert "[变更锁超时]" in result
        assert not (workspace / "a.txt").exists()

        release.set()
        await task

    @pytest.mark.asyncio
    async def test_subagents_runtime_dir_is_denied(self, tmp_path):
        _, writer, workspace, _ = make_tools(tmp_path)

        result = await writer.execute(
            file_path="memory/subagents/audit.jsonl", content="x"
        )

        assert "[安全拦截]" in result
        assert not (workspace / "memory" / "subagents" / "audit.jsonl").exists()
