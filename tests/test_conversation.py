"""ConversationService 的单元测试.

覆盖 M2 的编排契约:
- 装载历史 → AgentLoop.run_turn → 仅完整轮次回写 store;
- 窗口参数生效、外部历史不写入 AgentLoop 实例;
- 同会话串行、每会话 agent 复用;
- store 读/写失败时 fail-soft;
- archive/purge/list 透传。
"""

import asyncio
from typing import Any, Optional

import pytest
from unittest.mock import MagicMock

from meowmeowclaw.agent.context import ContextBuilder
from meowmeowclaw.agent.loop import AgentLoop
from meowmeowclaw.conversation import ConversationService
from meowmeowclaw.llm.base import FINISH_REASON_ERROR, FINISH_REASON_STOP, LLMProvider, LLMResponse
from meowmeowclaw.memory import JsonlSessionStore, SessionKey, SessionStoreError
from meowmeowclaw.tools.registry import ToolRegistry


# --------------------------------------------------------------------- 测试替身


class ScriptedProvider(LLMProvider):
    """按脚本返回回答; 记录 messages 快照与并发峰值."""

    def __init__(
        self,
        answers: Optional[list[str]] = None,
        *,
        error: Optional[BaseException] = None,
        delay: float = 0.0,
    ) -> None:
        self._answers = list(answers or [])
        self.error = error
        self.delay = delay
        self.calls: list[list[dict[str, Any]]] = []
        self.active = 0
        self.max_active = 0

    async def chat(self, messages, tools=None, model=None) -> LLMResponse:
        self.calls.append([dict(message) for message in messages])
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                return LLMResponse(
                    content=f"[LLM调用失败] {self.error}", finish_reason=FINISH_REASON_ERROR
                )
            answer = self._answers.pop(0) if self._answers else "默认回答"
            return LLMResponse(content=answer, finish_reason=FINISH_REASON_STOP)
        finally:
            self.active -= 1


def make_agent(provider: LLMProvider) -> AgentLoop:
    registry = MagicMock(spec=ToolRegistry)
    registry.get_definitions.return_value = []
    registry.list_tools.return_value = []
    context = MagicMock(spec=ContextBuilder)
    context.build_messages.side_effect = lambda history=None, current_message="": (
        [{"role": "system", "content": "SYS"}]
        + (list(history) if history else [])
        + ([{"role": "user", "content": current_message}] if current_message else [])
    )
    return AgentLoop(provider=provider, tools=registry, context=context)


class AgentFactory:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider
        self.calls: list[SessionKey] = []

    def __call__(self, key: SessionKey) -> AgentLoop:
        self.calls.append(key)
        return make_agent(self.provider)


class RecordingStore:
    """最小 SessionStore 替身: 可注入读/写失败, 记录 append 调用."""

    def __init__(
        self,
        *,
        load_error: Optional[BaseException] = None,
        append_error: Optional[BaseException] = None,
    ) -> None:
        self.load_error = load_error
        self.append_error = append_error
        self.history = []
        self.appended: list[tuple[SessionKey, list, Optional[dict]]] = []

    async def load_recent(self, key, *, max_turns=None, max_chars=None):
        if self.load_error is not None:
            raise self.load_error
        return list(self.history)

    async def append_turn(self, key, messages, *, turn_id=None, meta=None):
        if self.append_error is not None:
            raise self.append_error
        self.appended.append((key, list(messages), meta))
        return None  # type: ignore[return-value]


def cli_key(conversation_id: str = "conv-1") -> SessionKey:
    return SessionKey(channel="cli", scope="session", conversation_id=conversation_id)


# ------------------------------------------------------------------ 正常编排


