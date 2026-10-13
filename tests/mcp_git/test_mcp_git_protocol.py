"""GM1 MCP 协议层单测(``meowmeowclaw_mcp_git/server.py``, §9.3/§10.2 验收项).

- 真 SDK 客户端握手(list_tools/call_tool), 进程内(memory)与真实子进程(stdio)各一组;
- stdout 只允许 MCP 协议消息: 空 stdin 启动退出后 stdout 必须为 0 字节(§7.1)。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")  # 未装 mcp-git 可选依赖时跳过(G7)

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from meowmeowclaw_mcp_git import __version__  # noqa: E402
from meowmeowclaw_mcp_git.app import GitMcpApp  # noqa: E402
from meowmeowclaw_mcp_git.config import ServerConfig  # noqa: E402
from meowmeowclaw_mcp_git.server import SERVER_NAME, build_server  # noqa: E402
from meowmeowclaw_mcp_git.tools import build_read_tools  # noqa: E402
from meowmeowclaw_mcp_git.tools.base import LEVEL_READ, LEVEL_WRITE, ToolOutcome, ToolSpec  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def server_env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}


def run_server(args: list[str], *, input_bytes: bytes = b"", timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "meowmeowclaw_mcp_git", *args],
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
        cwd=str(PROJECT_ROOT),
        env=server_env(),
    )


def make_app(repo: Path, **overrides) -> GitMcpApp:
    return GitMcpApp(ServerConfig(roots=(str(repo),), **overrides))


class TestInProcessProtocol:
    @pytest.mark.asyncio
    async def test_initialize_and_list_tools(self, repo: Path):
        server = build_server(make_app(repo))

        async with create_connected_server_and_client_session(server) as session:
            info = await session.initialize()
            tools = await session.list_tools()

        assert info.serverInfo.name == SERVER_NAME
        assert info.serverInfo.version == __version__
        assert [tool.name for tool in tools.tools] == [spec.name for spec in build_read_tools()]
        schema = tools.tools[0].inputSchema
        assert schema["type"] == "object"
        assert set(schema["properties"]) == {"repo", "short"}
        assert schema["additionalProperties"] is False

    @pytest.mark.asyncio
    async def test_call_tool_returns_json_envelope(self, repo: Path):
        server = build_server(make_app(repo))

        async with create_connected_server_and_client_session(server) as session:
            await session.initialize()
            result = await session.call_tool("git_status", {"short": True})

        payload = json.loads(result.content[0].text)
        assert payload["ok"] is True
        assert payload["data"]["branch"] == "main"
        assert payload["data"]["counts"]["staged"] == 0

    @pytest.mark.asyncio
    async def test_invalid_arguments_return_envelope_not_protocol_error(self, repo: Path):
        server = build_server(make_app(repo))

        async with create_connected_server_and_client_session(server) as session:
            await session.initialize()
            result = await session.call_tool("git_status", {"repo": 42})

        payload = json.loads(result.content[0].text)
        assert payload["ok"] is False
        assert payload["error_code"] == "invalid_argument"

    @pytest.mark.asyncio
    async def test_write_tools_not_registered_in_read_only_mode(self, repo: Path):
        async def noop(ctx, args):
            return ToolOutcome()

        write_spec = ToolSpec(
            name="git_commit",
            description="fake write tool",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=noop,
            level=LEVEL_WRITE,
        )
        read_spec = ToolSpec(
            name="git_status",
            description="fake read tool",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=noop,
            level=LEVEL_READ,
        )
        app = GitMcpApp(ServerConfig(roots=(str(repo),)), specs=[read_spec, write_spec])
        server = build_server(app)

        async with create_connected_server_and_client_session(server) as session:
            await session.initialize()
            tools = await session.list_tools()

        assert [tool.name for tool in tools.tools] == ["git_status"]


class TestStdioProcess:
    @pytest.mark.asyncio
    async def test_real_stdio_client_handshake_and_call(self, repo: Path):
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "meowmeowclaw_mcp_git", "--root", str(repo)],
            env=server_env(),
            cwd=str(PROJECT_ROOT),
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool("git_status", {})

        assert [tool.name for tool in tools.tools] == [spec.name for spec in build_read_tools()]
        payload = json.loads(result.content[0].text)
        assert payload["ok"] is True
        assert payload["meta"]["repo"] == repo.name

    def test_stdout_is_protocol_only_after_clean_exit(self, repo: Path):
        completed = run_server(["--root", str(repo)])

        assert completed.returncode == 0
        assert completed.stdout == b""  # stdout 无日志污染(§7.1)
        stderr = completed.stderr.decode("utf-8", errors="replace")
        assert "启动" in stderr and "已退出" in stderr

    def test_bad_root_exits_with_code_2_and_stderr_message(self, tmp_path: Path):
        plain = tmp_path / "plain"
        plain.mkdir()

        completed = run_server(["--root", str(plain)])

        assert completed.returncode == 2
        assert completed.stdout == b""
        assert "启动失败" in completed.stderr.decode("utf-8", errors="replace")

    def test_invalid_flag_exits_with_code_2(self, repo: Path):
        completed = run_server(["--root", str(repo), "--timeout", "abc"])

        assert completed.returncode == 2
        assert completed.stdout == b""
        assert "配置错误" in completed.stderr.decode("utf-8", errors="replace")
