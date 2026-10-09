"""QQ 渠道测试共用替身(非 test_ 前缀)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

from meowmeowclaw.agent.context import ContextBuilder
from meowmeowclaw.agent.loop import AgentLoop
from meowmeowclaw.channels.base import IncomingMessage
from meowmeowclaw.channels.qq_policy import QqPolicy
from meowmeowclaw.channels.qq_private import QqPrivateActiveStore
from meowmeowclaw.conversation import ConversationService
from meowmeowclaw.gateway import PolicyDecision
from meowmeowclaw.llm.base import FINISH_REASON_STOP, LLMProvider, LLMResponse
from meowmeowclaw.memory import JsonlSessionStore, SessionKey
from meowmeowclaw.tools.registry import ToolRegistry

OWNER = "10001"


class ScriptedProvider(LLMProvider):
    """按脚本返回回答; 记录每次收到的 messages."""

    def __init__(self, answers: Optional[list[str]] = None) -> None:
        self._answers = list(answers or [])
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None, model=None, max_tokens=None) -> LLMResponse:
        self.calls.append([dict(message) for message in messages])
        content = self._answers.pop(0) if self._answers else "默认回答"
        return LLMResponse(content=content, finish_reason=FINISH_REASON_STOP)


class Clock:
    """可推进的注入时钟(毫秒)."""

    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, delta_ms: int) -> None:
        self.now += delta_ms


@dataclass
class PolicyStack:
    conversation: ConversationService
    active_store: QqPrivateActiveStore
    policy: QqPolicy


def build_conversation(memory_dir: Path, provider: LLMProvider) -> ConversationService:
    registry = MagicMock(spec=ToolRegistry)
    registry.get_definitions.return_value = []
    registry.list_tools.return_value = []
    context = MagicMock(spec=ContextBuilder)
    context.build_messages.side_effect = lambda history=None, current_message="": (
        [{"role": "system", "content": "SYS"}]
        + (list(history) if history else [])
        + ([{"role": "user", "content": current_message}] if current_message else [])
    )

    def agent_factory(session_key: SessionKey) -> AgentLoop:
        return AgentLoop(provider=provider, tools=registry, context=context)

    return ConversationService(JsonlSessionStore(memory_dir), agent_factory)


def build_policy_stack(
    tmp_path: Path,
    provider: LLMProvider,
    clock: Clock,
    *,
    memory_dir: Optional[Path] = None,
    owner: str = OWNER,
    idle_timeout_ms: int = 6 * 60 * 60 * 1000,
) -> PolicyStack:
    memory_dir = memory_dir or tmp_path / "memory"
    conversation = build_conversation(memory_dir, provider)
    active_store = QqPrivateActiveStore(memory_dir)
    policy = QqPolicy(
        conversation,
        active_store,
        owner,
        idle_timeout_ms=idle_timeout_ms,
        clock=clock,
    )
    return PolicyStack(conversation=conversation, active_store=active_store, policy=policy)


async def run_agent(conversation: ConversationService, decision: PolicyDecision) -> str:
    """模拟 AgentWorker: 执行 agent 裁决并返回回答."""
    if decision.session_key is None:  # pragma: no cover
        raise AssertionError("decision.session_key is None")
    result = await conversation.handle_message(
        decision.session_key, decision.text, meta=decision.meta
    )
    return result.answer


def qq_message(
    text: str,
    *,
    owner: str = OWNER,
    received_at_ms: Optional[int] = None,
    scope: str = "private",
    message_id: Optional[str] = None,
) -> IncomingMessage:
    return IncomingMessage(
        channel="qq",
        scope=scope,
        conversation_id=owner,
        sender_id=owner,
        text=text,
        message_id=message_id,
        received_at_ms=received_at_ms,
    )


def contact_key(owner: str = OWNER) -> SessionKey:
    return SessionKey(channel="qq", scope="private", conversation_id=owner)


def cli_key(conversation_id: str = "cli-1") -> SessionKey:
    return SessionKey(channel="cli", scope="session", conversation_id=conversation_id)
