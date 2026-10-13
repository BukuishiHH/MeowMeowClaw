"""GM2 引用类只读工具端到端单测(branches/tags/remotes/rev_parse/config_get)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meowmeowclaw_mcp_git.app import GitMcpApp
from meowmeowclaw_mcp_git.config import ServerConfig


def make_app(repo: Path, **overrides) -> GitMcpApp:
    return GitMcpApp(ServerConfig(roots=(str(repo),), **overrides))


async def call(app: GitMcpApp, name: str, **arguments) -> dict:
    return json.loads(await app.call_tool(name, arguments))


class TestListBranches:
    @pytest.mark.asyncio
    async def test_local_branches(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_list_branches")

        data = payload["data"]
        assert data["scope"] == "local"
        assert [item["name"] for item in data["branches"]] == ["feature/login", "main"]
        assert [item["current"] for item in data["branches"]] == [False, True]
        assert all(len(item["hash"]) == 40 for item in data["branches"])

    @pytest.mark.asyncio
    async def test_verbose_includes_tracking_and_subject(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_list_branches", verbose=True)

        main = next(item for item in payload["data"]["branches"] if item["name"] == "main")
        assert main["subject"] == "fix: update greeting"
        assert main["upstream"] is None
        assert set(main) >= {"upstream", "ahead", "behind", "upstream_gone", "symbolic", "subject"}

    @pytest.mark.asyncio
    async def test_compact_mode_omits_tracking(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_list_branches", verbose=False)

        assert "upstream" not in payload["data"]["branches"][0]

    @pytest.mark.asyncio
    async def test_remote_scope_and_ahead_count(self, remote_setup, git):
        repo, _remote = remote_setup
        (repo / "local.txt").write_text("local\n", encoding="utf-8")
        git(repo, "add", "local.txt")
        git(repo, "commit", "-qm", "local work")
        app = make_app(repo)

        remote = await call(app, "git_list_branches", remote=True, verbose=True)
        local = await call(app, "git_list_branches", verbose=True)

        assert remote["data"]["scope"] == "remote"
        assert [item["name"] for item in remote["data"]["branches"]] == ["origin/main"]
        main = local["data"]["branches"][0]
        assert main["upstream"] == "origin/main"
        assert main["ahead"] == 1 and main["behind"] == 0


class TestListTags:
    @pytest.mark.asyncio
    async def test_tags_with_types_and_subjects(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_list_tags")

        data = payload["data"]
        assert data["pattern"] is None
        assert [item["name"] for item in data["tags"]] == ["v0.1", "v0.2"]
        by_name = {item["name"]: item for item in data["tags"]}
        assert by_name["v0.1"]["object_type"] == "commit"
        assert by_name["v0.2"]["object_type"] == "tag"
        assert by_name["v0.2"]["subject"] == "release v0.2"
        assert all(item["short_hash"] and item["created_at"] for item in data["tags"])

    @pytest.mark.asyncio
    async def test_pattern_filter(self, history_repo: Path):
        app = make_app(history_repo)

        matched = await call(app, "git_list_tags", pattern="v0.*")
        none = await call(app, "git_list_tags", pattern="nope*")

        assert [item["name"] for item in matched["data"]["tags"]] == ["v0.1", "v0.2"]
        assert none["data"]["count"] == 0
        assert none["data"]["tags"] == []

    @pytest.mark.asyncio
    async def test_option_like_pattern_rejected(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_list_tags", pattern="-d")

        assert payload["error_code"] == "invalid_argument"


class TestListRemotes:
    @pytest.mark.asyncio
    async def test_no_remotes(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_list_remotes")

        assert payload["data"] == {"remotes": [], "count": 0, "urls_masked": True}

    @pytest.mark.asyncio
    async def test_urls_are_masked(self, history_repo: Path, git):
        git(history_repo, "remote", "add", "origin", "https://user:tok3n@example.com/repo.git")

        payload = await call(make_app(history_repo), "git_list_remotes")

        remote = payload["data"]["remotes"][0]
        assert remote["name"] == "origin"
        assert remote["fetch_urls"] == ["https://***@example.com/repo.git"]
        assert remote["push_urls"] == ["https://***@example.com/repo.git"]

    @pytest.mark.asyncio
    async def test_multiple_urls(self, history_repo: Path, git):
        git(history_repo, "remote", "add", "origin", "https://example.com/a.git")
        # git 语义: set-url --add 追加的是 push URL, fetch 仍是第一个
        git(history_repo, "remote", "set-url", "--add", "origin", "https://example.com/b.git")

        payload = await call(make_app(history_repo), "git_list_remotes")

        remote = payload["data"]["remotes"][0]
        assert remote["fetch_urls"] == ["https://example.com/a.git"]
        assert remote["push_urls"] == ["https://example.com/a.git", "https://example.com/b.git"]


class TestRevParse:
    @pytest.mark.asyncio
    async def test_head_full_and_short(self, history_repo: Path, git):
        app = make_app(history_repo)
        expected = git(history_repo, "rev-parse", "HEAD").stdout.strip()

        full = await call(app, "git_rev_parse", ref="HEAD")
        short = await call(app, "git_rev_parse", ref="HEAD", short=True)

        assert full["data"]["oid"] == expected
        assert len(full["data"]["oid"]) == 40
        assert len(short["data"]["oid"]) < 40
        assert expected.startswith(short["data"]["oid"])

    @pytest.mark.asyncio
    async def test_annotated_tag_resolves_to_tag_object(self, history_repo: Path, git):
        expected = git(history_repo, "rev-parse", "v0.2").stdout.strip()

        payload = await call(make_app(history_repo), "git_rev_parse", ref="v0.2")

        assert payload["data"]["oid"] == expected

    @pytest.mark.asyncio
    async def test_bad_and_option_like_refs(self, history_repo: Path):
        app = make_app(history_repo)

        bad = await call(app, "git_rev_parse", ref="does-not-exist")
        option = await call(app, "git_rev_parse", ref="--help")
        missing = await call(app, "git_rev_parse")

        assert bad["error_code"] == "command_failed"
        assert option["error_code"] == "invalid_argument"
        assert missing["error_code"] == "invalid_argument"


class TestConfigGet:
    @pytest.mark.asyncio
    async def test_value_and_missing_key(self, history_repo: Path):
        app = make_app(history_repo)

        found = await call(app, "git_config_get", key="user.name")
        missing = await call(app, "git_config_get", key="does.not.exist")

        assert found["data"] == {"key": "user.name", "found": True, "value": "Alice", "redacted": False, "masked": False}
        assert missing["data"]["found"] is False
        assert missing["data"]["value"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["http.extraHeader", "credential.helper", "my.token", "auth.password"])
    async def test_sensitive_keys_are_redacted(self, history_repo: Path, git, key: str):
        git(history_repo, "config", key, "super-secret-value")

        payload = await call(make_app(history_repo), "git_config_get", key=key)

        assert payload["data"] == {"key": key, "found": True, "value": None, "redacted": True, "masked": False}
        assert "super-secret-value" not in json.dumps(payload, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_url_values_are_masked(self, history_repo: Path, git):
        git(history_repo, "remote", "add", "origin", "https://user:token@example.com/repo.git")

        payload = await call(make_app(history_repo), "git_config_get", key="remote.origin.url")

        assert payload["data"]["value"] == "https://***@example.com/repo.git"
        assert payload["data"]["masked"] is True
        assert payload["data"]["redacted"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["-x", "bad key", "url.https://example.com/.insteadOf", ""])
    async def test_invalid_keys(self, history_repo: Path, key: str):
        payload = await call(make_app(history_repo), "git_config_get", key=key)

        assert payload["ok"] is False
        assert payload["error_code"] == "invalid_argument"

    @pytest.mark.asyncio
    async def test_meta_contains_repo(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_config_get", key="user.name")

        assert payload["meta"]["repo"] == history_repo.name
