"""GM2 搜索类只读工具端到端单测(git_grep / git_search_history)."""

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


class TestGrepTool:
    @pytest.mark.asyncio
    async def test_basic_matches(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_grep", pattern="hello")

        data = payload["data"]
        paths = {item["path"] for item in data["matches"]}
        assert {"app.py", "README.md"} <= paths
        assert data["count"] == len(data["matches"])
        assert data["truncated"] is False
        assert all(isinstance(item["line"], int) and item["content"] for item in data["matches"])

    @pytest.mark.asyncio
    async def test_paths_filter(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_grep", pattern="hello", paths=["app.py"])

        assert {item["path"] for item in payload["data"]["matches"]} == {"app.py"}

    @pytest.mark.asyncio
    async def test_ref_search_uses_revision_content(self, history_repo: Path):
        app = make_app(history_repo)

        old = await call(app, "git_grep", pattern="world", ref="HEAD~1")
        new = await call(app, "git_grep", pattern="world", ref="HEAD")

        assert old["data"]["count"] == 0
        assert new["data"]["count"] == 1
        assert new["data"]["ref"] == "HEAD"

    @pytest.mark.asyncio
    async def test_case_sensitivity(self, history_repo: Path):
        app = make_app(history_repo)

        sensitive = await call(app, "git_grep", pattern="HELLO")
        insensitive = await call(app, "git_grep", pattern="HELLO", case_sensitive=False)

        assert sensitive["data"]["count"] == 0
        assert insensitive["data"]["count"] >= 2

    @pytest.mark.asyncio
    async def test_fixed_string_vs_regex(self, history_repo: Path):
        app = make_app(history_repo)

        pattern = 'print("hello world")'
        regex = await call(app, "git_grep", pattern=pattern, regex=True)
        fixed = await call(app, "git_grep", pattern=pattern, regex=False)

        assert regex["data"]["count"] == 0  # 括号在正则里是分组, 不匹配字面量
        assert fixed["data"]["count"] == 1

    @pytest.mark.asyncio
    async def test_option_like_pattern_is_not_injected(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_grep", pattern="-i")

        assert payload["ok"] is True
        assert payload["data"]["count"] == 0

    @pytest.mark.asyncio
    async def test_max_lines_marks_truncated(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_grep", pattern="hello", max_lines=1)

        assert payload["data"]["count"] == 1
        assert payload["data"]["truncated"] is True
        assert payload["meta"]["truncated"] is True

    @pytest.mark.asyncio
    async def test_max_bytes_marks_truncated(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_grep", pattern="hello", max_bytes=60)

        assert payload["data"]["truncated"] is True
        assert payload["data"]["count"] >= 1
        assert len(json.dumps(payload["data"]["matches"], ensure_ascii=False)) <= 200

    @pytest.mark.asyncio
    async def test_no_matches_is_success(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_grep", pattern="definitely-not-present")

        assert payload["ok"] is True
        assert payload["data"] == {
            "pattern": "definitely-not-present",
            "ref": None,
            "matches": [],
            "count": 0,
            "max_lines": 100,
            "max_bytes": 32768,
            "truncated": False,
        }

    @pytest.mark.asyncio
    async def test_invalid_arguments(self, history_repo: Path):
        app = make_app(history_repo)

        missing = await call(app, "git_grep")
        empty = await call(app, "git_grep", pattern="")
        escape = await call(app, "git_grep", pattern="hello", paths=["../../etc"])
        option_ref = await call(app, "git_grep", pattern="hello", ref="--help")

        assert missing["error_code"] == "invalid_argument"
        assert empty["error_code"] == "invalid_argument"
        assert escape["error_code"] == "invalid_path"
        assert option_ref["error_code"] == "invalid_argument"


class TestSearchHistoryTool:
    @pytest.mark.asyncio
    async def test_pickaxe_s_finds_introducing_commit(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_search_history", pattern="hello world")

        assert payload["data"]["count"] == 1
        assert payload["data"]["commits"][0]["subject"] == "fix: update greeting"
        assert payload["data"]["pickaxe"] == "S"

    @pytest.mark.asyncio
    async def test_pickaxe_g_searches_diff(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_search_history", pattern="world", pickaxe="G")

        assert payload["data"]["count"] == 1
        assert payload["data"]["pickaxe"] == "G"

    @pytest.mark.asyncio
    async def test_has_more_with_max_count(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_search_history", pattern="hello", pickaxe="G", max_count=1)

        assert payload["data"]["count"] == 1
        assert payload["data"]["has_more"] is True
        assert payload["meta"]["has_more"] is True

    @pytest.mark.asyncio
    async def test_pattern_with_leading_dash(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_search_history", pattern="-not-present")

        assert payload["ok"] is True
        assert payload["data"]["count"] == 0

    @pytest.mark.asyncio
    async def test_invalid_arguments(self, history_repo: Path):
        app = make_app(history_repo)

        missing = await call(app, "git_search_history")
        empty = await call(app, "git_search_history", pattern="")
        bad_pickaxe = await call(app, "git_search_history", pattern="x", pickaxe="X")

        assert missing["error_code"] == "invalid_argument"
        assert empty["error_code"] == "invalid_argument"
        assert bad_pickaxe["error_code"] == "invalid_argument"
