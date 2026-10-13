"""协议无关的应用核心(装配 + 工具派发 + 统一信封)。

``server.py`` 只做 MCP SDK 适配, 业务都在这里; 因此配置/工具/解析层可以在没有
``mcp`` 依赖的环境下单测(G7)。

工具选择遵循 §3.3/§6.2 权限矩阵:

- ``read`` 默认注册;
- ``write`` 仅在 ``read_only=false`` 时注册(GM3 才有实现);
- ``remote`` 仅在 ``allow_remote=true`` 时注册(GM4 才有实现);
- ``destructive`` v1 永不注册(即使 ``allow_destructive=true``, 见 §6.2)。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional, Sequence

from .config import ServerConfig
from .envelope import dumps, failure, ok
from .errors import ERROR_INTERNAL, ERROR_INVALID_ARGUMENT, ERROR_OUTPUT_TRUNCATED, GitMcpError
from .registry import RepoRegistry
from .runner import GitRunner
from .tools import build_read_tools
from .tools.base import (
    LEVEL_DESTRUCTIVE,
    LEVEL_READ,
    LEVEL_REMOTE,
    LEVEL_WRITE,
    ToolContext,
    ToolOutcome,
    ToolSpec,
)
from .validation import validate_arguments

logger = logging.getLogger("meowmeowclaw_mcp_git")


def select_tools(config: ServerConfig, specs: Iterable[ToolSpec]) -> tuple[ToolSpec, ...]:
    """按权限矩阵过滤工具(默认只读, §6.2)."""
    selected = []
    for spec in specs:
        if spec.level == LEVEL_READ:
            selected.append(spec)
        elif spec.level == LEVEL_WRITE and not config.read_only:
            selected.append(spec)
        elif spec.level == LEVEL_REMOTE and config.allow_remote:
            selected.append(spec)
        elif spec.level == LEVEL_DESTRUCTIVE:
            # v1 明确不实现: 即使 --allow-destructive=true 也不注册
            continue
    return tuple(selected)


class GitMcpApp:
    """一次会话的应用实例: 持有 registry/runner 与已注册工具表."""

    def __init__(
        self,
        config: ServerConfig,
        *,
        runner: Optional[GitRunner] = None,
        specs: Optional[Sequence[ToolSpec]] = None,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self.logger = logger_ or logger
        self.runner = runner or GitRunner.from_config(config)
        self.registry = RepoRegistry.build(config, runner=self.runner)
        available = tuple(build_read_tools()) if specs is None else tuple(specs)
        self._specs: dict[str, ToolSpec] = {spec.name: spec for spec in select_tools(config, available)}
        self._context = ToolContext(
            config=config,
            registry=self.registry,
            runner=self.runner,
            logger=self.logger,
        )
        self.logger.info(
            "装配完成: repos=%s tools=%s read_only=%s",
            [entry.alias for entry in self.registry.list_repos()],
            sorted(self._specs),
            config.read_only,
        )

    @classmethod
    def from_config(cls, config: ServerConfig, **kwargs: Any) -> "GitMcpApp":
        return cls(config, **kwargs)

    # ---- 只读视图 --------------------------------------------------------

    def tool_names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def tool_definitions(self) -> list[dict[str, Any]]:
        """``list_tools`` 用的协议无关描述列表."""
        return [spec.definition() for spec in self._specs.values()]

    # ---- 派发 ------------------------------------------------------------

    async def call_tool(self, name: str, arguments: Any = None) -> str:
        """执行工具并返回 JSON 信封文本; 任何失败都不抛异常(§4.2)."""
        spec = self._specs.get(name)
        if spec is None:
            allowed = ", ".join(sorted(self._specs)) or "无"
            return dumps(failure(ERROR_INVALID_ARGUMENT, f"未知工具: {name!r}; 可用工具: {allowed}"))
        try:
            params = validate_arguments(spec.parameters, arguments)
        except GitMcpError as exc:
            return dumps(failure(exc.code, str(exc), exc.meta))
        try:
            outcome: ToolOutcome = await spec.handler(self._context, params)
        except GitMcpError as exc:
            self.logger.warning("工具 %s 失败(%s): %s", name, exc.code, exc)
            return dumps(failure(exc.code, str(exc), exc.meta))
        except Exception as exc:  # noqa: BLE001 - 兜底: 不把未处理异常泄漏给协议层
            self.logger.exception("工具 %s 内部错误", name)
            return dumps(failure(ERROR_INTERNAL, f"内部错误: {exc!r}"))
        return self._dump_success(name, outcome)

    def _dump_success(self, name: str, outcome: ToolOutcome) -> str:
        """序列化成功信封, 并对最终输出做一次硬上限兜底(§3.1/G3).

        工具自身已按预算裁剪文本/列表; 这里是最后一道保险: 若信封仍超过
        ``--max-output-bytes``, 返回 ``output_truncated`` 错误而不是把巨型 JSON
        塞给模型(错误信封不参与该检查, 消息本身有界)。
        """
        text = dumps(ok(outcome.data, outcome.meta))
        size = len(text.encode("utf-8"))
        if size <= self.config.max_output_bytes:
            return text
        self.logger.warning("工具 %s 输出 %d 字节, 超过上限 %d", name, size, self.config.max_output_bytes)
        return dumps(
            failure(
                ERROR_OUTPUT_TRUNCATED,
                f"工具输出 {size} 字节, 超过 --max-output-bytes={self.config.max_output_bytes}; "
                "请缩小查询范围(如 max_count/paths/max_bytes)后重试",
                {"size_bytes": size, "limit_bytes": self.config.max_output_bytes},
            )
        )
