"""GM5 Cline 接入资产单测: 配置模板/接入指南/启动包装与实现保持一致.

- 模板必须是合法 JSON、autoApprove 只含只读工具、占位符明确;
- 用模板(替换占位符)真实启动 stdio server, 校验工具列表与 `--read-only` 一致;
- 指南覆盖工具清单/关键参数/排障; 包装脚本可执行并能启动模块。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from meowmeowclaw_mcp_git.tools import build_read_tools, build_write_tools  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = PROJECT_ROOT / "docs"
GUIDE = TEMPLATE_DIR / "CLINE_MCP_GIT.md"
WRAPPER = PROJECT_ROOT / "scripts" / "run_git_mcp_server.sh"
READ_TOOL_NAMES = [spec.name for spec in build_read_tools()]
WRITE_TOOL_NAMES = [spec.name for spec in build_write_tools()]
REMOTE_TOOL_NAMES = ["git_fetch"]


def load_template(name: str) -> dict:
    return json.loads((TEMPLATE_DIR / f"cline_mcp_settings.{name}.json").read_text(encoding="utf-8"))


def server_env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}


def substitute(template: dict, repo: Path) -> tuple[str, list[str]]:
    config = template["mcpServers"]["git"]
    command = config["command"].replace("/path/to/python", sys.executable)
    args = [str(item).replace("/path/to/repo", str(repo)) for item in config["args"]]
    return command, args


class TestTemplateStructure:
    @pytest.mark.parametrize("name", ["readonly", "writable"])
    def test_valid_json_and_required_fields(self, name: str):
        config = load_template(name)["mcpServers"]["git"]

        assert set(config) >= {"command", "args", "disabled", "autoApprove"}
        assert config["disabled"] is False
        assert isinstance(config["args"], list) and config["args"]
        assert "/path/to/python" in config["command"] or config["command"].startswith("/path/to/")

    def test_readonly_template_flags(self):
        config = load_template("readonly")["mcpServers"]["git"]

        assert "--read-only=true" in config["args"]
        assert "--read-only=false" not in config["args"]
        assert "--allow-remote=true" not in config["args"]

    def test_writable_template_flags(self):
        config = load_template("writable")["mcpServers"]["git"]

        assert "--read-only=false" in config["args"]
        assert "--allow-remote=false" in config["args"]  # 远端默认关闭, 需要时手动改
        assert "main,master" in config["args"]

    @pytest.mark.parametrize("name", ["readonly", "writable"])
    def test_placeholders_are_obvious(self, name: str):
        config = load_template(name)["mcpServers"]["git"]

        assert config["command"] == "/path/to/python"
        assert "/path/to/repo" in config["args"]

    @pytest.mark.parametrize("name", ["readonly", "writable"])
    def test_auto_approve_only_read_tools(self, name: str):
        auto_approve = load_template(name)["mcpServers"]["git"]["autoApprove"]

        assert sorted(auto_approve) == sorted(READ_TOOL_NAMES)
        assert not set(auto_approve) & set(WRITE_TOOL_NAMES + REMOTE_TOOL_NAMES)

    def test_guide_covers_tools_flags_and_troubleshooting(self):
        text = GUIDE.read_text(encoding="utf-8")

        assert all(tool in text for tool in READ_TOOL_NAMES)
        assert all(tool in text for tool in ("git_commit", "git_stage", "git_stash"))
        for token in ("--read-only=false", "--allow-remote=true", "--protected-branches", "autoApprove", "排障"):
            assert token in text
        assert "cline_mcp_settings.readonly.json" in text
        assert "cline_mcp_settings.writable.json" in text

    def test_env_example_points_to_guide(self):
        text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

        assert "docs/CLINE_MCP_GIT.md" in text
        assert "MCP_GIT_READ_ONLY" in text and "MCP_GIT_ALLOW_REMOTE" in text


class TestTemplateSmoke:
    """用模板启动真实 stdio server: 模板保证与实现一致(可复制的验收)."""

    @pytest.mark.asyncio
    async def test_readonly_template_exposes_only_read_tools(self, repo: Path):
        command, args = substitute(load_template("readonly"), repo)

        async with stdio_client(StdioServerParameters(command=command, args=args, env=server_env())) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()

        names = [tool.name for tool in tools.tools]
        assert names == READ_TOOL_NAMES
        assert not set(names) & set(WRITE_TOOL_NAMES + REMOTE_TOOL_NAMES)

    @pytest.mark.asyncio
    async def test_writable_template_exposes_write_tools(self, repo: Path):
        command, args = substitute(load_template("writable"), repo)

        async with stdio_client(StdioServerParameters(command=command, args=args, env=server_env())) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()

        names = [tool.name for tool in tools.tools]
        assert names == [*READ_TOOL_NAMES, *WRITE_TOOL_NAMES]
        assert "git_fetch" not in names  # writable 模板保持 --allow-remote=false


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell 脚本")
class TestWrapperScript:
    def test_script_is_executable(self):
        assert WRAPPER.is_file()
        assert os.access(WRAPPER, os.X_OK)
        assert "-m meowmeowclaw_mcp_git" in WRAPPER.read_text(encoding="utf-8")

    def test_script_runs_module(self):
        completed = subprocess.run(
            [str(WRAPPER), "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "MCP_GIT_PYTHON": sys.executable, "PYTHONPATH": str(PROJECT_ROOT)},
        )

        assert completed.returncode == 0
        assert "meowmeowclaw-mcp-git" in completed.stdout
