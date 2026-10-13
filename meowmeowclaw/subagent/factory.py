"""SubagentFactory(SA2, 见 docs/SUBAGENT_DESIGN.md §3.2/§5/§11.3).

职责:
- 深度校验(第一层缺失工具, 这里第二层执行校验);
- 每 root turn 预算预检(children / total);
- 构造子代理 AgentLoop(独立 prompt / history / registry / model);
- 可重入顺序调度(多个 root 并发时串行, 嵌套 spawn 不自我死锁);
- 超时、结果折叠、观测计数。

SA2 临时约束: 子代理工具强制只读; write_file/exec 在 SA3/SA4 完成竞态与安全护栏后放开。
"""

import asyncio
import logging
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Optional, Union

from meowmeowclaw.agent.loop import AgentLoop
from meowmeowclaw.llm.base import LLMProvider
from meowmeowclaw.tools.mutation import ReadVersionTracker
from meowmeowclaw.tools.registry import ToolRegistry

from .models import (
    ERROR_CHILDREN_EXCEEDED,
    ERROR_DEPTH_EXCEEDED,
    ERROR_ENABLED,
    ERROR_INTERNAL,
    ERROR_TIMEOUT,
    STATUS_ERROR,
    STATUS_LIMIT,
    STATUS_TIMEOUT,
    SpawnContext,
    SubagentBudget,
    SubagentRequest,
    SubagentResult,
    SubagentStats,
)
from .prompt import SubagentPromptBuilder

logger = logging.getLogger(__name__)

# SA2 只读工具白名单; write_file/exec 等写/执行工具在 SA3/SA4 后放开
READ_ONLY_TOOL_NAMES: tuple[str, ...] = (
    "read_file",
    "list_dir",
    "web_search",
    "web_fetch",
    "load_skill",
)
DEFAULT_CHILD_TOOL_NAMES = READ_ONLY_TOOL_NAMES

# 调度器可重入标记: root spawn 持锁, 嵌套 spawn 复用同一 root 锁
_SCHEDULER_HELD: ContextVar[bool] = ContextVar(
    "meowmeowclaw_subagent_scheduler_held", default=False
)


