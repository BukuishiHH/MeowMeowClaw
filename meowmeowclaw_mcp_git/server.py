"""MCP stdio 适配层与进程入口(§7.1)。

- 这里唯一 import 官方 ``mcp`` SDK 的地方(见 ``__init__`` 的分层说明);
- **stdout 只允许 MCP 协议消息**: 日志一律走 stderr;
- 首帧握手由 SDK 负责, 本层只声明能力(注册 tools)与转发工具调用;
- ``validate_input=False``: 参数校验在 :mod:`meowmeowclaw_mcp_git.validation`,
  失败同样返回 ``{ok:false,error_code}`` 信封而不是协议错误(§4.2)。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Mapping, Optional, Sequence

from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    ResourceTemplate,
    TextContent,
    Tool,
)
from pydantic import AnyUrl

from . import __version__
from .app import GitMcpApp
from .config import load_config
from .errors import ConfigError

SERVER_NAME = "meowmeowclaw-git"
SERVER_INSTRUCTIONS = (
    "MeowMeowClaw Git MCP Server: 通过 git CLI 提供结构化 Git 工具(默认 read_only=true, 只读工具开放)。"
    "仓库以 alias 暴露, 工具返回值统一为 {ok,data,meta} JSON; 失败为 {ok:false,error_code,message,meta}。"
    "另提供只读 Prompts(提交信息/评审/分支汇总/冲突解释)与 Resources"
    "(git://<repo>/status|head|log/<ref>|file/<ref>/<path>)。"
)
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logger = logging.getLogger("meowmeowclaw_mcp_git")


def configure_stderr_logging(level: str = "INFO") -> None:
    """所有日志写 stderr(stdio 的 stdout 属于协议流, §7.1)."""
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, str(level).upper(), logging.INFO),
        format=LOG_FORMAT,
        force=True,
    )


def build_server(app: GitMcpApp) -> Server:
    """把 app 的工具表适配成 MCP ``Server``(list_tools + call_tool)."""
    server: Server = Server(
        name=SERVER_NAME,
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
    )
    definitions = app.tool_definitions()

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name=item["name"],
                description=item["description"],
                inputSchema=item["inputSchema"],
            )
            for item in definitions
        ]

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        text = await app.call_tool(name, arguments)
        return [TextContent(type="text", text=text)]

    @server.list_prompts()
    async def _list_prompts() -> list[Prompt]:
        return [
            Prompt(
                name=item["name"],
                title=item.get("title"),
                description=item["description"],
                arguments=[
                    PromptArgument(
                        name=arg["name"],
                        description=arg.get("description"),
                        required=arg.get("required", False),
                    )
                    for arg in item["arguments"]
                ],
            )
            for item in app.prompt_definitions()
        ]

    @server.get_prompt()
    async def _get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
        text = await app.build_prompt(name, arguments or {})
        definition = next((item for item in app.prompt_definitions() if item["name"] == name), None)
        return GetPromptResult(
            description=definition["description"] if definition else None,
            messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))],
        )

    resources, templates = app.resource_definitions()

    @server.list_resources()
    async def _list_resources() -> list[Resource]:
        return [Resource(**item) for item in resources]

    @server.list_resource_templates()
    async def _list_resource_templates() -> list[ResourceTemplate]:
        return [ResourceTemplate(**item) for item in templates]

    @server.read_resource()
    async def _read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
        payload = await app.read_resource(str(uri))
        return [ReadResourceContents(content=payload.text, mime_type=payload.mime_type)]

    return server


async def serve_stdio(app: GitMcpApp) -> None:
    """在 stdio 上跑 MCP 会话, 直到客户端关闭管道/进程被终止."""
    server = build_server(app)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: Optional[Sequence[str]] = None, *, environ: Optional[Mapping[str, str]] = None) -> int:
    """进程入口: 解析配置 → 装配 → stdio 会话; 退出码 0 正常 / 2 配置错误."""
    configure_stderr_logging()
    try:
        config = load_config(argv, environ=environ)
    except ConfigError as exc:
        logger.error("配置错误: %s", exc)
        return 2
    configure_stderr_logging(config.log_level)
    if config.audit_log:
        logger.info("--audit-log 已记录, 审计写入将在后续里程碑接入(GM6): %s", config.audit_log)
    if config.lock_dir:
        logger.info("跨进程仓库写锁已启用: %s", config.lock_dir)
    logger.info("启动 %s %s: %s", SERVER_NAME, __version__, config.describe())
    try:
        app = GitMcpApp.from_config(config)
    except ConfigError as exc:
        logger.error("启动失败: %s", exc)
        return 2
    try:
        asyncio.run(serve_stdio(app))
    except KeyboardInterrupt:  # pragma: no cover - 交互式终止
        logger.info("收到中断信号, 退出")
    except Exception:
        logger.exception("服务器异常退出")
        return 1
    logger.info("已退出")
    return 0
