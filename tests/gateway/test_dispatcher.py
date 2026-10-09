"""GatewayDispatcher 契约测试."""

import asyncio

import pytest

from meowmeowclaw.gateway import (
    AsyncioQueueBus,
    DedupCache,
    GatewayDispatcher,
    INBOUND,
    PolicyDecision,
    make_inbound,
    make_reply,
    outbound_queue,
    AgentWorker,
)

from _helpers import FixedPolicy, StubConversation, session_key

CHANNEL = "fake"


def make_dispatcher(conversation=None, *, decide=None, max_inflight=4, shutdown_timeout=1.0):
    conversation = conversation or StubConversation()
    bus = AsyncioQueueBus()
    decide = decide or (
        lambda env: PolicyDecision.agent(session_key(), env.text, meta={"channel": CHANNEL})
    )
    dispatcher = GatewayDispatcher(
        bus=bus,
        worker=AgentWorker(conversation),  # type: ignore[arg-type]
        policies={CHANNEL: FixedPolicy(CHANNEL, decide)},
        dedup=DedupCache(),
        max_inflight=max_inflight,
        shutdown_timeout=shutdown_timeout,
    )
    return dispatcher, bus, conversation


async def drain_outbound(bus, count=1, timeout=1.0):
    items = []
    for _ in range(count):
        items.append(await asyncio.wait_for(bus.get(outbound_queue(CHANNEL)), timeout=timeout))
    return items


class TestDispatch:
    @pytest.mark.asyncio
    async def test_agent_reply_routed_to_outbound(self):
        dispatcher, bus, conversation = make_dispatcher()
        await dispatcher.start()

        await bus.publish(INBOUND, make_inbound(channel=CHANNEL, text="hi"))
        reply = (await drain_outbound(bus))[0]

        assert reply.text == "回答"
        assert reply.target_channel == CHANNEL
        assert conversation.calls[0]["text"] == "hi"
        await dispatcher.stop()

    @pytest.mark.asyncio
    async def test_reply_policy_short_circuits_agent(self):
        request = make_inbound(channel=CHANNEL, text="/help")

        def decide(env):
            return PolicyDecision.reply(make_reply(env, "帮助文本"))

        dispatcher, bus, conversation = make_dispatcher(decide=decide)
        await dispatcher.start()

        await bus.publish(INBOUND, request)
        reply = (await drain_outbound(bus))[0]

        assert reply.text == "帮助文本"
        assert conversation.calls == []
        await dispatcher.stop()

    @pytest.mark.asyncio
    async def test_ignore_policy_drops_silently(self):
        dispatcher, bus, conversation = make_dispatcher(
            decide=lambda env: PolicyDecision.ignore()
        )
        await dispatcher.start()

        await bus.publish(INBOUND, make_inbound(channel=CHANNEL, text="spam"))
        await asyncio.sleep(0.05)

        assert conversation.calls == []
        assert bus.qsize(outbound_queue(CHANNEL)) == 0
        await dispatcher.stop()

    @pytest.mark.asyncio
    async def test_pre_replies_are_sent_before_agent_reply(self):
        request = make_inbound(channel=CHANNEL, text="hi")

        def decide(env):
            return PolicyDecision.agent(
                session_key(),
                env.text,
                pre_replies=(make_reply(env, "已开始新对话"),),
            )

        dispatcher, bus, _ = make_dispatcher(decide=decide)
        await dispatcher.start()

        await bus.publish(INBOUND, request)
        first, second = await drain_outbound(bus, count=2)

        assert first.text == "已开始新对话"
        assert second.text == "回答"
        await dispatcher.stop()

    @pytest.mark.asyncio
    async def test_duplicate_message_is_ignored(self):
        dispatcher, bus, conversation = make_dispatcher()
        await dispatcher.start()
        request = make_inbound(channel=CHANNEL, text="hi", message_id="m-1")

        await bus.publish(INBOUND, request)
        await bus.publish(INBOUND, request)
        await drain_outbound(bus, count=1)
        await asyncio.sleep(0.05)

        assert len(conversation.calls) == 1
        await dispatcher.stop()

    @pytest.mark.asyncio
    async def test_unknown_channel_is_ignored_without_crash(self):
        dispatcher, bus, conversation = make_dispatcher()
        await dispatcher.start()

        await bus.publish(INBOUND, make_inbound(channel="unknown", text="hi"))
        await asyncio.sleep(0.05)

        assert conversation.calls == []
        await dispatcher.stop()

    @pytest.mark.asyncio
    async def test_worker_error_does_not_stop_dispatcher(self):
        conversation = StubConversation(error=RuntimeError("boom"))
        dispatcher, bus, conversation = make_dispatcher(conversation=conversation)
        await dispatcher.start()

        await bus.publish(INBOUND, make_inbound(channel=CHANNEL, text="bad"))
        await asyncio.sleep(0.05)
        assert bus.qsize(outbound_queue(CHANNEL)) == 0

        conversation.error = None  # 下一条恢复正常
        await bus.publish(INBOUND, make_inbound(channel=CHANNEL, text="good"))
        reply = (await drain_outbound(bus))[0]
        assert reply.text == "回答"
        await dispatcher.stop()


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_stop_waits_for_inflight(self):
        conversation = StubConversation(answer="慢回答", delay=0.2)
        dispatcher, bus, _ = make_dispatcher(conversation=conversation)
        await dispatcher.start()

        await bus.publish(INBOUND, make_inbound(channel=CHANNEL, text="hi"))
        await asyncio.sleep(0.01)
        await dispatcher.stop(timeout=2)

        reply = await asyncio.wait_for(bus.get(outbound_queue(CHANNEL)), timeout=1)
        assert reply.text == "慢回答"

    @pytest.mark.asyncio
    async def test_stop_timeout_cancels_inflight(self):
        conversation = StubConversation(answer="永远等不到", delay=5.0)
        dispatcher, bus, _ = make_dispatcher(conversation=conversation)
        await dispatcher.start()

        await bus.publish(INBOUND, make_inbound(channel=CHANNEL, text="hi"))
        await asyncio.sleep(0.01)
        await asyncio.wait_for(dispatcher.stop(timeout=0.05), timeout=2)

        assert dispatcher.inflight_count == 0
        assert bus.qsize(outbound_queue(CHANNEL)) == 0

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self):
        dispatcher, _, _ = make_dispatcher()
        await dispatcher.start()
        first = dispatcher._task  # noqa: SLF001
        await dispatcher.start()

        assert dispatcher._task is first  # noqa: SLF001
        await dispatcher.stop()

    def test_invalid_max_inflight(self):
        bus = AsyncioQueueBus()
        with pytest.raises(ValueError):
            GatewayDispatcher(
                bus=bus,
                worker=AgentWorker(StubConversation()),  # type: ignore[arg-type]
                max_inflight=0,
            )
