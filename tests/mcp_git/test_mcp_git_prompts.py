"""GM4 MCP Prompts 单测(§4.4): 生成提示词文本, 不执行写操作."""

from __future__ import annotations

from pathlib import Path

import pytest

from meowmeowclaw_mcp_git.app import GitMcpApp
from meowmeowclaw_mcp_git.config import ServerConfig
from meowmeowclaw_mcp_git.errors import GitMcpError


def make_app(repo: Path, **overrides) -> GitMcpApp:
    return GitMcpApp(ServerConfig(roots=(str(repo),), **overrides))


async def build(app: GitMcpApp, name: str, **arguments: str) -> str:
    return await app.build_prompt(name, arguments)


class TestDefinitions:
    def test_four_prompts_and_arguments(self, repo: Path):
        definitions = make_app(repo).prompt_definitions()

        assert [item["name"] for item in definitions] == [
            "write_commit_message",
            "review_staged_changes",
            "summarize_branch",
            "explain_conflict",
        ]
        required = {
            item["name"]: [arg["name"] for arg in item["arguments"] if arg["required"]]
            for item in definitions
        }
        assert required["summarize_branch"] == ["base"]
        assert required["explain_conflict"] == ["path"]
        assert all(item["description"] for item in definitions)

    @pytest.mark.asyncio
    async def test_unknown_prompt_rejected(self, repo: Path):
        with pytest.raises(ValueError, match="未知 prompt"):
            await build(make_app(repo), "no_such_prompt")


class TestWriteCommitMessage:
    @pytest.mark.asyncio
    async def test_staged_diff_is_included(self, write_repo: Path, git):
        (write_repo / "app.py").write_text("line1 changed\nline2\n", encoding="utf-8")
        git(write_repo, "add", "app.py")

        text = await build(make_app(write_repo), "write_commit_message")

        assert "app.py" in text
        assert "line1 changed" in text
        assert "暂存区" in text
        assert "conventional commits" in text

    @pytest.mark.asyncio
    async def test_worktree_scope(self, write_repo: Path):
        (write_repo / "app.py").write_text("unstaged\n", encoding="utf-8")

        staged_only = await build(make_app(write_repo), "write_commit_message")
        worktree = await build(make_app(write_repo), "write_commit_message", staged_only="false")

        assert "(无变更文件)" in staged_only
        assert "unstaged" in worktree
        assert "工作区" in worktree

    @pytest.mark.asyncio
    async def test_invalid_bool_argument(self, write_repo: Path):
        with pytest.raises(GitMcpError) as excinfo:
            await build(make_app(write_repo), "write_commit_message", staged_only="maybe")

        assert excinfo.value.code == "invalid_argument"


class TestReviewStagedChanges:
    @pytest.mark.asyncio
    async def test_review_contains_diff_and_instructions(self, write_repo: Path, git):
        (write_repo / "app.py").write_text("review me\n", encoding="utf-8")
        git(write_repo, "add", "app.py")

        text = await build(make_app(write_repo), "review_staged_changes")

        assert "评审" in text
        assert "review me" in text
        assert "blocker" in text and "测试缺口" in text


class TestSummarizeBranch:
    @pytest.mark.asyncio
    async def test_summary_contains_commits_and_files(self, write_repo: Path, git):
        git(write_repo, "checkout", "-qb", "feat/summary")
        (write_repo / "feature.py").write_text("feature\n", encoding="utf-8")
        git(write_repo, "add", "feature.py")
        git(write_repo, "commit", "-qm", "feat: add feature summary")

        text = await build(make_app(write_repo), "summarize_branch", base="work")

        assert "feat: add feature summary" in text
        assert "feature.py" in text
        assert "work..HEAD" in text

    @pytest.mark.asyncio
    async def test_base_required_and_validated(self, write_repo: Path):
        app = make_app(write_repo)

        with pytest.raises(GitMcpError) as missing:
            await build(app, "summarize_branch")
        with pytest.raises(GitMcpError) as bad:
            await build(app, "summarize_branch", base="--all")

        assert missing.value.code == "invalid_argument"
        assert bad.value.code == "invalid_argument"

    @pytest.mark.asyncio
    async def test_unknown_base_fails_stably(self, write_repo: Path):
        with pytest.raises(GitMcpError) as excinfo:
            await build(make_app(write_repo), "summarize_branch", base="no-such-ref")

        assert excinfo.value.code == "command_failed"


class TestExplainConflict:
    @pytest.mark.asyncio
    async def test_conflict_markers_are_included(self, conflict_repo: Path):
        text = await build(make_app(conflict_repo), "explain_conflict", path="app.py")

        assert "<<<<<<<" in text
        assert ">>>>>>>" in text
        assert "检测到冲突标记: 是" in text
        assert "不要自动修改任何文件" in text

    @pytest.mark.asyncio
    async def test_missing_and_escaped_paths(self, conflict_repo: Path):
        app = make_app(conflict_repo)

        with pytest.raises(GitMcpError) as missing:
            await build(app, "explain_conflict")
        with pytest.raises(GitMcpError) as escape:
            await build(app, "explain_conflict", path="../outside.txt")
        with pytest.raises(GitMcpError) as absent:
            await build(app, "explain_conflict", path="no/such/file.py")

        assert missing.value.code == "invalid_argument"
        assert escape.value.code == "invalid_path"
        assert absent.value.code == "invalid_argument"


class TestSafetyAndBudget:
    @pytest.mark.asyncio
    async def test_multi_repo_requires_alias(self, repo: Path, tmp_path: Path, git):
        second = tmp_path / "second"
        second.mkdir()
        git(second, "init", "-q", "-b", "main")
        app = GitMcpApp(ServerConfig(roots=(str(repo), str(second))))

        with pytest.raises(GitMcpError) as excinfo:
            await build(app, "review_staged_changes")

        assert excinfo.value.code == "invalid_argument"

    @pytest.mark.asyncio
    async def test_prompt_does_not_modify_repository(self, write_repo: Path, git):
        (write_repo / "app.py").write_text("dirty\n", encoding="utf-8")
        before_head = git(write_repo, "rev-parse", "HEAD").stdout
        before_status = git(write_repo, "status", "--porcelain").stdout
        app = make_app(write_repo)

        await build(app, "write_commit_message", staged_only="false")
        await build(app, "review_staged_changes")
        await build(app, "summarize_branch", base="HEAD")  # write_repo 只有 1 个提交

        assert git(write_repo, "rev-parse", "HEAD").stdout == before_head
        assert git(write_repo, "status", "--porcelain").stdout == before_status

    @pytest.mark.asyncio
    async def test_large_diff_is_budgeted(self, write_repo: Path):
        (write_repo / "app.py").write_text("line\n" * 60_000, encoding="utf-8")

        text = await build(make_app(write_repo), "write_commit_message", staged_only="false")

        assert len(text.encode("utf-8")) < 25_000
        assert "已按预算截断" in text
