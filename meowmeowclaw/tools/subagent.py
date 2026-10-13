"""SpawnSubagentTool(SA2, 见 docs/SUBAGENT_DESIGN.md §3.1).

薄适配层: function calling schema → ``SubagentRequest`` → ``SubagentFactory.spawn()``
→ 结构化 JSON tool content。不负责深度/预算/提示词/工具集构造(depth 已由 factory 冻结)。
"""

import asyncio
import uuid
from typing import Any, Optional

from meowmeowclaw.agent.loop import current_agent_turn
from meowmeowclaw.tools.base import BaseTool

from meowmeowclaw.subagent.models import (
    ERROR_INTERNAL,
    STATUS_ERROR,
    SpawnContext,
    SubagentBudget,
    SubagentRequest,
    SubagentResult,
)


class SpawnSubagentTool(BaseTool):
    """由主模型/上级子代理决定是否创建子代理的工具."""

    def __init__(
        self,
        factory: Any,
        *,
        depth: int = 0,
        parent_id: str = "root",
        fixed_context: Optional[SpawnContext] = None,
    ) -> None:
        if depth < 0:
            raise ValueError("depth 不能为负数")
        self._factory = factory
        self._depth = int(depth)
        self._parent_id = str(parent_id)
        self._fixed_context = fixed_context
        # 仅在工厂外直接调用工具(没有 AgentTurnContext)时的兜底状态:
        # 生产路径的预算挂在每个 task 的 AgentTurnContext 上, 不共享实例状态。
        self._fallback_root_id: Optional[str] = None
        self._fallback_budget: Optional[SubagentBudget] = None

    @property
    def name(self) -> str:
        return "spawn_subagent"

    @property
    def description(self) -> str:
        return (
            "创建一个子代理独立完成一个具体子任务，并返回它的结构化结果摘要。"
            "适合：任务可拆分为自包含子任务、需要长链探索/阅读、且不希望污染当前上下文。"
            "不适合：一句话问答、单次工具即可完成、必须与用户交互、或无法在 task/context 中写清背景的任务。"
            "子代理看不到当前对话历史；请把目标、输入、期望产出与验收标准写进 task，"
            "把已确认的必要事实写进 context。返回值是 JSON 摘要（status/summary 等），"
            "不是子代理的原始消息；请基于 summary 继续推理。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "子代理要独立完成的具体任务。必须自包含：目标、输入、"
                        "期望产出、验收标准。父代理不应假设子代理能看到当前对话。"
                    ),
                },
                "role": {
                    "type": "string",
                    "description": "可选角色短标签，例如 researcher / code-reader / test-writer。",
                },
                "context": {
                    "type": "string",
                    "description": "可选背景资料：父代理已确认、子代理无法自行获取的事实。",
                },
            },
            "required": ["task"],
            "additionalProperties": False,
        }

    # ------------------------------------------------------------------ 上下文

    def _resolve_context(self) -> SpawnContext:
        if self._fixed_context is not None:
            return self._fixed_context

        turn = current_agent_turn()
        if turn is not None:
            # 预算属于当前 task 的 AgentTurnContext, 并发会话/多 turn 均不会串号
            if turn.budget is None:
                turn.budget = self._factory.new_root_budget()
            return SpawnContext(
                root_id=turn.turn_id,
                parent_id=self._parent_id,
                depth=self._depth,
                budget=turn.budget,
                tool_names=tuple(getattr(self._factory, "tool_names", ())),
            )

        # 直接调用工具(不在 AgentLoop.run_turn 内)的兜底: 单实例一个临时预算
        if self._fallback_budget is None:
            self._fallback_root_id = f"adhoc-{uuid.uuid4().hex[:12]}"
            self._fallback_budget = self._factory.new_root_budget()
        return SpawnContext(
            root_id=self._fallback_root_id or f"adhoc-{uuid.uuid4().hex[:12]}",
            parent_id=self._parent_id,
            depth=self._depth,
            budget=self._fallback_budget,
            tool_names=tuple(getattr(self._factory, "tool_names", ())),
        )

    # ------------------------------------------------------------------ 执行

    async def execute(self, **kwargs: Any) -> str:
        try:
            request = SubagentRequest(
                task=kwargs.get("task"),
                role=kwargs.get("role"),
                context=kwargs.get("context"),
            )
        except ValueError as exc:
            result = self._error_result(f"参数不合法: {exc}")
            return result.to_json(max_chars=int(getattr(self._factory, "result_max_chars", 8000)))

        try:
            ctx = self._resolve_context()
            result = await self._factory.spawn(request, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 工具契约: 除取消外绝不抛给 AgentLoop
            result = self._error_result(f"子代理异常: {type(exc).__name__}")

        return result.to_json(max_chars=int(getattr(self._factory, "result_max_chars", 8000)))

    @staticmethod
    def _error_result(summary: str) -> SubagentResult:
        return SubagentResult(
            status=STATUS_ERROR,
            summary=summary,
            finish_reason=ERROR_INTERNAL,
            iterations=0,
            elapsed_ms=0,
            subagent_id=f"sa-invalid-{uuid.uuid4().hex[:8]}",
            depth=0,
            error_code=ERROR_INTERNAL,
        )


__all__ = ["SpawnSubagentTool"]
