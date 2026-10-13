"""GM4 ``git_fetch`` 端到端单测(本地 bare remote, 不依赖网络)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meowmeowclaw_mcp_git.app import GitMcpApp
from meowmeowclaw_mcp_git.config import ServerConfig


def make_app(repo: Path, **overrides) -> GitMcpApp:
    overrides.setdefault("allow_remote", True)
    return GitMcpApp(ServerConfig(roots=(str(repo),), **overrides))


async def call(app: GitMcpApp, tool: str, **arguments) -> dict:
    return json.loads(await app.call_tool(tool, arguments))


def commit_and_push(git, other: Path, name: str, content: str, message: str) -> None:
    (other / name).write_text(content, encoding="utf-8")
    git(other, "add", name)
    git(other, "commit", "-qm", message)
    git(other, "push", "-q", "origin", "main")


class TestRegistration:
    def test_not_registered_by_default(self, remote_pair):
        work, _remote, _other = remote_pair
        app = GitMcpApp(ServerConfig(roots=(str(work),)))

        assert "git_fetch" not in app.tool_names()

    def test_registered_when_allow_remote(self, remote_pair):
        work, _remote, _other = remote_pair
        app = make_app(work)

        assert "git_fetch" in app.tool_names()
        assert len(app.tool_names()) == 14

    def test_registered_alongside_write_tools(self, remote_pair):
        work, _remote, _other = remote_pair
        app = GitMcpApp(ServerConfig(roots=(str(work),), read_only=False, allow_remote=True))

        assert len(app.tool_names()) == 22
        assert {"git_fetch", "git_commit", "git_status"} <= set(app.tool_names())


class TestFetch:
    @pytest.mark.asyncio
    async def test_fetch_updates_tracking_ref_without_merging(self, remote_pair, git):
        work, _remote, other = remote_pair
        commit_and_push(git, other, "remote.txt", "remote\n", "feat: remote work")

        payload = await call(make_app(work), "git_fetch", remote="origin")

        assert payload["ok"] is True
        assert payload["data"]["remote"] == "origin"
        refs = {item["name"]: item for item in payload["data"]["refs"]}
        assert refs["origin/main"]["change"] == "updated"
        assert refs["origin/main"]["new_oid"]
        # fetch 不合并: 本地 main 还停在旧 commit, 工作区没有 remote.txt
        assert (work / "remote.txt").exists() is False
        assert payload["data"]["after"]["branch"] == "main"
        assert payload["data"]["refs_changed"] >= 1

    @pytest.mark.asyncio
    async def test_second_fetch_reports_no_change(self, remote_pair):
        work, _remote, _other = remote_pair
        app = make_app(work)

        first = await call(app, "git_fetch", remote="origin")
        second = await call(app, "git_fetch", remote="origin")

        assert first["data"]["refs_changed"] == 0  # 已经与远端同步
        assert second["data"]["refs_changed"] == 0
        assert second["data"]["output"] == ""

    @pytest.mark.asyncio
    async def test_fetch_without_remote_uses_default(self, remote_pair, git):
        work, _remote, other = remote_pair
        commit_and_push(git, other, "remote.txt", "remote\n", "feat: remote work")

        payload = await call(make_app(work), "git_fetch")

        assert payload["ok"] is True
        assert payload["data"]["remote"] is None
        assert any(item["name"] == "origin/main" for item in payload["data"]["refs"])

    @pytest.mark.asyncio
    async def test_remote_head_symref_is_not_reported(self, remote_pair, git):
        """回归(CI git ≥2.46): fetch 自动创建的 refs/remotes/origin/HEAD 是符号引用,
        其短名会被缩写成 "origin"; 它不应出现在 ref 变化里, 真实分支更新仍要报告."""
        work, _remote, other = remote_pair
        # 显式造出远端 HEAD 符号引用: 老 git 默认不创建, 用它确定性地模拟新 git 的行为
        git(work, "remote", "set-head", "origin", "--auto")
        assert git(work, "symbolic-ref", "refs/remotes/origin/HEAD").returncode == 0
        (other / "remote.txt").write_text("remote\n", encoding="utf-8")
        git(other, "add", "remote.txt")
        git(other, "commit", "-qm", "feat: remote work")
        git(other, "push", "-q", "origin", "main")
        app = make_app(work)

        first = await call(app, "git_fetch", remote="origin")
        second = await call(app, "git_fetch", remote="origin")

        assert [item["name"] for item in first["data"]["refs"]] == ["origin/main"]
        assert first["data"]["refs_changed"] == 1
        assert second["data"]["refs_changed"] == 0
        assert second["data"]["refs"] == []

    @pytest.mark.asyncio
    async def test_prune_deletes_removed_tracking_branch(self, remote_pair, git):
        work, _remote, other = remote_pair
        git(other, "checkout", "-qb", "feature")
        (other / "f.txt").write_text("f\n", encoding="utf-8")
        git(other, "add", "f.txt")
        git(other, "commit", "-qm", "feat: feature")
        git(other, "push", "-qu", "origin", "feature")
        app = make_app(work)
        created = await call(app, "git_fetch", remote="origin")
        assert [item["name"] for item in created["data"]["refs"]] == ["origin/feature"]

        git(other, "push", "-q", "origin", ":feature")
        no_prune = await call(app, "git_fetch", remote="origin")
        pruned = await call(app, "git_fetch", remote="origin", prune=True)

        assert no_prune["data"]["refs_changed"] == 0  # 不带 --prune 时保留
        assert pruned["data"]["refs"] == [
            {
                "name": "origin/feature",
                "change": "deleted",
                "old_oid": pruned["data"]["refs"][0]["old_oid"],
                "new_oid": None,
            }
        ]

    @pytest.mark.asyncio
    async def test_before_after_and_meta(self, remote_pair):
        work, _remote, _other = remote_pair

        payload = await call(make_app(work), "git_fetch", remote="origin")

        assert set(payload["data"]["before"]) == set(payload["data"]["after"])
        assert payload["data"]["before"]["branch"] == "main"
        assert payload["meta"]["command"][4:] == ["fetch", "origin"]
        assert payload["meta"]["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_invalid_remote_name_rejected(self, remote_pair):
        work, _remote, _other = remote_pair

        payload = await call(make_app(work), "git_fetch", remote="--all")

        assert payload["error_code"] == "invalid_argument"

    @pytest.mark.asyncio
    async def test_unknown_remote_fails_stably(self, remote_pair):
        work, _remote, _other = remote_pair

        payload = await call(make_app(work), "git_fetch", remote="ghost")

        assert payload["ok"] is False
        assert payload["error_code"] == "command_failed"
        assert payload["meta"]["repo"] == work.name

    @pytest.mark.asyncio
    async def test_error_message_masks_credentials(self, remote_pair, git):
        work, _remote, _other = remote_pair
        git(work, "remote", "add", "broken", "https://user:tok3n@127.0.0.1:1/repo.git")

        payload = await call(make_app(work), "git_fetch", remote="broken")

        text = json.dumps(payload, ensure_ascii=False)
        assert payload["error_code"] == "command_failed"
        # git 自身通常不回显 userinfo; 这里保证任何情况下 token 都不会出现在工具返回里
        assert "tok3n" not in text

    @pytest.mark.asyncio
    async def test_read_only_mode_still_allows_fetch(self, remote_pair):
        """fetch 由 --allow-remote 控制, 与 read_only 无关(默认只读也允许显式开启远端)."""
        work, _remote, _other = remote_pair
        app = GitMcpApp(ServerConfig(roots=(str(work),), read_only=True, allow_remote=True))

        payload = await call(app, "git_fetch", remote="origin")

        assert payload["ok"] is True
