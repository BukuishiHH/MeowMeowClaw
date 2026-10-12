"""网关流式通道测试(S2): KIND_STREAM 信封 / best-effort delta / dispatcher 三相位 / 水位线.

对照 docs/STREAMING_DESIGN.md §4.5/§4.6:
- ``start`` → (``delta``)* → ``end`` → ``reply`` 的顺序与字段;
- delta 走 best-effort: 背压丢弃且不写死信, 丢 delta 不影响 reply;
- ``stream_enabled=False`` 时**零 stream 信封**, 且不向 worker 传 on_delta(零回归);
- worker 异常也要发 ``end``(避免渠道状态悬挂), 且不发 reply;
- ``StreamAssembler``: 前缀水位线、只补后缀、前缀不一致回退整段、分片、LRU 清理。
"""

import asyncio
import logging
from typing import Any, Optional

import pytest

from meowmeowclaw.channels.streaming import StreamAssembler, chunk_text
from meowmeowclaw.gateway import (
    DEADLETTER,
    INBOUND,
    KIND_REPLY,
    KIND_STREAM,
    STREAM_PHASE_DELTA,
    STREAM_PHASE_END,
    STREAM_PHASE_START,
    AgentWorker,
    AsyncioQueueBus,
    Envelope,
    Gateway,
    GatewayDispatcher,
    PolicyDecision,
    make_inbound,
    make_stream_event,
    outbound_queue,
)

from _helpers import FixedPolicy, StubConversation, session_key

CHANNEL = "fake"


# --------------------------------------------------------------------- 工具


def agent_decision(text: str = "hi") -> PolicyDecision:
    return PolicyDecision.agent(session_key(), text, meta={"channel": CHANNEL})


async def collect_outbound(
    bus: AsyncioQueueBus, count: int, timeout: float = 3.0
) -> list[Envelope]:
    """从 outbound:<CHANNEL> 取 count 条信封(取完 task_done)."""
    key = outbound_queue(CHANNEL)
    items: list[Envelope] = []
    for _ in range(count):
        envelope = await asyncio.wait_for(bus.get(key), timeout=timeout)
        bus.task_done(key)
        items.append(envelope)
    return items


async def build_dispatcher(
    *,
    stream_enabled: bool,
    conversation: StubConversation,
    bus: Optional[AsyncioQueueBus] = None,
):
    bus = bus or AsyncioQueueBus()
    worker = AgentWorker(conversation)
    policy = FixedPolicy(CHANNEL, lambda envelope: agent_decision(envelope.text))
    dispatcher = GatewayDispatcher(
        bus=bus,
        worker=worker,
        policies={CHANNEL: policy},
        stream_enabled=stream_enabled,
    )
    return bus, dispatcher


