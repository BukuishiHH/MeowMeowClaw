"""GM3 安全写工具端到端单测(stage/unstage/restore/commit/branch/stash)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meowmeowclaw_mcp_git.app import GitMcpApp
from meowmeowclaw_mcp_git.config import ServerConfig

STATE_KEYS = {
    "branch",
    "head_oid",
    "detached",
    "unborn",
    "dirty_files",
    "staged",
    "unstaged",
    "untracked",
    "conflicts",
}


def make_app(repo: Path, **overrides) -> GitMcpApp:
    overrides.setdefault("read_only", False)
    return GitMcpApp(ServerConfig(roots=(str(repo),), **overrides))


async def call(app: GitMcpApp, tool: str, **arguments) -> dict:
    return json.loads(await app.call_tool(tool, arguments))


def assert_before_after(payload: dict) -> None:
    assert set(payload["data"]["before"]) == STATE_KEYS
    assert set(payload["data"]["after"]) == STATE_KEYS


class TestStage:
    @pytest.mark.asyncio
    async def test_stage_paths(self, write_repo: Path):
        (write_repo / "app.py").write_text("changed\n", encoding="utf-8")

        payload = await call(make_app(write_repo), "git_stage", paths=["app.py"])

        assert payload["ok"] is True
        assert_before_after(payload)
        assert payload["data"]["before"]["staged"] == 0
        assert payload["data"]["after"]["staged"] == 1
        assert payload["data"]["paths"] == ["app.py"]

    @pytest.mark.asyncio
    async def test_stage_all_includes_untracked_and_deletions(self, write_repo: Path):
        (write_repo / "app.py").unlink()
        (write_repo / "new.txt").write_text("new\n", encoding="utf-8")

        payload = await call(make_app(write_repo), "git_stage", all=True)

        assert payload["data"]["after"]["staged"] == 2
        assert payload["data"]["all"] is True

    @pytest.mark.asyncio
    async def test_stage_requires_paths_or_all(self, write_repo: Path):
        payload = await call(make_app(write_repo), "git_stage")

        assert payload["error_code"] == "invalid_argument"

    @pytest.mark.asyncio
    async def test_stage_all_with_paths_rejected(self, write_repo: Path):
        payload = await call(make_app(write_repo), "git_stage", all=True, paths=["app.py"])

        assert payload["error_code"] == "invalid_argument"

    @pytest.mark.asyncio
    async def test_stage_path_escape_rejected(self, write_repo: Path):
        payload = await call(make_app(write_repo), "git_stage", paths=["../outside.txt"])

        assert payload["error_code"] == "invalid_path"
        assert payload["meta"]["repo"] == write_repo.name

    @pytest.mark.asyncio
    async def test_meta_records_add_command(self, write_repo: Path):
        (write_repo / "app.py").write_text("changed\n", encoding="utf-8")

        payload = await call(make_app(write_repo), "git_stage", paths=["app.py"])

        assert payload["meta"]["command"][4:] == ["add", "--", "app.py"]


class TestUnstage:
    @pytest.mark.asyncio
    async def test_unstage_after_stage(self, write_repo: Path):
        (write_repo / "app.py").write_text("changed\n", encoding="utf-8")
        app = make_app(write_repo)
        await call(app, "git_stage", paths=["app.py"])

        payload = await call(app, "git_unstage", paths=["app.py"])

        assert payload["ok"] is True
        assert_before_after(payload)
        assert payload["data"]["after"]["staged"] == 0
        assert payload["data"]["after"]["unstaged"] == 1

    @pytest.mark.asyncio
    async def test_unstage_in_unborn_repo(self, empty_repo: Path):
        (empty_repo / "first.txt").write_text("first\n", encoding="utf-8")
        app = make_app(empty_repo)
        await call(app, "git_stage", paths=["first.txt"])

        payload = await call(app, "git_unstage", paths=["first.txt"])

        assert payload["ok"] is True
        assert payload["data"]["before"]["unborn"] is True
        assert payload["data"]["after"]["staged"] == 0
        assert payload["data"]["after"]["untracked"] == 1

    @pytest.mark.asyncio
    async def test_unstage_requires_paths(self, write_repo: Path):
        payload = await call(make_app(write_repo), "git_unstage")

        assert payload["error_code"] == "invalid_argument"


class TestRestore:
    @pytest.mark.asyncio
    async def test_restore_worktree_requires_force_when_dirty(self, write_repo: Path):
        (write_repo / "app.py").write_text("dirty\n", encoding="utf-8")
        app = make_app(write_repo)

        denied = await call(app, "git_restore", paths=["app.py"])
        forced = await call(app, "git_restore", paths=["app.py"], force=True)

        assert denied["error_code"] == "dirty_worktree"
        assert denied["meta"]["repo"] == write_repo.name
        assert forced["ok"] is True
        assert (write_repo / "app.py").read_text(encoding="utf-8") == "line1\nline2\n"
        assert forced["data"]["after"]["dirty_files"] == 0

    @pytest.mark.asyncio
    async def test_restore_staged_unstages_without_force(self, write_repo: Path):
        (write_repo / "app.py").write_text("changed\n", encoding="utf-8")
        app = make_app(write_repo)
        await call(app, "git_stage", paths=["app.py"])

        payload = await call(app, "git_restore", paths=["app.py"], staged=True)

        assert payload["ok"] is True
        assert payload["data"]["staged"] is True
        assert payload["data"]["after"]["staged"] == 0
        assert (write_repo / "app.py").read_text(encoding="utf-8") == "changed\n"

    @pytest.mark.asyncio
    async def test_restore_clean_repo_is_noop(self, write_repo: Path):
        payload = await call(make_app(write_repo), "git_restore", paths=["app.py"])

        assert payload["ok"] is True
        assert payload["data"]["after"]["dirty_files"] == 0

    @pytest.mark.asyncio
    async def test_restore_path_escape(self, write_repo: Path):
        payload = await call(make_app(write_repo), "git_restore", paths=["/etc/passwd"], force=True)

        assert payload["error_code"] == "invalid_path"


class TestCommit:
    @pytest.mark.asyncio
    async def test_commit_happy_path(self, write_repo: Path, git):
        (write_repo / "app.py").write_text("line1 changed\nline2\n", encoding="utf-8")
        app = make_app(write_repo)
        await call(app, "git_stage", paths=["app.py"])

        payload = await call(app, "git_commit", message="feat: change app")

        assert payload["ok"] is True
        assert_before_after(payload)
        data = payload["data"]
        assert data["before"]["head_oid"] != data["after"]["head_oid"]
        assert data["after"]["dirty_files"] == 0
        assert data["message"] == "feat: change app"
        assert git(write_repo, "log", "-1", "--format=%s").stdout.strip() == "feat: change app"

    @pytest.mark.asyncio
    async def test_commit_requires_message(self, write_repo: Path):
        app = make_app(write_repo)

        missing = await call(app, "git_commit")
        blank = await call(app, "git_commit", message="   ")

        assert missing["error_code"] == "invalid_argument"
        assert blank["error_code"] == "invalid_argument"

    @pytest.mark.asyncio
    async def test_commit_empty_index_rejected(self, write_repo: Path):
        payload = await call(make_app(write_repo), "git_commit", message="nothing")

        assert payload["error_code"] == "invalid_argument"
        assert "allow_empty" in payload["message"]

    @pytest.mark.asyncio
    async def test_commit_allow_empty(self, write_repo: Path, git):
        before_count = int(git(write_repo, "rev-list", "--count", "HEAD").stdout.strip())

        payload = await call(make_app(write_repo), "git_commit", message="chore: empty", allow_empty=True)

        after_count = int(git(write_repo, "rev-list", "--count", "HEAD").stdout.strip())
        assert payload["ok"] is True
        assert after_count == before_count + 1

    @pytest.mark.asyncio
    async def test_commit_on_protected_branch_rejected(self, repo: Path, git):
        (repo / "README.md").write_text("changed\n", encoding="utf-8")
        git(repo, "add", "README.md")
        app = make_app(repo)

        denied = await call(app, "git_commit", message="feat: on main")
        allowed = await call(make_app(repo, protected_branches=()), "git_commit", message="feat: on main")

        assert denied["error_code"] == "protected_branch"
        assert denied["meta"]["repo"] == repo.name
        assert allowed["ok"] is True

    @pytest.mark.asyncio
    async def test_commit_amend(self, write_repo: Path, git):
        (write_repo / "app.py").write_text("v2\n", encoding="utf-8")
        app = make_app(write_repo)
        await call(app, "git_stage", paths=["app.py"])
        await call(app, "git_commit", message="first message")
        (write_repo / "app.py").write_text("v3\n", encoding="utf-8")
        await call(app, "git_stage", paths=["app.py"])

        payload = await call(app, "git_commit", message="amended message", amend=True)

        assert payload["ok"] is True
        assert payload["data"]["amend"] is True
        assert git(write_repo, "log", "-1", "--format=%s").stdout.strip() == "amended message"
        assert git(write_repo, "rev-list", "--count", "HEAD").stdout.strip() == "2"
        assert git(write_repo, "status", "--porcelain").stdout.strip() == ""

    @pytest.mark.asyncio
    async def test_commit_signoff(self, write_repo: Path, git):
        (write_repo / "app.py").write_text("signed\n", encoding="utf-8")
        app = make_app(write_repo)
        await call(app, "git_stage", paths=["app.py"])

        payload = await call(app, "git_commit", message="feat: signed", signoff=True)

        body = git(write_repo, "log", "-1", "--format=%B").stdout
        assert payload["data"]["signoff"] is True
        assert "Signed-off-by: Writer <writer@example.com>" in body

    @pytest.mark.asyncio
    async def test_commit_paths_mode_commits_worktree_content(self, write_repo: Path, git):
        (write_repo / "app.py").write_text("paths mode\n", encoding="utf-8")
        (write_repo / "other.txt").write_text("untracked\n", encoding="utf-8")

        payload = await call(make_app(write_repo), "git_commit", message="feat: paths", paths=["app.py"])

        assert payload["ok"] is True
        assert payload["data"]["paths"] == ["app.py"]
        assert git(write_repo, "show", "--name-only", "--format=", "HEAD").stdout.strip() == "app.py"
        assert git(write_repo, "status", "--porcelain").stdout.strip() == "?? other.txt"

    @pytest.mark.asyncio
    async def test_commit_path_escape(self, write_repo: Path):
        payload = await call(make_app(write_repo), "git_commit", message="x", paths=["../outside"])

        assert payload["error_code"] == "invalid_path"


class TestBranchCreate:
    @pytest.mark.asyncio
    async def test_create_without_switch(self, write_repo: Path, git):
        payload = await call(make_app(write_repo), "git_branch_create", name="feat/new")

        assert payload["ok"] is True
        assert payload["data"]["after"]["branch"] == "work"
        assert payload["data"]["switch"] is False
        assert "feat/new" in git(write_repo, "branch", "--list", "--format=%(refname:short)").stdout

    @pytest.mark.asyncio
    async def test_create_with_switch(self, write_repo: Path):
        payload = await call(make_app(write_repo), "git_branch_create", name="feat/switch", switch=True)

        assert payload["data"]["after"]["branch"] == "feat/switch"
        assert payload["data"]["switch"] is True

    @pytest.mark.asyncio
    async def test_create_from_start_point(self, write_repo: Path, git):
        git(write_repo, "tag", "base-tag")

        payload = await call(make_app(write_repo), "git_branch_create", name="feat/from-tag", start_point="base-tag")

        assert payload["ok"] is True
        assert payload["data"]["start_point"] == "base-tag"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["bad name", "-x", "a@{1}", "x..y"])
    async def test_invalid_names(self, write_repo: Path, name: str):
        payload = await call(make_app(write_repo), "git_branch_create", name=name)

        assert payload["ok"] is False
        assert payload["error_code"] == "invalid_argument"

    @pytest.mark.asyncio
    async def test_duplicate_name_fails_stably(self, write_repo: Path):
        app = make_app(write_repo)
        await call(app, "git_branch_create", name="feat/dup")

        payload = await call(app, "git_branch_create", name="feat/dup")

        assert payload["error_code"] == "command_failed"


class TestBranchSwitch:
    @pytest.mark.asyncio
    async def test_switch_happy_path(self, write_repo: Path):
        app = make_app(write_repo)
        await call(app, "git_branch_create", name="feat/target")

        payload = await call(app, "git_branch_switch", name="feat/target")

        assert payload["ok"] is True
        assert payload["data"]["before"]["branch"] == "work"
        assert payload["data"]["after"]["branch"] == "feat/target"

    @pytest.mark.asyncio
    async def test_switch_dirty_rejected_then_forced(self, write_repo: Path):
        app = make_app(write_repo)
        await call(app, "git_branch_create", name="feat/target")
        (write_repo / "app.py").write_text("dirty\n", encoding="utf-8")

        denied = await call(app, "git_branch_switch", name="feat/target")
        forced = await call(app, "git_branch_switch", name="feat/target", force=True)

        assert denied["error_code"] == "dirty_worktree"
        assert forced["ok"] is True
        assert forced["data"]["after"]["branch"] == "feat/target"
        assert forced["data"]["after"]["dirty_files"] == 0

    @pytest.mark.asyncio
    async def test_switch_missing_branch(self, write_repo: Path):
        payload = await call(make_app(write_repo), "git_branch_switch", name="does-not-exist")

        assert payload["error_code"] == "command_failed"


class TestBranchDelete:
    @pytest.mark.asyncio
    async def test_delete_merged_branch(self, write_repo: Path):
        app = make_app(write_repo)
        await call(app, "git_branch_create", name="feat/merged")

        payload = await call(app, "git_branch_delete", name="feat/merged")

        assert payload["ok"] is True
        assert payload["data"]["force"] is False

    @pytest.mark.asyncio
    async def test_delete_current_branch_rejected(self, write_repo: Path):
        payload = await call(make_app(write_repo), "git_branch_delete", name="work")

        assert payload["error_code"] == "policy_denied"

    @pytest.mark.asyncio
    async def test_delete_protected_branch_rejected(self, write_repo: Path, git):
        git(write_repo, "branch", "feat/protected")

        payload = await call(
            make_app(write_repo, protected_branches=("feat/protected",)),
            "git_branch_delete",
            name="feat/protected",
        )

        assert payload["error_code"] == "protected_branch"

    @pytest.mark.asyncio
    async def test_delete_unmerged_rejected_then_forced(self, write_repo: Path):
        app = make_app(write_repo)
        await call(app, "git_branch_create", name="feat/unmerged", switch=True)
        (write_repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        await call(app, "git_stage", paths=["feature.txt"])
        await call(app, "git_commit", message="feat: unmerged work")
        await call(app, "git_branch_switch", name="work")

        denied = await call(app, "git_branch_delete", name="feat/unmerged")
        forced = await call(app, "git_branch_delete", name="feat/unmerged", force=True)

        assert denied["error_code"] == "policy_denied"
        assert forced["ok"] is True
        assert forced["data"]["force"] is True

    @pytest.mark.asyncio
    async def test_delete_missing_branch(self, write_repo: Path):
        payload = await call(make_app(write_repo), "git_branch_delete", name="ghost")

        assert payload["error_code"] == "command_failed"


class TestStash:
    @pytest.mark.asyncio
    async def test_list_empty(self, write_repo: Path):
        payload = await call(make_app(write_repo), "git_stash", action="list")

        assert payload["ok"] is True
        assert payload["data"] == {"action": "list", "entries": [], "count": 0}

    @pytest.mark.asyncio
    async def test_push_creates_stash_and_cleans_worktree(self, write_repo: Path):
        (write_repo / "app.py").write_text("wip\n", encoding="utf-8")
        app = make_app(write_repo)

        payload = await call(app, "git_stash", action="push", message="wip message")
        listing = await call(app, "git_stash", action="list")

        assert payload["ok"] is True
        assert_before_after(payload)
        assert payload["data"]["created"] is True
        assert payload["data"]["after"]["dirty_files"] == 0
        assert listing["data"]["count"] == 1
        assert "wip message" in listing["data"]["entries"][0]["subject"]
        assert listing["data"]["entries"][0]["selector"] == "stash@{0}"

    @pytest.mark.asyncio
    async def test_push_without_changes_reports_not_created(self, write_repo: Path):
        payload = await call(make_app(write_repo), "git_stash", action="push")

        assert payload["ok"] is True
        assert payload["data"]["created"] is False

    @pytest.mark.asyncio
    async def test_push_with_paths_keeps_untracked(self, write_repo: Path):
        (write_repo / "app.py").write_text("wip\n", encoding="utf-8")
        (write_repo / "new.txt").write_text("new\n", encoding="utf-8")

        payload = await call(make_app(write_repo), "git_stash", action="push", paths=["app.py"])

        assert payload["ok"] is True
        assert payload["data"]["after"]["untracked"] == 1
        assert payload["data"]["after"]["unstaged"] == 0

    @pytest.mark.asyncio
    async def test_pop_restores_and_removes_entry(self, write_repo: Path):
        (write_repo / "app.py").write_text("wip\n", encoding="utf-8")
        app = make_app(write_repo)
        await call(app, "git_stash", action="push")

        payload = await call(app, "git_stash", action="pop")
        listing = await call(app, "git_stash", action="list")

        assert payload["ok"] is True
        assert (write_repo / "app.py").read_text(encoding="utf-8") == "wip\n"
        assert listing["data"]["count"] == 0

    @pytest.mark.asyncio
    async def test_apply_keeps_entry(self, write_repo: Path):
        (write_repo / "app.py").write_text("wip\n", encoding="utf-8")
        app = make_app(write_repo)
        await call(app, "git_stash", action="push")

        payload = await call(app, "git_stash", action="apply")
        listing = await call(app, "git_stash", action="list")

        assert payload["ok"] is True
        assert listing["data"]["count"] == 1
        assert (write_repo / "app.py").read_text(encoding="utf-8") == "wip\n"

    @pytest.mark.asyncio
    async def test_apply_rejected_when_dirty(self, write_repo: Path):
        (write_repo / "app.py").write_text("wip\n", encoding="utf-8")
        app = make_app(write_repo)
        await call(app, "git_stash", action="push")
        (write_repo / "app.py").write_text("dirty again\n", encoding="utf-8")

        payload = await call(app, "git_stash", action="apply")

        assert payload["error_code"] == "dirty_worktree"

    @pytest.mark.asyncio
    async def test_drop_requires_confirm(self, write_repo: Path):
        (write_repo / "app.py").write_text("wip\n", encoding="utf-8")
        app = make_app(write_repo)
        await call(app, "git_stash", action="push")

        denied = await call(app, "git_stash", action="drop")
        allowed = await call(app, "git_stash", action="drop", confirm=True)
        listing = await call(app, "git_stash", action="list")

        assert denied["error_code"] == "policy_denied"
        assert allowed["ok"] is True
        assert listing["data"]["count"] == 0

    @pytest.mark.asyncio
    async def test_drop_without_stash_fails_stably(self, write_repo: Path):
        payload = await call(make_app(write_repo), "git_stash", action="drop", confirm=True)

        assert payload["error_code"] == "command_failed"

    @pytest.mark.asyncio
    async def test_action_argument_validation(self, write_repo: Path):
        app = make_app(write_repo)

        bad_action = await call(app, "git_stash", action="clear")
        message_on_list = await call(app, "git_stash", action="list", message="x")
        paths_on_pop = await call(app, "git_stash", action="pop", paths=["app.py"])
        confirm_on_push = await call(app, "git_stash", action="push", confirm=True)

        assert bad_action["error_code"] == "invalid_argument"
        assert message_on_list["error_code"] == "invalid_argument"
        assert paths_on_pop["error_code"] == "invalid_argument"
        assert confirm_on_push["error_code"] == "invalid_argument"


class TestReadOnlyRegistration:
    @pytest.mark.asyncio
    async def test_write_tools_not_registered_by_default(self, write_repo: Path):
        app = GitMcpApp(ServerConfig(roots=(str(write_repo),), read_only=True))

        assert "git_commit" not in app.tool_names()
        payload = await call(app, "git_commit", message="x")

        assert payload["error_code"] == "invalid_argument"
        assert "未知工具" in payload["message"]

    @pytest.mark.asyncio
    async def test_error_meta_carries_repo_when_repo_omitted(self, write_repo: Path):
        """单仓库模式下 repo 省略时, 策略错误信封也应带 alias(便于定位)."""
        app = make_app(write_repo, protected_branches=("work",))
        (write_repo / "app.py").write_text("dirty\n", encoding="utf-8")
        await call(app, "git_stage", paths=["app.py"])

        payload = await call(app, "git_commit", message="x")

        assert payload["error_code"] == "protected_branch"
        assert payload["meta"]["repo"] == write_repo.name