class SubagentFactory:
    """按需创建并同步执行子代理的工厂(每个进程一个实例)."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        base_registry: ToolRegistry,
        workspace: Union[str, Path],
        enabled: bool = True,
        max_depth: int = 3,
        model: Optional[str] = None,
        max_iterations: int = 8,
        timeout_seconds: float = 120.0,
        result_max_chars: int = 8000,
        max_children_per_turn: int = 3,
        max_total_per_turn: int = 6,
        tool_names: tuple[str, ...] = DEFAULT_CHILD_TOOL_NAMES,
        read_only: bool = True,
        allow_exec: bool = False,
        stats: Optional[SubagentStats] = None,
    ) -> None:
        if max_depth < 2:
            raise ValueError("max_depth 必须 >= 2")
        if max_iterations < 1:
            raise ValueError("max_iterations 必须 >= 1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须 > 0")
        if result_max_chars <= 0:
            raise ValueError("result_max_chars 必须 > 0")
        if max_children_per_turn < 1:
            raise ValueError("max_children_per_turn 必须 >= 1")
        if max_total_per_turn < 1:
            raise ValueError("max_total_per_turn 必须 >= 1")

        self.provider = provider
        self.base_registry = base_registry
        self.workspace = Path(workspace)
        self.enabled = bool(enabled)
        self.max_depth = int(max_depth)
        self.model = model
        self.max_iterations = int(max_iterations)
        self.timeout_seconds = float(timeout_seconds)
        self.result_max_chars = int(result_max_chars)
        self.max_children_per_turn = int(max_children_per_turn)
        self.max_total_per_turn = int(max_total_per_turn)
        self.read_only = bool(read_only)
        self.allow_exec = bool(allow_exec)
        self.stats = stats if stats is not None else SubagentStats(enabled=self.enabled)
        self._scheduler = asyncio.Lock()

        names = tuple(name for name in tool_names if name != "spawn_subagent")
        if self.read_only:
            allowed = set(READ_ONLY_TOOL_NAMES)
            dropped = [name for name in names if name not in allowed]
            names = tuple(name for name in names if name in allowed)
            if dropped:
                logger.warning(
                    "子代理当前为只读模式, 已忽略写/执行工具: %s",
                    ", ".join(dropped),
                )
        elif not self.allow_exec and "exec" in names:
            # SA3: write_file 已开放; exec 仍待 SA4 安全策略
            names = tuple(name for name in names if name != "exec")
            logger.warning("SA3 暂未开放子代理 exec, 已忽略 (SA4 完成安全策略后放开)")
        # 未注册的工具在 base_registry 中不存在, 启动时一次性过滤(避免每次 spawn 重复 warning)
        available = set(base_registry.list_tools())
        missing = [name for name in names if name not in available]
        names = tuple(name for name in names if name in available)
        if missing:
            logger.warning("子代理工具白名单包含未注册工具, 已跳过: %s", ", ".join(missing))
        # 去重并保持顺序
        self.tool_names = tuple(dict.fromkeys(names))

        logger.info(
            "子代理已启用: max_depth=%d model=%s tools=%s timeout=%.1fs",
            self.max_depth,
            self.model or "(inherit)",
            list(self.tool_names),
            self.timeout_seconds,
        )

    def __repr__(self) -> str:
        return (
            f"<SubagentFactory enabled={self.enabled} max_depth={self.max_depth} "
            f"tools={list(self.tool_names)!r}>"
        )

    # ------------------------------------------------------------------ 预算/工具

    def new_root_budget(self) -> SubagentBudget:
        """为 root turn 创建预算(children/total 取自 factory 配置)."""
        return SubagentBudget(
            max_children_per_turn=self.max_children_per_turn,
            max_total_per_turn=self.max_total_per_turn,
        )

    def make_tool(
        self,
        *,
        depth: int = 0,
        parent_id: str = "root",
        root_id: Optional[str] = None,
        budget: Optional[SubagentBudget] = None,
    ):
        """创建 spawn 工具; 子代理用固定 ``SpawnContext``, root 用当前 turn 动态解析."""
        from meowmeowclaw.tools.subagent import SpawnSubagentTool

        fixed: Optional[SpawnContext] = None
        if root_id is not None and budget is not None:
            fixed = SpawnContext(
                root_id=root_id,
                parent_id=parent_id,
                depth=depth,
                budget=budget,
                tool_names=self.tool_names,
            )
        return SpawnSubagentTool(
            self,
            depth=depth,
            parent_id=parent_id,
            fixed_context=fixed,
        )

    def make_root_tool(self):
        return self.make_tool(depth=0, parent_id="root")

    # ------------------------------------------------------------------ 执行

    async def spawn(self, request: SubagentRequest, ctx: SpawnContext) -> SubagentResult:
        """执行一次 spawn; 除父任务取消外, 所有失败都折叠为 ``SubagentResult``."""
        started = time.monotonic()
        child_depth = ctx.depth + 1
        subagent_id = self._new_subagent_id()

        if not self.enabled:
            return self._error(
                subagent_id,
                child_depth,
                ERROR_ENABLED,
                "子代理功能未启用",
                started,
            )
        if child_depth >= self.max_depth:
            return self._limit(
                subagent_id,
                child_depth,
                ERROR_DEPTH_EXCEEDED,
                f"已达子代理深度上限 {self.max_depth - 1}(当前 depth={ctx.depth})",
                started,
            )

        budget_error = await ctx.budget.reserve_child(ctx.parent_id)
        if budget_error is not None:
            code = budget_error
            if budget_error == ERROR_CHILDREN_EXCEEDED:
                summary = "本轮直接子代理数量已达上限"
            else:
                summary = "本轮子代理总数已达上限"
            return self._limit(subagent_id, child_depth, code, summary, started)

        self.stats.record_spawn(child_depth)
        try:
            result = await self._run_scheduled(request, ctx, child_depth, subagent_id, started)
        except asyncio.CancelledError:
            self.stats.record_finish("cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 子代理失败不能炸父代理
            logger.exception("子代理执行异常: id=%s", subagent_id)
            result = self._error(
                subagent_id,
                child_depth,
                ERROR_INTERNAL,
                f"子代理执行异常: {type(exc).__name__}",
                started,
            )
        self.stats.record_finish(result.status)
        return result

    async def _run_scheduled(
        self,
        request: SubagentRequest,
        ctx: SpawnContext,
        child_depth: int,
        subagent_id: str,
        started: float,
    ) -> SubagentResult:
        """可重入顺序调度: root spawn 拿全局锁, 嵌套 spawn 复用当前 root 的锁."""
        if _SCHEDULER_HELD.get():
            return await self._run_child(request, ctx, child_depth, subagent_id, started)

        token = _SCHEDULER_HELD.set(True)
        try:
            async with self._scheduler:
                return await self._run_child(
                    request, ctx, child_depth, subagent_id, started
                )
        finally:
            _SCHEDULER_HELD.reset(token)

    def _build_child_registry(self, tracker: ReadVersionTracker) -> ToolRegistry:
        """构造子代理工具集: read/write 克隆为带私有 tracker 的副本, 其他工具复用."""
        registry = ToolRegistry()
        for name in self.tool_names:
            tool = self.base_registry.get(name)
            if tool is None:
                continue
            clone_for_subagent = getattr(tool, "for_subagent", None)
            if callable(clone_for_subagent):
                tool = clone_for_subagent(version_tracker=tracker)
            registry.register(tool)
        return registry

    async def _run_child(
        self,
        request: SubagentRequest,
        ctx: SpawnContext,
        child_depth: int,
        subagent_id: str,
        started: float,
    ) -> SubagentResult:
        tracker = ReadVersionTracker()
        child_registry = self._build_child_registry(tracker)
        if child_depth + 1 < self.max_depth:
            # 子代理自己还可以再创建一层: 给它配置与 root 共享的 budget/parent
            child_registry.register(
                self.make_tool(
                    depth=child_depth,
                    parent_id=subagent_id,
                    root_id=ctx.root_id,
                    budget=ctx.budget,
                )
            )

        builder = SubagentPromptBuilder(
            self.workspace,
            request=request,
            depth=child_depth,
            max_depth=self.max_depth,
            tool_names=tuple(child_registry.list_tools()),
        )
        child = AgentLoop(
            provider=self.provider,
            tools=child_registry,
            context=builder,
            model=self.model,
            max_iterations=self.max_iterations,
        )

        try:
            turn = await asyncio.wait_for(
                child.run_turn(request.task, history=[]),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning(
                "子代理超时: id=%s depth=%d timeout=%.1fs",
                subagent_id,
                child_depth,
                self.timeout_seconds,
            )
            return SubagentResult(
                status=STATUS_TIMEOUT,
                summary=f"子代理在 {self.timeout_seconds:g} 秒内未完成，已取消。",
                finish_reason="timeout",
                iterations=0,
                elapsed_ms=elapsed_ms,
                subagent_id=subagent_id,
                depth=child_depth,
                model=self._model_name,
                error_code=ERROR_TIMEOUT,
            )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        return SubagentResult.from_turn(
            turn,
            subagent_id=subagent_id,
            depth=child_depth,
            model=self._model_name,
            elapsed_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------ 结果辅助

    @property
    def _model_name(self) -> str:
        return self.model or str(getattr(self.provider, "model", "") or "")

    @staticmethod
    def _new_subagent_id() -> str:
        return f"sa-{uuid.uuid4().hex[:12]}"

    def _error(
        self,
        subagent_id: str,
        depth: int,
        error_code: str,
        summary: str,
        started: float,
    ) -> SubagentResult:
        return SubagentResult(
            status=STATUS_ERROR,
            summary=summary,
            finish_reason=error_code,
            iterations=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            subagent_id=subagent_id,
            depth=depth,
            model=self._model_name,
            error_code=error_code,
        )

    def _limit(
        self,
        subagent_id: str,
        depth: int,
        error_code: str,
        summary: str,
        started: float,
    ) -> SubagentResult:
        return SubagentResult(
            status=STATUS_LIMIT,
            summary=summary,
            finish_reason=error_code,
            iterations=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            subagent_id=subagent_id,
            depth=depth,
            model=self._model_name,
            error_code=error_code,
        )


__all__ = [
    "DEFAULT_CHILD_TOOL_NAMES",
    "READ_ONLY_TOOL_NAMES",
    "SubagentFactory",
]
