"""GM4 MCP Resources 单测(§4.3): git://<repo>/status|head|log/<ref>|file/<ref>/<path>."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meowmeowclaw_mcp_git.app import GitMcpApp
from meowmeowclaw_mcp_git.config import ServerConfig
from meowmeowclaw_mcp_git.errors import GitMcpError


def make_app(repo: Path, **overrides) -> GitMcpApp:
    return GitMcpApp(ServerConfig(roots=(str(repo),), **overrides))


async def read(app: GitMcpApp, uri: str):
    return await app.read_resource(uri)


class TestListing:
    def test_static_resources_and_templates(self, history_repo: Path):
        resources, templates = make_app(history_repo).resource_definitions()

        assert [item["uri"] for item in resources] == [
            f"git://{history_repo.name}/status",
            f"git://{history_repo.name}/head",
        ]
        assert all(item["mimeType"] == "application/json" for item in resources)
        assert [item["uriTemplate"] for item in templates] == [
            "git://{repo}/log/{ref}",
            "git://{repo}/file/{ref}/{path}",
        ]

    def test_multi_repo_expands_static_resources(self, repo: Path, tmp_path: Path, git):
        second = tmp_path / "second"
        second.mkdir()
        git(second, "init", "-q", "-b", "main")
        app = GitMcpApp(ServerConfig(roots=(str(repo), str(second))))

        resources, _templates = app.resource_definitions()

        assert len(resources) == 4
        assert {item["uri"] for item in resources} == {
            f"git://{repo.name}/status",
            f"git://{repo.name}/head",
            f"git://{second.name}/status",
            f"git://{second.name}/head",
        }


class TestStatusAndHead:
    @pytest.mark.asyncio
    async def test_status_returns_json_summary(self, history_repo: Path):
        payload = await read(make_app(history_repo), f"git://{history_repo.name}/status")

        data = json.loads(payload.text)
        assert payload.mime_type == "application/json"
        assert data["branch"] == "main"
        assert set(data) >= {"staged", "unstaged", "untracked", "conflicts", "head_oid"}

    @pytest.mark.asyncio
    async def test_head_returns_branch_and_upstream(self, remote_setup):
        repo, _remote = remote_setup

        payload = await read(make_app(repo), f"git://{repo.name}/head")

        data = json.loads(payload.text)
        assert data["branch"] == "main"
        assert data["head_oid"]
        assert data["upstream"] == "origin/main"
        assert data["ahead"] == 0 and data["behind"] == 0


class TestLog:
    @pytest.mark.asyncio
    async def test_log_head(self, history_repo: Path):
        payload = await read(make_app(history_repo), f"git://{history_repo.name}/log/HEAD")

        data = json.loads(payload.text)
        assert payload.mime_type == "application/json"
        assert data["ref"] == "HEAD"
        assert data["count"] >= 1
        assert data["commits"][0]["subject"] == "fix: update greeting"

    @pytest.mark.asyncio
    async def test_log_with_slashed_ref(self, history_repo: Path):
        payload = await read(make_app(history_repo), f"git://{history_repo.name}/log/feature/login")

        data = json.loads(payload.text)
        assert data["ref"] == "feature/login"
        assert data["commits"][0]["subject"] == "feat: add login"

    @pytest.mark.asyncio
    async def test_log_with_range(self, history_repo: Path):
        payload = await read(make_app(history_repo), f"git://{history_repo.name}/log/HEAD~1..HEAD")

        data = json.loads(payload.text)
        assert data["ref"] == "HEAD~1..HEAD"
        assert data["count"] == 1
        assert data["commits"][0]["subject"] == "fix: update greeting"

    @pytest.mark.asyncio
    async def test_log_bad_ref(self, history_repo: Path):
        with pytest.raises(GitMcpError) as excinfo:
            await read(make_app(history_repo), f"git://{history_repo.name}/log/no-such-ref")

        assert excinfo.value.code == "invalid_argument"


class TestFile:
    @pytest.mark.asyncio
    async def test_file_at_head_is_raw_text(self, history_repo: Path):
        payload = await read(make_app(history_repo), f"git://{history_repo.name}/file/HEAD/app.py")

        assert payload.mime_type == "text/plain"
        assert "hello world" in payload.text

    @pytest.mark.asyncio
    async def test_file_at_previous_revision(self, history_repo: Path):
        payload = await read(make_app(history_repo), f"git://{history_repo.name}/file/HEAD~1/app.py")

        assert "world" not in payload.text

    @pytest.mark.asyncio
    async def test_file_with_slashed_ref(self, history_repo: Path):
        payload = await read(make_app(history_repo), f"git://{history_repo.name}/file/feature/login/login.py")

        assert "def login" in payload.text

    @pytest.mark.asyncio
    async def test_subdirectory_path(self, history_repo: Path):
        payload = await read(make_app(history_repo), f"git://{history_repo.name}/file/HEAD/data/中文.txt")

        assert "你好" in payload.text

    @pytest.mark.asyncio
    async def test_binary_file_returns_json_metadata(self, history_repo: Path):
        payload = await read(make_app(history_repo), f"git://{history_repo.name}/file/HEAD/bin.dat")

        data = json.loads(payload.text)
        assert payload.mime_type == "application/json"
        assert data["binary"] is True
        assert data["text"] is None
        assert data["size"] == 15

    @pytest.mark.asyncio
    async def test_truncated_file_returns_json_metadata(self, history_repo: Path, git):
        (history_repo / "big.txt").write_text("line\n" * 40_000, encoding="utf-8")
        git(history_repo, "add", "big.txt")
        git(history_repo, "commit", "-qm", "chore: big file")

        payload = await read(make_app(history_repo), f"git://{history_repo.name}/file/HEAD/big.txt")

        data = json.loads(payload.text)
        assert payload.mime_type == "application/json"
        assert data["truncated"] is True
        assert data["size"] > 65_536
        assert len(data["text"].encode("utf-8")) <= 65_536


class TestErrors:
    @pytest.mark.asyncio
    async def test_unknown_scheme(self, history_repo: Path):
        with pytest.raises(GitMcpError) as excinfo:
            await read(make_app(history_repo), f"http://{history_repo.name}/status")

        assert excinfo.value.code == "invalid_argument"

    @pytest.mark.asyncio
    async def test_unknown_kind_and_incomplete_uri(self, history_repo: Path):
        app = make_app(history_repo)

        with pytest.raises(GitMcpError) as kind:
            await read(app, f"git://{history_repo.name}/branches")
        with pytest.raises(GitMcpError) as incomplete:
            await read(app, f"git://{history_repo.name}/file/HEAD")

        assert kind.value.code == "invalid_argument"
        assert incomplete.value.code == "invalid_argument"

    @pytest.mark.asyncio
    async def test_unknown_alias(self, history_repo: Path):
        with pytest.raises(GitMcpError) as excinfo:
            await read(make_app(history_repo), "git://ghost/status")

        assert excinfo.value.code == "repo_not_found"

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, history_repo: Path):
        with pytest.raises(GitMcpError) as excinfo:
            await read(make_app(history_repo), f"git://{history_repo.name}/file/HEAD/../outside.txt")

        assert excinfo.value.code == "invalid_path"

    @pytest.mark.asyncio
    async def test_alias_match_is_case_insensitive(self, history_repo: Path):
        app = GitMcpApp(
            ServerConfig(roots=(str(history_repo.parent),), repos=(("MyRepo", str(history_repo)),))
        )

        payload = await read(app, "git://myrepo/status")

        assert json.loads(payload.text)["branch"] == "main"


class TestReadOnly:
    @pytest.mark.asyncio
    async def test_read_does_not_modify_repository(self, history_repo: Path, git):
        before_head = git(history_repo, "rev-parse", "HEAD").stdout
        before_status = git(history_repo, "status", "--porcelain").stdout
        app = make_app(history_repo)
        name = history_repo.name

        await read(app, f"git://{name}/status")
        await read(app, f"git://{name}/head")
        await read(app, f"git://{name}/log/HEAD")
        await read(app, f"git://{name}/file/HEAD/app.py")

        assert git(history_repo, "rev-parse", "HEAD").stdout == before_head
        assert git(history_repo, "status", "--porcelain").stdout == before_status