class TestConversationService:
    @pytest.mark.asyncio
    async def test_handle_message_persists_complete_turn(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        provider = ScriptedProvider(["答1", "答2"])
        factory = AgentFactory(provider)
        service = ConversationService(store, factory)
        key = cli_key()

        first = await service.handle_message(key, "第一问")

        assert first.answer == "答1"
        assert first.completed is True
        assert first.persisted is True
        meta = await store.get_meta(key)
        assert meta is not None
        assert meta.turn_count == 1
        assert meta.message_count == 2

        second = await service.handle_message(key, "第二问")

        assert second.answer == "答2"
        meta = await store.get_meta(key)
        assert meta is not None
        assert meta.turn_count == 2
        assert meta.message_count == 4

        # 第二次请求应带上第一轮历史
        second_call = provider.calls[1]
        contents = [
            message.get("content")
            for message in second_call
            if message["role"] != "system"
        ]
        assert contents == ["第一问", "答1", "第二问"]

        # 同会话复用 agent, 不同会话才会新建
        assert factory.calls == [key]

    @pytest.mark.asyncio
    async def test_history_window_is_applied(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        provider = ScriptedProvider(["答1", "答2", "答3"])
        service = ConversationService(store, AgentFactory(provider), max_turns=1)
        key = cli_key()

        await service.handle_message(key, "第一问")
        await service.handle_message(key, "第二问")
        await service.handle_message(key, "第三问")

        third_call = provider.calls[2]
        contents = [
            message.get("content")
            for message in third_call
            if message["role"] != "system"
        ]
        # 窗口只保留上一轮(第二问/答2) + 当前第三问
        assert contents == ["第二问", "答2", "第三问"]

    @pytest.mark.asyncio
    async def test_external_history_does_not_fill_agent_instance(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        provider = ScriptedProvider(["答1", "答2"])
        factory = AgentFactory(provider)
        service = ConversationService(store, factory)
        key = cli_key()

        await service.handle_message(key, "第一问")
        await service.handle_message(key, "第二问")

        # 直接取服务缓存的 agent 校验: run_turn(history=...) 不写实例历史
        cached_agent = service._agents[key.storage_id]  # noqa: SLF001 - 契约测试
        assert cached_agent._session_history == []      # noqa: SLF001

    @pytest.mark.asyncio
    async def test_error_turn_is_not_persisted(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        provider = ScriptedProvider(error=RuntimeError("boom"))
        service = ConversationService(store, AgentFactory(provider))
        key = cli_key()

        result = await service.handle_message(key, "hi")

        assert result.completed is False
        assert result.persisted is False
        assert await store.get_meta(key) is None
        assert await store.load_recent(key) == []


# ------------------------------------------------------------------ fail-soft

class TestFailSoft:
    @pytest.mark.asyncio
    async def test_append_failure_still_returns_answer(self):
        store = RecordingStore(append_error=SessionStoreError("disk full"))
        provider = ScriptedProvider(["答1"])
        service = ConversationService(store, AgentFactory(provider))

        result = await service.handle_message(cli_key(), "hi")

        assert result.answer == "答1"
        assert result.persisted is False
        assert result.completed is True

    @pytest.mark.asyncio
    async def test_load_failure_degrades_to_empty_history(self):
        store = RecordingStore(load_error=SessionStoreError("read fail"))
        provider = ScriptedProvider(["答1"])
        service = ConversationService(store, AgentFactory(provider))
        key = cli_key()

        result = await service.handle_message(key, "hi")

        assert result.answer == "答1"
        assert result.persisted is True
        assert len(store.appended) == 1
        assert [message.role for message in store.appended[0][1]] == ["user", "assistant"]
        # 降级为空历史: 请求里不应出现旧内容
        assert [message["role"] for message in provider.calls[0]] == ["system", "user"]

    @pytest.mark.asyncio
    async def test_meta_is_forwarded_to_store(self):
        store = RecordingStore()
        service = ConversationService(store, AgentFactory(ScriptedProvider(["答1"])))
        meta = {"channel": "qq", "sender_id": "10001"}

        await service.handle_message(cli_key(), "hi", meta=meta)

        assert store.appended[0][2] == meta


# ------------------------------------------------------------------ 并发与缓存

class TestConcurrencyAndCache:
    @pytest.mark.asyncio
    async def test_same_session_is_serialized(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        provider = ScriptedProvider(["答1", "答2"], delay=0.01)
        service = ConversationService(store, AgentFactory(provider))
        key = cli_key()

        await asyncio.gather(
            service.handle_message(key, "第一问"),
            service.handle_message(key, "第二问"),
        )

        assert provider.max_active == 1  # 同会话串行, 不存在并发模型调用
        meta = await store.get_meta(key)
        assert meta is not None
        assert meta.turn_count == 2  # 两轮都被串行写入

    @pytest.mark.asyncio
    async def test_agent_factory_is_cached_per_session(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        factory = AgentFactory(ScriptedProvider(["答"]))
        service = ConversationService(store, factory)
        first = cli_key("conv-1")
        second = cli_key("conv-2")

        await service.handle_message(first, "a")
        await service.handle_message(first, "b")
        await service.handle_message(second, "c")

        assert factory.calls == [first, second]

    @pytest.mark.asyncio
    async def test_close_clears_agent_cache(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        factory = AgentFactory(ScriptedProvider(["答1", "答2"]))
        service = ConversationService(store, factory)
        key = cli_key()

        await service.handle_message(key, "a")
        await service.close()
        await service.handle_message(key, "b")

        assert factory.calls == [key, key]


# ------------------------------------------------------------------ 透传与管理

class TestPassthrough:
    @pytest.mark.asyncio
    async def test_archive_purge_and_list(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        service = ConversationService(store, AgentFactory(ScriptedProvider(["答1"])))
        key = cli_key()

        await service.handle_message(key, "hi")
        await service.archive_session(key)

        summaries = await service.list_sessions()
        assert len(summaries) == 1
        assert summaries[0].archived is True

        await service.purge_session(key)

        assert await service.list_sessions() == []
        assert await service.get_meta(key) is None

    @pytest.mark.asyncio
    async def test_invalid_inputs_are_rejected(self, tmp_path):
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"), AgentFactory(ScriptedProvider())
        )

        with pytest.raises(ValueError):
            await service.handle_message(cli_key(), "   ")

        with pytest.raises(TypeError):
            await service.handle_message("not-a-key", "hi")

    def test_repr(self, tmp_path):
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"), AgentFactory(ScriptedProvider())
        )

        assert "ConversationService" in repr(service)
