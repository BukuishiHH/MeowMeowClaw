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
from meowmeowclaw.llm.base import (
    FINISH_REASON_ERROR,
    FINISH_REASON_STOP,
    LLMProvider,
    LLMResponse,
    StreamFinal,
    StreamTextDelta,
)
from meowmeowclaw.memory import JsonlSessionStore, SessionKey, SessionStoreError
from meowmeowclaw.subagent import STATUS_OK, SubagentStats
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

        # 直接取服务缓存的 runtime 校验: run_turn(history=...) 不写实例历史
        cached_agent = service._runtimes[key.storage_id].agent  # noqa: SLF001 - 契约测试
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


# ------------------------------------------------------------ 流式回调透传(S1)


class StreamingScriptedProvider(LLMProvider):
    """只实现流式接口的替身: 证明 on_delta 确实穿透到 AgentLoop 的流式路径."""

    def __init__(self, parts: list[str]) -> None:
        self.parts = parts
        self.stream_calls = 0
        self.chat_calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=None) -> LLMResponse:
        self.chat_calls += 1
        raise AssertionError("传了 on_delta 时不应走非流式 chat()")

    async def chat_stream(self, messages, tools=None, model=None, max_tokens=None):
        self.stream_calls += 1
        for part in self.parts:
            yield StreamTextDelta(part)
        yield StreamFinal(
            LLMResponse(content="".join(self.parts), finish_reason=FINISH_REASON_STOP)
        )


class DualScriptedProvider(StreamingScriptedProvider):
    """两条路径都实现: 用于断言 on_delta=None 时绝不走流式."""

    async def chat(self, messages, tools=None, model=None, max_tokens=None) -> LLMResponse:
        self.chat_calls += 1
        return LLMResponse(content="".join(self.parts), finish_reason=FINISH_REASON_STOP)


class TestStreamingPassthrough:
    @pytest.mark.asyncio
    async def test_on_delta_receives_streamed_answer_and_turn_persists(self, tmp_path):
        provider = StreamingScriptedProvider(["流式", "回答"])
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"), AgentFactory(provider)
        )
        chunks: list[str] = []

        async def on_delta(text: str) -> None:
            chunks.append(text)

        result = await service.handle_message(cli_key(), "问题", on_delta=on_delta)

        assert provider.stream_calls == 1
        assert result.answer == "流式回答"
        assert result.completed is True
        assert result.persisted is True
        # 宽限内结束的短回答: 结束时一次性补发, 且是最终文本的前缀
        assert chunks == ["流式回答"]
        assert result.answer.startswith("".join(chunks))

    @pytest.mark.asyncio
    async def test_on_delta_none_keeps_non_streaming_path(self, tmp_path):
        provider = DualScriptedProvider(["非流式回答"])
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"), AgentFactory(provider)
        )

        result = await service.handle_message(cli_key(), "问题")

        assert result.answer == "非流式回答"
        assert provider.chat_calls == 1
        assert provider.stream_calls == 0

    @pytest.mark.asyncio
    async def test_on_delta_failure_does_not_break_result(self, tmp_path):
        provider = StreamingScriptedProvider(["照常回答"])
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"), AgentFactory(provider)
        )

        async def broken_on_delta(text: str) -> None:
            raise RuntimeError("渲染层故障")

        result = await service.handle_message(cli_key(), "问题", on_delta=broken_on_delta)

        assert result.answer == "照常回答"
        assert result.completed is True
        assert result.persisted is True


# ------------------------------------------------------------ A+ A1 观测


class BrokenStatsStore(RecordingStore):
    """store.stats() 抛异常: 观测失败必须降级, 不能影响主流程."""

    def stats(self):
        raise RuntimeError("stats boom")


