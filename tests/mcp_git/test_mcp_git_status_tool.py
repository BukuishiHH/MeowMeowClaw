"""GM1 ``git_status`` 工具端到端单测(临时 git 仓库, §9.2/§10.2 验收项)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meowmeowclaw_mcp_git.app import GitMcpApp
from meowmeowclaw_mcp_git.config import ServerConfig


def make_app(repo: Path | None = None, **overrides) -> GitMcpApp:
    config = ServerConfig(roots=(str(repo),) if repo is not None else (), **overrides)
    return GitMcpApp(config)


async def call_status(app: GitMcpApp, **arguments) -> dict:
    return json.loads(await app.call_tool("git_status", arguments))


@pytest.mark.asyncio
async def test_clean_repo_returns_branch_and_empty_groups(repo: Path):
    payload = await call_status(make_app(repo))

    assert payload["ok"] is True
    data = payload["data"]
    assert data["branch"] == "main"
    assert data["ahead"] == 0 and data["behind"] == 0
    assert data["detached"] is False and data["unborn"] is False
    assert data["clean"] is True
    assert data["staged"] == [] and data["unstaged"] == [] and data["untracked"] == []
    assert payload["meta"]["repo"] == repo.name
    assert payload["meta"]["exit_code"] == 0


@pytest.mark.asyncio
async def test_dirty_repo_groups_match_git(repo: Path, git):
    (repo / "README.md").write_text("changed\n", encoding="utf-8")          # unstaged
    (repo / "staged.py").write_text("print(1)\n", encoding="utf-8")         # staged
    (repo / "untracked.md").write_text("note\n", encoding="utf-8")          # untracked
    git(repo, "add", "staged.py")

    payload = await call_status(make_app(repo))

    data = payload["data"]
    assert data["clean"] is False
    assert data["staged"] == ["staged.py"]
    assert data["unstaged"] == ["README.md"]
    assert data["untracked"] == ["untracked.md"]
    assert data["conflicts"] == []


@pytest.mark.asyncio
async def test_short_mode_returns_counts_only(repo: Path, git):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    (repo / "c.py").write_text("c\n", encoding="utf-8")
    git(repo, "add", "a.py", "b.py")

    payload = await call_status(make_app(repo), short=True)

    data = payload["data"]
    assert data["counts"] == {"staged": 2, "unstaged": 0, "untracked": 1, "conflicts": 0, "renames": 0}
    assert "staged" not in data and "unstaged" not in data
    assert data["clean"] is False


@pytest.mark.asyncio
async def test_unborn_repository(empty_repo: Path):
    (empty_repo / "first.txt").write_text("first\n", encoding="utf-8")
    payload = await call_status(make_app(empty_repo))

    data = payload["data"]
    assert data["unborn"] is True
    assert data["branch"] == "main"
    assert data["head_oid"] is None
    assert data["untracked"] == ["first.txt"]


@pytest.mark.asyncio
async def test_detached_head(repo: Path, git):
    git(repo, "checkout", "-q", "--detach", "HEAD")

    payload = await call_status(make_app(repo))

    assert payload["data"]["detached"] is True
    assert payload["data"]["branch"] is None
    assert payload["data"]["head_oid"]


@pytest.mark.asyncio
async def test_ahead_of_upstream(remote_setup, git):
    repo, _remote = remote_setup
    (repo / "local.txt").write_text("local\n", encoding="utf-8")
    git(repo, "add", "local.txt")
    git(repo, "commit", "-qm", "local work")

    payload = await call_status(make_app(repo))

    assert payload["data"]["upstream"] == "origin/main"
    assert payload["data"]["ahead"] == 1
    assert payload["data"]["behind"] == 0


@pytest.mark.asyncio
async def test_behind_upstream(remote_setup, git, tmp_path: Path):
    repo, remote = remote_setup
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(remote), str(clone))
    git(clone, "config", "user.name", "Other Tester")
    git(clone, "config", "user.email", "other@example.com")
    (clone / "remote.txt").write_text("remote\n", encoding="utf-8")
    git(clone, "add", "remote.txt")
    git(clone, "commit", "-qm", "remote work")
    git(clone, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin")

    payload = await call_status(make_app(repo))

    assert payload["data"]["behind"] == 1
    assert payload["data"]["ahead"] == 0


@pytest.mark.asyncio
async def test_explicit_repo_alias(repo: Path):
    app = GitMcpApp(
        ServerConfig(roots=(str(repo.parent),), repos=(("web", str(repo)),))
    )

    payload = await call_status(app, repo="web")

    assert payload["ok"] is True
    assert payload["meta"]["repo"] == "web"


@pytest.mark.asyncio
async def test_unknown_alias_returns_repo_not_found(repo: Path):
    payload = await call_status(make_app(repo), repo="ghost")

    assert payload["ok"] is False
    assert payload["error_code"] == "repo_not_found"
    assert payload["meta"]["repo"] == "ghost"


@pytest.mark.asyncio
async def test_multi_repo_requires_alias(repo: Path, tmp_path: Path, git):
    second = tmp_path / "second"
    second.mkdir()
    git(second, "init", "-q", "-b", "main")
    app = GitMcpApp(ServerConfig(roots=(str(repo), str(second))))

    payload = await call_status(app)

    assert payload["ok"] is False
    assert payload["error_code"] == "invalid_argument"
    assert repo.name in payload["message"] and second.name in payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"repo": 123},
        {"short": "yes"},
        {"short": 1},
        {"repo": ""},
    ],
)
async def test_invalid_arguments_are_enveloped(repo: Path, arguments):
    payload = json.loads(await make_app(repo).call_tool("git_status", arguments))

    assert payload["ok"] is False
    assert payload["error_code"] == "invalid_argument"


@pytest.mark.asyncio
async def test_null_optional_is_treated_as_absent(repo: Path):
    payload = await call_status(make_app(repo), repo=None, short=None)

    assert payload["ok"] is True
    assert payload["meta"]["repo"] == repo.name


@pytest.mark.asyncio
async def test_unknown_extra_argument_rejected(repo: Path):
    payload = await call_status(make_app(repo), repo=repo.name, nope=1)

    assert payload["ok"] is False
    assert payload["error_code"] == "invalid_argument"
    assert "nope" in payload["message"]


@pytest.mark.asyncio
async def test_unknown_tool_rejected(repo: Path):
    payload = json.loads(await make_app(repo).call_tool("git_commit", {}))

    assert payload["ok"] is False
    assert payload["error_code"] == "invalid_argument"
    assert "git_status" in payload["message"]


@pytest.mark.asyncio
async def test_meta_contains_command_and_repo(repo: Path):
    payload = await call_status(make_app(repo))

    meta = payload["meta"]
    assert meta["command"][:2] == ["git", "--no-pager"]
    assert meta["command"][3] == str(repo)
    assert meta["command"][4:] == ["status", "--porcelain=v2", "--branch", "-z"]
    assert isinstance(meta["elapsed_ms"], int)
    assert meta["cwd"] == str(repo)
    assert meta["truncated"] is False


@pytest.mark.asyncio
async def test_truncated_output_is_explicit_error(repo: Path, git):
    (repo / "dirty.txt").write_text("dirty\n" * 50, encoding="utf-8")

    payload = await call_status(make_app(repo, max_output_bytes=1))

    assert payload["ok"] is False
    assert payload["error_code"] == "output_truncated"
    assert payload["meta"]["truncated"] is True


@pytest.mark.asyncio
async def test_unicode_filenames_are_preserved(repo: Path):
    (repo / "文件 名.txt").write_text("内容\n", encoding="utf-8")

    text = await make_app(repo).call_tool("git_status", {})
    payload = json.loads(text)

    assert "文件 名.txt" in payload["data"]["untracked"]
    assert "文件 名.txt" in text  # ensure_ascii=False, 直接可读
