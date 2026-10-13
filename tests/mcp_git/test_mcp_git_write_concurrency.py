"""GM3 并发写测试(§9.2: 并发写不损坏 index, 串行化与锁超时生效)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from meowmeowclaw_mcp_git.app import GitMcpApp
from meowmeowclaw_mcp_git.config import ServerConfig
from meowmeowclaw_mcp_git.locks import RepoLockManager


def make_app(repo: Path, **overrides) -> GitMcpApp:
    overrides.setdefault("read_only", False)
    return GitMcpApp(ServerConfig(roots=(str(repo),), **overrides))


async def call(app: GitMcpApp, tool: str, **arguments) -> dict:
    return json.loads(await app.call_tool(tool, arguments))


class TestInProcessSerialization:
    @pytest.mark.asyncio
    async def test_concurrent_stage_keeps_index_intact(self, write_repo: Path, git):
        app = make_app(write_repo)
        first = [f"a{index}.txt" for index in range(5)]
        second = [f"b{index}.txt" for index in range(5)]
        for name in (*first, *second):
            (write_repo / name).write_text(f"{name}\n", encoding="utf-8")

        results = await asyncio.gather(
            *(call(app, "git_stage", paths=[name]) for name in first),
            *(call(app, "git_stage", paths=[name]) for name in second),
        )

        assert all(item["ok"] is True for item in results)
        staged = git(write_repo, "diff", "--cached", "--name-only").stdout.split()
        assert sorted(staged) == sorted([*first, *second])
        assert not (write_repo / ".git" / "index.lock").exists()

    @pytest.mark.asyncio
    async def test_second_write_waits_for_lock(self, write_repo: Path):
        app = make_app(write_repo)
        (write_repo / "late.txt").write_text("late\n", encoding="utf-8")
        entry = app.registry.resolve(None)

        async with app.locks.acquire(entry):
            pending = asyncio.create_task(call(app, "git_stage", paths=["late.txt"]))
            await asyncio.sleep(0.05)
            assert pending.done() is False  # 被仓库锁挡住
        result = await pending

        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_lock_timeout_is_enveloped(self, write_repo: Path):
        locks = RepoLockManager(timeout=0.05)
        app = GitMcpApp(ServerConfig(roots=(str(write_repo),), read_only=False), locks=locks)
        entry = app.registry.resolve(None)

        async with app.locks.acquire(entry):
            payload = await call(app, "git_stage", all=True)

        assert payload["ok"] is False
        assert payload["error_code"] == "lock_timeout"
        assert payload["meta"]["repo"] == write_repo.name

    @pytest.mark.asyncio
    async def test_lock_released_after_failed_write(self, write_repo: Path):
        app = make_app(write_repo)

        denied = await call(app, "git_commit", message="empty")  # 锁内失败: 无暂存变更
        (write_repo / "app.py").write_text("changed\n", encoding="utf-8")
        await call(app, "git_stage", paths=["app.py"])
        committed = await call(app, "git_commit", message="feat: after failure")

        assert denied["error_code"] == "invalid_argument"
        assert committed["ok"] is True  # 锁没有被失败路径泄漏

    @pytest.mark.asyncio
    async def test_concurrent_commits_serialize_without_corruption(self, write_repo: Path, git):
        app = make_app(write_repo)
        (write_repo / "one.txt").write_text("one\n", encoding="utf-8")
        (write_repo / "two.txt").write_text("two\n", encoding="utf-8")
        await call(app, "git_stage", paths=["one.txt", "two.txt"])

        # 两个 commit 同时发: 一个成功, 另一个因为暂存区已被清空而稳定失败; index 不损坏
        results = await asyncio.gather(
            call(app, "git_commit", message="feat: first"),
            call(app, "git_commit", message="feat: second"),
        )

        assert sorted(item["ok"] for item in results) == [False, True]
        assert git(write_repo, "fsck", "--no-progress").returncode == 0
        assert git(write_repo, "status", "--porcelain").stdout.strip() == ""


class TestCrossProcessSerialization:
    @pytest.mark.asyncio
    async def test_second_server_times_out_on_lock(self, write_repo: Path, tmp_path: Path):
        lock_dir = str(tmp_path / "locks")
        first = GitMcpApp(
            ServerConfig(roots=(str(write_repo),), read_only=False),
            locks=RepoLockManager(lock_dir=lock_dir, timeout=1.0),
        )
        second = GitMcpApp(
            ServerConfig(roots=(str(write_repo),), read_only=False),
            locks=RepoLockManager(lock_dir=lock_dir, timeout=0.05),
        )
        entry = first.registry.resolve(None)

        async with first.locks.acquire(entry):
            payload = await call(second, "git_stage", all=True)

        assert payload["error_code"] == "lock_timeout"

    @pytest.mark.asyncio
    async def test_second_server_succeeds_after_release(self, write_repo: Path, tmp_path: Path):
        lock_dir = str(tmp_path / "locks")
        first = GitMcpApp(
            ServerConfig(roots=(str(write_repo),), read_only=False),
            locks=RepoLockManager(lock_dir=lock_dir, timeout=1.0),
        )
        second = GitMcpApp(
            ServerConfig(roots=(str(write_repo),), read_only=False),
            locks=RepoLockManager(lock_dir=lock_dir, timeout=1.0),
        )
        entry = first.registry.resolve(None)
        (write_repo / "app.py").write_text("changed\n", encoding="utf-8")

        async with first.locks.acquire(entry):
            pending = asyncio.create_task(call(second, "git_stage", paths=["app.py"]))
            await asyncio.sleep(0.05)
            assert pending.done() is False
        payload = await pending

        assert payload["ok"] is True
