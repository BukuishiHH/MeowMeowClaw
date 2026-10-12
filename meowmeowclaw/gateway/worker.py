"""AgentWorker: 把策略裁决翻译成 ``ConversationService`` 调用(见 docs/GATEWAY_DESIGN.md §7.3).

不改动 ConversationService/AgentLoop 的任何语义; 失败由 dispatcher 记录日志(fail-soft).
"""

import logging
from typing import Awaitable, Callable, Optional

from meowmeowclaw.conversation import ConversationService

from .envelope import Envelope, make_reply
from .policy import PolicyDecision

logger = logging.getLogger(__name__)

# 流式增量回调(与 AgentLoop.DeltaCallback 同构; 网关侧不反向依赖 Agent 层)
DeltaSink = Callable[[str], Awaitable[None]]


class AgentWorker:
    """会话执行器: 每个网关进程一个实例, 供 dispatcher 并发调用."""

    def __init__(self, conversation: ConversationService) -> None:
        self.conversation = conversation

    def __repr__(self) -> str:
        return f"<AgentWorker conversation={self.conversation!r}>"

    async def handle(
        self,
        request: Envelope,
        decision: PolicyDecision,
        *,
        on_delta: Optional[DeltaSink] = None,
    ) -> Envelope:
        """执行一轮对话并返回回程信封; 异常向上抛给 dispatcher 统一兜底.

        ``on_delta`` 非空时透传给 ``ConversationService``(该轮走 Provider 流式);
        None(默认)时行为与不流式完全一致。
        """
        if decision.session_key is None:  # PolicyDecision 已校验; 这里兜底类型收窄
            raise ValueError("action=agent 必须携带 session_key")
        result = await self.conversation.handle_message(
            decision.session_key,
            decision.text,
            meta=decision.meta or None,
            on_delta=on_delta,
        )
        return make_reply(
            request,
            result.answer,
            session_id=decision.session_key.session_id,
            metadata={
                "source": "agent",
                "completed": bool(getattr(result, "completed", True)),
                "persisted": bool(getattr(result, "persisted", False)),
            },
        )
