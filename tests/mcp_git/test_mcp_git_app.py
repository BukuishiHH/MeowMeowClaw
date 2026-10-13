"""GM1 应用层单测(``meowmeowclaw_mcp_git/app.py``): 权限矩阵 + 统一信封 + 兜底."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from meowmeowclaw_mcp_git.app import GitMcpApp, select_tools
from meowmeowclaw_mcp_git.config import ServerConfig
from meowmeowclaw_mcp_git.tools.base import (
    LEVEL_DESTRUCTIVE,
    LEVEL_READ,
    LEVEL_REMOTE,
    LEVEL_WRITE,
    ToolOutcome,
    ToolSpec,
)

SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}


def fake_spec(name: str, level: str, handler=None) -> ToolSpec:
    async def default_handler(ctx, args):
        return ToolOutcome(data={"ok": name})

    return ToolSpec(
        name=name,
        description=f"fake {name}",
        parameters=SCHEMA,
        handler=handler or default_handler,
        level=level,
    )


class TestToolSelection:
    def test_read_default_write_off_remote_off_destructive_never(self):
        specs = [
            fake_spec("read_a", LEVEL_READ),
            fake_spec("write_a", LEVEL_WRITE),
            fake_spec("remote_a", LEVEL_REMOTE),
            fake_spec("destructive_a", LEVEL_DESTRUCTIVE),
        ]

        selected = select_tools(ServerConfig(), specs)

        assert [spec.name for spec in selected] == ["read_a"]

    def test_write_enabled_when_read_only_false(self):
        specs = [fake_spec("write_a", LEVEL_WRITE)]

        assert [spec.name for spec in select_tools(ServerConfig(read_only=False), specs)] == ["write_a"]

    def test_remote_enabled_when_allow_remote_true(self):
        specs = [fake_spec("remote_a", LEVEL_REMOTE)]

        assert select_tools(ServerConfig(allow_remote=True), specs)[0].name == "remote_a"

    def test_destructive_never_registered_even_when_allowed(self):
        specs = [fake_spec("destructive_a", LEVEL_DESTRUCTIVE)]

        assert select_tools(ServerConfig(read_only=False, allow_destructive=True), specs) == ()


class TestAppWiring:
    def test_default_app_registers_only_read_tools(self, repo: Path):
        app = GitMcpApp(ServerConfig(roots=(str(repo),)))

        assert app.tool_names() == ("git_status",)
        definitions = app.tool_definitions()
        assert definitions[0]["name"] == "git_status"
        assert definitions[0]["inputSchema"]["properties"]["repo"]["type"] == "string"

    def test_write_spec_not_registered_in_read_only_mode(self, repo: Path):
        app = GitMcpApp(
            ServerConfig(roots=(str(repo),)),
            specs=[fake_spec("git_status_like", LEVEL_READ), fake_spec("git_commit", LEVEL_WRITE)],
        )

        assert app.tool_names() == ("git_status_like",)

    def test_write_spec_registered_when_read_only_false(self, repo: Path):
        app = GitMcpApp(
            ServerConfig(roots=(str(repo),), read_only=False),
            specs=[fake_spec("git_commit", LEVEL_WRITE)],
        )

        assert app.tool_names() == ("git_commit",)


class TestEnvelope:
    @pytest.mark.asyncio
    async def test_success_envelope(self, repo: Path):
        async def handler(ctx, args):
            return ToolOutcome(data={"x": 1}, meta={"repo": "demo"})

        app = GitMcpApp(ServerConfig(roots=(str(repo),)), specs=[fake_spec("t", LEVEL_READ, handler)])

        payload = json.loads(await app.call_tool("t", {}))

        assert payload == {"ok": True, "data": {"x": 1}, "meta": {"repo": "demo"}}

    @pytest.mark.asyncio
    async def test_internal_error_is_enveloped_and_logged(self, repo: Path, caplog):
        async def handler(ctx, args):
            raise RuntimeError("boom")

        app = GitMcpApp(ServerConfig(roots=(str(repo),)), specs=[fake_spec("t", LEVEL_READ, handler)])

        with caplog.at_level(logging.ERROR, logger="meowmeowclaw_mcp_git"):
            payload = json.loads(await app.call_tool("t", {}))

        assert payload["ok"] is False
        assert payload["error_code"] == "internal_error"
        assert "boom" in payload["message"]
        assert any("内部错误" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_meta_always_present(self, repo: Path):
        app = GitMcpApp(ServerConfig(roots=(str(repo),)))

        payload = json.loads(await app.call_tool("git_status", {"repo": "ghost"}))

        assert payload["meta"] == {"repo": "ghost"}

    @pytest.mark.asyncio
    async def test_json_is_compact_and_unicode_friendly(self, repo: Path):
        app = GitMcpApp(ServerConfig(roots=(str(repo),)))

        text = await app.call_tool("git_status", {"repo": "ghost"})

        assert "\n" not in text
        assert '"error_code":"repo_not_found"' in text
        assert "未知仓库" in text  # 中文不转义, 便于模型阅读