class TestConversationStats:
    @pytest.mark.asyncio
    async def test_stats_exposes_runtime_and_store(self, tmp_path):
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"), AgentFactory(ScriptedProvider(["答"]))
        )

        await service.handle_message(cli_key(), "问题")
        stats = service.stats()

        assert stats["cached_agents"] == 1
        assert stats["locks"] == 1
        assert stats["evictions"] == 0
        assert stats["store"]["metrics"]["load_recent"]["count"] == 1
        assert stats["store"]["metrics"]["append_turn"]["count"] == 1
        assert stats["store"]["active_file_count"] == 1
        assert stats["subagents"]["enabled"] is False
        # RSS 只在支持的平台给值(Windows 为 None)
        assert stats["rss_mb"] is None or stats["rss_mb"] > 0

    @pytest.mark.asyncio
    async def test_stats_counts_reset_after_close(self, tmp_path):
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"), AgentFactory(ScriptedProvider(["答"]))
        )
        await service.handle_message(cli_key(), "问题")

        await service.close()

        assert service.stats()["cached_agents"] == 0
        assert service.stats()["locks"] == 0

    def test_stats_exposes_subagent_snapshot(self):
        subagent_stats = SubagentStats(enabled=True)
        subagent_stats.record_spawn(depth=1)
        subagent_stats.record_finish(STATUS_OK, lock_wait_ms=3.5)
        service = ConversationService(
            RecordingStore(),
            AgentFactory(ScriptedProvider()),
            subagent_stats=subagent_stats,
        )

        snapshot = service.stats()["subagents"]

        assert snapshot["enabled"] is True
        assert snapshot["spawned_total"] == 1
        assert snapshot["ok"] == 1
        assert snapshot["active"] == 0
        assert snapshot["lock_wait_total_ms"] == 3.5

    def test_stats_degrades_when_store_has_no_stats(self):
        service = ConversationService(RecordingStore(), AgentFactory(ScriptedProvider()))

        stats = service.stats()

        assert stats["store"] == {}
        assert stats["cached_agents"] == 0
        assert stats["subagents"]["enabled"] is False

    def test_stats_degrades_when_store_stats_raises(self):
        service = ConversationService(BrokenStatsStore(), AgentFactory(ScriptedProvider()))

        stats = service.stats()

        assert stats["store"] == {}
        assert set(stats) == {
            "cached_agents",
            "locks",
            "evictions",
            "store",
            "subagents",
            "rss_mb",
        }


# --------------------------------------------------------- A+ A4 运行时生命周期


class GatedProvider(LLMProvider):
    """进入 chat 后阻塞, 用于制造"在途 turn"并观察 refcount/淘汰时序."""

    def __init__(self, answer: str = "答") -> None:
        self.answer = answer
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def chat(self, messages, tools=None, model=None) -> LLMResponse:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return LLMResponse(content=self.answer, finish_reason=FINISH_REASON_STOP)


