"""网关测试共用替身(非 test_ 前缀, pytest 不收集)."""

import asyncio
from types import SimpleNamespace
from typing import Any, Callable, Optional

from meowmeowclaw.gateway import PolicyDecision
from meowmeowclaw.memory import SessionKey


class StubConversation:
    """替代 ConversationService: 记录调用并返回可控结果(可延迟/抛错)."""

    def __init__(
        self,
        answer: str = "回答",
        *,
        delay: float = 0.0,
        error: Optional[BaseException] = None,
        completed: bool = True,
        persisted: bool = True,
    ) -> None:
        self.answer = answer
        self.delay = delay
        self.error = error
        self.completed = completed
        self.persisted = persisted
        self.calls: list[dict[str, Any]] = []

    async def handle_message(self, key: SessionKey, text: str, *, meta: Optional[dict] = None):
        self.calls.append({"key": key, "text": text, "meta": meta})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(answer=self.answer, completed=self.completed, persisted=self.persisted)


class FixedPolicy:
    """按回调裁决的测试策略."""

    def __init__(self, channel: str, decide: Callable[[Any], PolicyDecision]) -> None:
        self.channel = channel
        self._decide = decide

    async def resolve(self, envelope) -> PolicyDecision:
        return self._decide(envelope)


def session_key(conversation_id: str = "c1", channel: str = "fake") -> SessionKey:
    return SessionKey(channel=channel, scope="private", conversation_id=conversation_id)
