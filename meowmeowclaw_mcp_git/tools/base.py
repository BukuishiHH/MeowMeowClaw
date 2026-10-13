"""工具契约: 声明式 :class:`ToolSpec` + 派发上下文/返回.

工具处理器只做“参数校验 → 策略 → Runner/解析器 → 结构化数据”, 返回
:class:`ToolOutcome`; 失败抛 :class:`~meowmeowclaw_mcp_git.errors.GitMcpError`
子类, 由 app 层统一转成 ``{ok:false,error_code,message,meta}`` 信封(§4.2)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from ..config import ServerConfig
from ..locks import RepoLockManager
from ..policy import PolicyEngine
from ..registry import RepoRegistry
from ..runner import GitRunner

# 安全等级(§3.3): read 默认开放; write/remote 需显式开关; destructive v1 永不注册
LEVEL_READ = "read"
LEVEL_WRITE = "write"
LEVEL_REMOTE = "remote"
LEVEL_DESTRUCTIVE = "destructive"
TOOL_LEVELS = (LEVEL_READ, LEVEL_WRITE, LEVEL_REMOTE, LEVEL_DESTRUCTIVE)

ToolHandler = Callable[["ToolContext", dict[str, Any]], Awaitable["ToolOutcome"]]


@dataclass(frozen=True)
class ToolContext:
    """一次服务器会话共享的依赖(registry/runner/config/policy/locks 由 app 装配)."""

    config: ServerConfig
    registry: RepoRegistry
    runner: GitRunner
    logger: logging.Logger
    policy: PolicyEngine
    locks: RepoLockManager


@dataclass(frozen=True)
class ToolOutcome:
    """工具成功返回: ``data`` 进信封 data, ``meta`` 进信封 meta."""

    data: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    """一个 MCP 工具的声明式定义(注册 + 派发共用)."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    level: str = LEVEL_READ
    title: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("工具 name 不能为空")
        if self.level not in TOOL_LEVELS:
            raise ValueError(f"未知工具等级: {self.level!r}")
        if self.parameters.get("type") != "object":
            raise ValueError(f"工具 {self.name} parameters 必须是 object schema")

    def definition(self) -> dict[str, Any]:
        """MCP ``Tool`` 的协议无关描述(app → server 使用)."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.parameters,
        }