class DroppingBus(AsyncioQueueBus):
    """把所有 best-effort delta 都丢掉的替身: 验证"丢 delta 不影响 reply"."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.best_effort_calls = 0

    async def publish_best_effort(
        self, queue_key: str, envelope: Envelope, timeout: float = 0.5
    ) -> bool:
        self.best_effort_calls += 1
        return False


# --------------------------------------------------------------- 信封契约


class TestStreamEnvelope:
    def test_make_stream_event_fields(self):
        request = make_inbound(
            channel="qq",
            text="你好",
            scope="private",
            conversation_id="2328428288",
            sender_id="2328428288",
            message_id="m-1",
        )

        event = make_stream_event(request, phase=STREAM_PHASE_DELTA, text="你", seq=3)

        assert event.kind == KIND_STREAM
        assert event.channel == "qq"
        assert event.target_channel == "qq"
        assert event.conversation_id == "2328428288"
        assert event.sender_id == "2328428288"
        assert event.correlation_id == "m-1"
        assert event.reply_to == "m-1"
        assert event.text == "你"
        assert event.delta_seq == 3
        assert event.metadata["phase"] == STREAM_PHASE_DELTA
        assert event.message_id and event.message_id != "m-1"  # 内部新 ID

    @pytest.mark.parametrize("phase", [STREAM_PHASE_START, STREAM_PHASE_END])
    def test_start_end_phases(self, phase):
        request = make_inbound(channel="qq", text="hi", message_id="m-1")

        event = make_stream_event(request, phase=phase)

        assert event.text == ""
        assert event.metadata["phase"] == phase

    def test_invalid_phase_rejected(self):
        request = make_inbound(channel="qq", text="hi", message_id="m-1")

        with pytest.raises(ValueError):
            make_stream_event(request, phase="middle")

    @pytest.mark.parametrize("bad", [-1, True, "1", None])
    def test_delta_seq_validation(self, bad):
        with pytest.raises(ValueError):
            Envelope(kind=KIND_STREAM, channel="qq", target_channel="qq", delta_seq=bad)

    def test_stream_kind_requires_channel(self):
        with pytest.raises(ValueError):
            Envelope(kind=KIND_STREAM, channel="")


# --------------------------------------------------------------- best-effort


class TestBestEffortPublish:
    @pytest.mark.asyncio
    async def test_backpressure_drops_without_deadletter(self):
        bus = AsyncioQueueBus(maxsize=1, publish_timeout=0.05)
        key = outbound_queue(CHANNEL)
        first = make_inbound(channel=CHANNEL, text="占位", message_id="m1")
        assert await bus.publish(key, first) is True  # 队列已满

        delta = make_stream_event(
            make_inbound(channel=CHANNEL, text="hi", message_id="m2"),
            phase=STREAM_PHASE_DELTA,
            text="x",
            seq=1,
        )
        dropped = await bus.publish_best_effort(key, delta, timeout=0.05)

        assert dropped is False
        assert bus.deadletter_count == 0  # 关键: 不走死信
        assert bus.qsize(key) == 1

    @pytest.mark.asyncio
    async def test_publish_best_effort_after_close_returns_false(self):
        bus = AsyncioQueueBus()
        await bus.close()
        delta = make_stream_event(
            make_inbound(channel=CHANNEL, text="hi", message_id="m1"),
            phase=STREAM_PHASE_DELTA,
            text="x",
        )

        assert await bus.publish_best_effort(outbound_queue(CHANNEL), delta) is False

    @pytest.mark.asyncio
    async def test_best_effort_on_deadletter_queue_is_plain_queue_put(self):
        """best-effort 自身不会主动往死信塞东西(死信作为普通队列时也能放)."""
        bus = AsyncioQueueBus(maxsize=1, publish_timeout=0.05, deadletter_max_keep=1)
        delta = make_stream_event(
            make_inbound(channel=CHANNEL, text="hi", message_id="m1"),
            phase=STREAM_PHASE_DELTA,
            text="x",
        )

        assert await bus.publish_best_effort(DEADLETTER, delta, timeout=0.05) is True
        assert await bus.publish_best_effort(DEADLETTER, delta, timeout=0.05) is False
        assert bus.deadletter_count == 1


# --------------------------------------------------------------- dispatcher


class TestDispatcherStreamPhases:
    @pytest.mark.asyncio
    async def test_start_delta_end_then_reply(self):
        conversation = StubConversation(answer="你好呀", deltas=["你好", "呀"])
        bus, dispatcher = await build_dispatcher(stream_enabled=True, conversation=conversation)
        await dispatcher.start()
        try:
            inbound = make_inbound(
                channel=CHANNEL, text="hi", conversation_id="c1", message_id="m-1"
            )
            await bus.publish(INBOUND, inbound)
            events = await collect_outbound(bus, 5)
        finally:
            await dispatcher.stop()

        phases = [event.metadata.get("phase") for event in events]
        assert phases == [
            STREAM_PHASE_START,
            STREAM_PHASE_DELTA,
            STREAM_PHASE_DELTA,
            STREAM_PHASE_END,
            None,  # reply 没有 phase
        ]
        assert [event.delta_seq for event in events[:4]] == [0, 1, 2, 0]
        assert [event.text for event in events[1:3]] == ["你好", "呀"]
        assert events[0].kind == KIND_STREAM and events[-1].kind == KIND_REPLY
        assert events[-1].text == "你好呀"
        assert events[-1].correlation_id == "m-1"
        assert events[0].correlation_id == "m-1"
        # worker 确实拿到了 on_delta 回调(该轮走流式)
        assert conversation.calls[0]["on_delta"] is not None

    @pytest.mark.asyncio
    async def test_stream_disabled_emits_only_reply(self):
        conversation = StubConversation(answer="普通回答")
        bus, dispatcher = await build_dispatcher(stream_enabled=False, conversation=conversation)
        await dispatcher.start()
        try:
            await bus.publish(
                INBOUND,
                make_inbound(channel=CHANNEL, text="hi", conversation_id="c1", message_id="m-1"),
            )
            events = await collect_outbound(bus, 1)
        finally:
            await dispatcher.stop()

        assert [event.kind for event in events] == [KIND_REPLY]
        assert conversation.calls[0]["on_delta"] is None  # 零回归: 不启用流式

    @pytest.mark.asyncio
    async def test_worker_failure_still_emits_end_without_reply(self, caplog):
        conversation = StubConversation(error=RuntimeError("agent boom"), deltas=[])
        bus, dispatcher = await build_dispatcher(stream_enabled=True, conversation=conversation)
        await dispatcher.start()
        try:
            with caplog.at_level(logging.ERROR, logger="meowmeowclaw.gateway.dispatcher"):
                await bus.publish(
                    INBOUND,
                    make_inbound(
                        channel=CHANNEL, text="hi", conversation_id="c1", message_id="m-1"
                    ),
                )
                events = await collect_outbound(bus, 2)
        finally:
            await dispatcher.stop()

        assert [event.metadata.get("phase") for event in events] == [
            STREAM_PHASE_START,
            STREAM_PHASE_END,
        ]
        assert all(event.kind == KIND_STREAM for event in events)
        assert any("Agent 处理失败" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_dropped_deltas_do_not_block_final_reply(self):
        """delta 全部被丢弃(背压)时, reply 仍必须送达且死信为 0."""
        conversation = StubConversation(answer="答案", deltas=["答", "案"])
        bus = DroppingBus()
        _, dispatcher = await build_dispatcher(
            stream_enabled=True, conversation=conversation, bus=bus
        )
        await dispatcher.start()
        try:
            await bus.publish(
                INBOUND,
                make_inbound(channel=CHANNEL, text="hi", conversation_id="c1", message_id="m-1"),
            )
            # 只有 start/end/reply 三条(两条 delta 被丢)
            events = await collect_outbound(bus, 3)
        finally:
            await dispatcher.stop()

        assert bus.best_effort_calls == 2
        assert [event.metadata.get("phase") for event in events] == [
            STREAM_PHASE_START,
            STREAM_PHASE_END,
            None,
        ]
        assert events[-1].kind == KIND_REPLY and events[-1].text == "答案"
        assert bus.deadletter_count == 0

    def test_gateway_stats_exposes_stream_flag(self):
        gateway = Gateway(conversation=StubConversation(), stream_enabled=True)
        assert gateway.stats()["stream_enabled"] is True
        assert Gateway(conversation=StubConversation()).stats()["stream_enabled"] is False


# --------------------------------------------------------------- 水位线


class TestStreamAssembler:
    def test_plan_final_returns_only_suffix(self):
        assembler = StreamAssembler()
        assembler.mark_sent("m1", "你好")

        assert assembler.plan_final("m1", "你好呀世界") == ["呀世界"]
        assert assembler.sent_text("m1") == "你好"

    def test_plan_final_without_sent_content_returns_full_text(self):
        assembler = StreamAssembler()

        assert assembler.plan_final("m1", "完整回答") == ["完整回答"]

    def test_plan_final_prefix_mismatch_resends_all_with_warning(self, caplog):
        assembler = StreamAssembler()
        assembler.mark_sent("m1", "我先查一下")

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.channels.streaming"):
            plan = assembler.plan_final("m1", "最终答案")

        assert plan == ["最终答案"]
        assert any("前缀与最终文本不一致" in record.message for record in caplog.records)

    def test_plan_final_chunks_long_remaining_text(self):
        assembler = StreamAssembler(max_chars=3)
        assembler.mark_sent("m1", "ab")

        assert assembler.plan_final("m1", "abcdefg") == ["cde", "fg"]

    def test_forget_and_clear(self):
        assembler = StreamAssembler()
        assembler.mark_sent("m1", "x")
        assembler.mark_sent("m2", "y")

        assembler.forget("m1")

        assert assembler.size == 1
        assert assembler.sent_text("m1") == ""
        assembler.clear()
        assert assembler.size == 0

    def test_lru_eviction_keeps_latest(self):
        assembler = StreamAssembler(max_entries=2)
        assembler.mark_sent("m1", "a")
        assembler.mark_sent("m2", "b")
        assembler.mark_sent("m3", "c")  # 触发淘汰最旧的 m1

        assert assembler.sent_text("m1") == ""
        assert assembler.sent_text("m2") == "b"
        assert assembler.sent_text("m3") == "c"

    def test_mark_sent_ignores_empty_text(self):
        assembler = StreamAssembler()
        assembler.mark_sent("m1", "")
        assert assembler.size == 0

    def test_register_refreshes_lru(self):
        assembler = StreamAssembler(max_entries=2)
        assembler.mark_sent("m1", "a")
        assembler.mark_sent("m2", "b")
        assembler.register("m1")  # m1 变为最近使用
        assembler.mark_sent("m3", "c")

        assert assembler.sent_text("m1") == "a"
        assert assembler.sent_text("m2") == ""

    def test_chunk_text_contract(self):
        assert chunk_text("abcdefg", 3) == ["abc", "def", "g"]
        assert chunk_text("abc", 3) == ["abc"]
        assert chunk_text("", 3) == []
        assert chunk_text("abcdefg", 0) == ["abcdefg"]
