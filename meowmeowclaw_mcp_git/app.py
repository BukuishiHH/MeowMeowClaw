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
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from .audit import AuditSink, digest_arguments
from .config import ServerConfig
from .envelope import dumps, failure, ok
from .errors import ERROR_INTERNAL, ERROR_INVALID_ARGUMENT, ERROR_OUTPUT_TRUNCATED, GitMcpError
from .locks import RepoLockManager
from .policy import PolicyEngine
from .prompts import PromptSpec, build_prompt_specs
from .registry import RepoRegistry
from .resources import ResourcePayload, read_git_resource, resource_listing
from .runner import GitRunner
from .tools import build_available_tools
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
        locks: Optional[RepoLockManager] = None,
    ) -> None:
        self.config = config
        self.logger = logger_ or logger
        self.runner = runner or GitRunner.from_config(config)
        self.registry = RepoRegistry.build(config, runner=self.runner)
        self.policy = PolicyEngine(config)
        self.locks = locks or RepoLockManager(lock_dir=config.lock_dir)
        # --audit-log 配置后启用最小审计(§8); 初始化失败在启动期报 ConfigError
        self.audit: Optional[AuditSink] = AuditSink(config.audit_log) if config.audit_log else None
        available = tuple(build_available_tools()) if specs is None else tuple(specs)
        self._specs: dict[str, ToolSpec] = {spec.name: spec for spec in select_tools(config, available)}
        self._prompts: dict[str, PromptSpec] = {spec.name: spec for spec in build_prompt_specs()}
        self._context = ToolContext(
            config=config,
            registry=self.registry,
            runner=self.runner,
            logger=self.logger,
            policy=self.policy,
            locks=self.locks,
        )
        self.logger.info(
            "装配完成: repos=%s tools=%s read_only=%s lock_dir=%s audit_log=%s",
            [entry.alias for entry in self.registry.list_repos()],
            sorted(self._specs),
            config.read_only,
            config.lock_dir or "(进程内)",
            str(self.audit.path) if self.audit else "(未启用)",
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

    # ---- Prompts / Resources (§4.3/§4.4) --------------------------------

    def prompt_definitions(self) -> list[dict[str, Any]]:
        """``list_prompts`` 用的协议无关描述列表."""
        return [spec.definition() for spec in self._prompts.values()]

    def prompt_names(self) -> tuple[str, ...]:
        return tuple(self._prompts)

    async def build_prompt(self, name: str, arguments: Any = None) -> str:
        """构建 prompt 文本(只读查询, 不执行写操作)."""
        spec = self._prompts.get(name)
        if spec is None:
            allowed = ", ".join(sorted(self._prompts)) or "无"
            raise ValueError(f"未知 prompt: {name!r}; 可用: {allowed}")
        return await spec.builder(self._context, dict(arguments or {}))

    def resource_definitions(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """``(静态资源, 资源模板)``; 读取不修改仓库状态."""
        return resource_listing(self._context)

    async def read_resource(self, uri: str) -> ResourcePayload:
        return await read_git_resource(self._context, uri)

    # ---- 派发 ------------------------------------------------------------

    async def call_tool(self, name: str, arguments: Any = None) -> str:
        """执行工具并返回 JSON 信封文本; 任何失败都不抛异常(§4.2)."""
        started = time.monotonic()
        payload = await self._dispatch(name, arguments)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._record_audit(name, arguments, payload, elapsed_ms)
        return dumps(payload)

    async def _dispatch(self, name: str, arguments: Any = None) -> dict[str, Any]:
        """执行工具并返回信封 payload(dict); 审计/序列化在 call_tool 里统一处理."""
        spec = self._specs.get(name)
        if spec is None:
            allowed = ", ".join(sorted(self._specs)) or "无"
            return failure(ERROR_INVALID_ARGUMENT, f"未知工具: {name!r}; 可用工具: {allowed}")
        try:
            params = validate_arguments(spec.parameters, arguments)
        except GitMcpError as exc:
            return failure(exc.code, str(exc), exc.meta)
        try:
            outcome: ToolOutcome = await spec.handler(self._context, params)
        except GitMcpError as exc:
            self.logger.warning("工具 %s 失败(%s): %s", name, exc.code, exc)
            return failure(exc.code, str(exc), self._error_meta(exc, params))
        except Exception as exc:  # noqa: BLE001 - 兜底: 不把未处理异常泄漏给协议层
            self.logger.exception("工具 %s 内部错误", name)
            return failure(ERROR_INTERNAL, f"内部错误: {exc!r}")
        return self._success_payload(name, outcome)

    def _record_audit(self, name: str, arguments: Any, payload: dict[str, Any], elapsed_ms: int) -> None:
        """追加一条工具调用审计(§8): 只存参数摘要与结果状态, 不落盘参数值/输出全文."""
        if self.audit is None:
            return
        try:
            meta = payload.get("meta") or {}
            data = payload.get("data") or {}
            event = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "tool": name,
                "repo": self._audit_repo(arguments),
                "args_digest": digest_arguments(arguments),
                "status": "ok" if payload.get("ok") else "error",
                "error_code": None if payload.get("ok") else payload.get("error_code"),
                "exit_code": meta.get("exit_code"),
                "elapsed_ms": elapsed_ms,
                "truncated": bool(meta.get("truncated") or data.get("truncated")),
            }
            self.audit.record(event)
        except Exception:  # noqa: BLE001 - 审计绝不阻塞工具主流程
            self.logger.exception("审计记录失败(已忽略)")

    def _audit_repo(self, arguments: Any) -> Optional[str]:
        requested = arguments.get("repo") if isinstance(arguments, dict) else None
        if isinstance(requested, str) and requested:
            return requested
        try:
            return self.registry.resolve(requested if isinstance(requested, str) else None).alias
        except GitMcpError:
            return None

    def _error_meta(self, exc: GitMcpError, params: Any) -> dict[str, Any]:
        """失败信封的 meta: 至少带上 repo alias, 便于客户端定位是哪个仓库被拒绝."""
        meta = dict(exc.meta)
        if "repo" in meta or not isinstance(params, dict):
            return meta
        requested = params.get("repo")
        try:
            meta["repo"] = self.registry.resolve(requested).alias
        except GitMcpError:
            if isinstance(requested, str) and requested:
                meta["repo"] = requested
        return meta

    def _success_payload(self, name: str, outcome: ToolOutcome) -> dict[str, Any]:
        """组装成功信封, 并对最终输出做一次硬上限兜底(§3.1/G3).

        工具自身已按预算裁剪文本/列表; 这里是最后一道保险: 若信封仍超过
        ``--max-output-bytes``, 返回 ``output_truncated`` 错误而不是把巨型 JSON
        塞给模型(错误信封不参与该检查, 消息本身有界)。
        """
        payload = ok(outcome.data, outcome.meta)
        size = len(dumps(payload).encode("utf-8"))
        if size <= self.config.max_output_bytes:
            return payload
        self.logger.warning("工具 %s 输出 %d 字节, 超过上限 %d", name, size, self.config.max_output_bytes)
        return failure(
            ERROR_OUTPUT_TRUNCATED,
            f"工具输出 {size} 字节, 超过 --max-output-bytes={self.config.max_output_bytes}; "
            "请缩小查询范围(如 max_count/paths/max_bytes)后重试",
            {"size_bytes": size, "limit_bytes": self.config.max_output_bytes},
        )