async def wait_until(predicate, *, timeout: float = 2.0) -> None:
    """轮询等待条件成立(仅测试用, 避免依赖固定 sleep)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0)


class TestRuntimeLifecycle:
    @pytest.mark.asyncio
    async def test_runtime_holds_agent_lock_and_refcount_returns_to_zero(self, tmp_path):
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"), AgentFactory(ScriptedProvider(["答"]))
        )
        key = cli_key()

        await service.handle_message(key, "问题")

        runtime = service._runtimes[key.storage_id]  # noqa: SLF001 - 生命周期契约
        assert runtime.refcount == 0
        assert isinstance(runtime.lock, asyncio.Lock)
        assert runtime.last_used_ms > 0
        assert runtime.evict_pending is False
        assert service.stats()["cached_agents"] == 1
        assert service.stats()["locks"] == 1

    @pytest.mark.asyncio
    async def test_close_session_idle_releases_and_unknown_is_false(self, tmp_path):
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"), AgentFactory(ScriptedProvider(["答"]))
        )
        key = cli_key()
        await service.handle_message(key, "问题")

        assert await service.close_session(key) is True

        assert service.stats()["cached_agents"] == 0
        assert service.stats()["evictions"] == 1
        assert await service.close_session(key) is False
        assert service.stats()["evictions"] == 1

    @pytest.mark.asyncio
    async def test_close_session_busy_marks_pending_and_finishes_on_release(self, tmp_path):
        provider = GatedProvider()
        factory = AgentFactory(provider)
        service = ConversationService(JsonlSessionStore(tmp_path / "memory"), factory)
        key = cli_key()

        task = asyncio.create_task(service.handle_message(key, "问题"))
        await wait_until(lambda: provider.started.is_set())
        runtime = service._runtimes[key.storage_id]  # noqa: SLF001
        assert runtime.refcount == 1

        assert await service.close_session(key) is False  # busy: 标记 pending

        assert runtime.evict_pending is True
        assert service._runtimes[key.storage_id] is runtime  # noqa: SLF001 - 未替换锁
        assert service.stats()["cached_agents"] == 1

        provider.release.set()
        result = await task

        assert result.answer == "答"
        assert service.stats()["cached_agents"] == 0
        assert service.stats()["evictions"] == 1
        assert factory.calls == [key]  # 在途期间未重建 agent/锁

    @pytest.mark.asyncio
    async def test_refcount_counts_queued_waiter_and_keeps_lock_identity(self, tmp_path):
        provider = GatedProvider()
        factory = AgentFactory(provider)
        service = ConversationService(JsonlSessionStore(tmp_path / "memory"), factory)
        key = cli_key()

        first = asyncio.create_task(service.handle_message(key, "第一问"))
        await wait_until(lambda: provider.started.is_set())
        second = asyncio.create_task(service.handle_message(key, "第二问"))
        runtime = service._runtimes[key.storage_id]  # noqa: SLF001

        await wait_until(lambda: runtime.refcount == 2)  # 1 执行 + 1 排队
        assert provider.calls == 1  # 第二个请求确实在等锁

        provider.release.set()
        results = await asyncio.gather(first, second)

        assert [result.answer for result in results] == ["答", "答"]
        assert provider.calls == 2
        assert runtime.refcount == 0
        assert factory.calls == [key]

    @pytest.mark.asyncio
    async def test_evict_idle_with_zero_ttl_is_explicit_full_cleanup(self, tmp_path):
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"), AgentFactory(ScriptedProvider())
        )
        await service.handle_message(cli_key("c1"), "一")
        await service.handle_message(cli_key("c2"), "二")

        assert await service.evict_idle() == 2

        assert service.stats()["cached_agents"] == 0
        assert service.stats()["evictions"] == 2

    @pytest.mark.asyncio
    async def test_evict_idle_none_uses_constructor_ttl(self, tmp_path):
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"),
            AgentFactory(ScriptedProvider()),
            cache_idle_seconds=3600.0,
        )
        key = cli_key()
        await service.handle_message(key, "一")

        assert await service.evict_idle() == 0  # 新运行时未超过 TTL

        service._runtimes[key.storage_id].last_used_ms -= 3_601_000  # noqa: SLF001
        assert await service.evict_idle() == 1
        assert service.stats()["cached_agents"] == 0

    @pytest.mark.asyncio
    async def test_cache_idle_seconds_evicts_expired_idle_automatically(self, tmp_path):
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"),
            AgentFactory(ScriptedProvider()),
            cache_idle_seconds=3600.0,
        )
        first, second = cli_key("c1"), cli_key("c2")
        await service.handle_message(first, "一")
        service._runtimes[first.storage_id].last_used_ms -= 3_601_000  # noqa: SLF001

        await service.handle_message(second, "二")  # 新消息触发自动 TTL 清理

        assert first.storage_id not in service._runtimes  # noqa: SLF001
        assert second.storage_id in service._runtimes  # noqa: SLF001
        assert service.stats()["cached_agents"] == 1
        assert service.stats()["evictions"] == 1

    @pytest.mark.asyncio
    async def test_cache_idle_seconds_recreates_runtime_on_expired_reuse(self, tmp_path):
        factory = AgentFactory(ScriptedProvider())
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"), factory, cache_idle_seconds=3600.0
        )
        key = cli_key()
        await service.handle_message(key, "一")
        expired = service._runtimes[key.storage_id]  # noqa: SLF001
        expired.last_used_ms -= 3_601_000  # noqa: SLF001

        await service.handle_message(key, "二")

        assert service._runtimes[key.storage_id] is not expired  # noqa: SLF001
        assert factory.calls == [key, key]
        assert service.stats()["evictions"] == 1

    @pytest.mark.asyncio
    async def test_evict_idle_never_touches_inflight_runtime(self, tmp_path):
        provider = GatedProvider()
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"), AgentFactory(provider)
        )
        key = cli_key()

        task = asyncio.create_task(service.handle_message(key, "问题"))
        await wait_until(lambda: provider.started.is_set())

        assert await service.evict_idle(ttl_seconds=0) == 0
        assert service.stats()["cached_agents"] == 1

        provider.release.set()
        await task

        assert await service.evict_idle(ttl_seconds=0) == 1

    @pytest.mark.asyncio
    async def test_lru_capacity_evicts_oldest_idle_runtime(self, tmp_path):
        factory = AgentFactory(ScriptedProvider())
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"), factory, max_cached_sessions=2
        )
        first, second, third = cli_key("c1"), cli_key("c2"), cli_key("c3")

        await service.handle_message(first, "一")
        await service.handle_message(second, "二")
        await service.handle_message(third, "三")

        assert service.stats()["cached_agents"] == 2
        assert service.stats()["evictions"] == 1
        assert first.storage_id not in service._runtimes  # noqa: SLF001
        assert second.storage_id in service._runtimes  # noqa: SLF001

        await service.handle_message(first, "再访问最旧的会话")

        assert factory.calls == [first, second, third, first]
        assert service.stats()["cached_agents"] == 2
        assert service.stats()["evictions"] == 2
        assert second.storage_id not in service._runtimes  # noqa: SLF001 - LRU 淘汰

    @pytest.mark.asyncio
    async def test_max_cached_sessions_non_positive_means_unlimited(self, tmp_path):
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"),
            AgentFactory(ScriptedProvider()),
            max_cached_sessions=0,
        )
        for index in range(5):
            await service.handle_message(cli_key(f"c-{index}"), "问题")

        assert service.stats()["cached_agents"] == 5
        assert service.stats()["evictions"] == 0

    @pytest.mark.asyncio
    async def test_archive_and_purge_release_runtime(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        service = ConversationService(store, AgentFactory(ScriptedProvider()))
        key = cli_key()

        await service.handle_message(key, "问题")
        await service.archive_session(key)

        assert service.stats()["cached_agents"] == 0
        assert service.stats()["evictions"] == 1

        await service.handle_message(key, "归档后重新写入")
        await service.purge_session(key)

        assert service.stats()["cached_agents"] == 0
        assert service.stats()["evictions"] == 2

    @pytest.mark.asyncio
    async def test_archive_while_turn_inflight_marks_pending(self, tmp_path):
        provider = GatedProvider()
        factory = AgentFactory(provider)
        service = ConversationService(JsonlSessionStore(tmp_path / "memory"), factory)
        key = cli_key()

        task = asyncio.create_task(service.handle_message(key, "问题"))
        await wait_until(lambda: provider.started.is_set())
        runtime = service._runtimes[key.storage_id]  # noqa: SLF001

        await service.archive_session(key)  # 在途: 先标记 pending, 不换锁

        assert runtime.evict_pending is True
        assert service._runtimes[key.storage_id] is runtime  # noqa: SLF001

        provider.release.set()
        result = await task

        assert result.completed is True
        assert service.stats()["cached_agents"] == 0
        assert service.stats()["evictions"] == 1
        assert factory.calls == [key]

    @pytest.mark.asyncio
    async def test_close_clears_all_runtimes(self, tmp_path):
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"), AgentFactory(ScriptedProvider())
        )
        await service.handle_message(cli_key("c1"), "一")
        await service.handle_message(cli_key("c2"), "二")
        assert service.stats()["cached_agents"] == 2

        await service.close()

        assert service.stats()["cached_agents"] == 0
        assert service.stats()["locks"] == 0
        await service.close()  # 幂等

    @pytest.mark.asyncio
    async def test_close_during_inflight_turn_is_safe(self, tmp_path):
        provider = GatedProvider()
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"), AgentFactory(provider)
        )
        key = cli_key()

        task = asyncio.create_task(service.handle_message(key, "问题"))
        await wait_until(lambda: provider.started.is_set())

        await service.close()  # 关停清空缓存; 在途 turn 正常结束且 finally 不抛错
        assert service.stats()["cached_agents"] == 0

        provider.release.set()
        result = await task

        assert result.answer == "答"
        assert service.stats()["cached_agents"] == 0

    @pytest.mark.asyncio
    async def test_close_session_rejects_non_key(self, tmp_path):
        service = ConversationService(
            JsonlSessionStore(tmp_path / "memory"), AgentFactory(ScriptedProvider())
        )

        with pytest.raises(TypeError):
            await service.close_session("not-a-key")  # type: ignore[arg-type]
