"""Gateway 组装与生命周期测试."""

import asyncio

import pytest

from meowmeowclaw.gateway import (
    AgentWorker,
    AsyncioQueueBus,
    Gateway,
    LoopbackAdapter,
    PolicyDecision,
    make_inbound,
    make_reply,
    new_message_id,
)

from _helpers import FixedPolicy, StubConversation, session_key

CHANNEL = "fake"


def make_gateway(conversation=None, *, max_inflight=4):
    conversation = conversation or StubConversation()
    policy = FixedPolicy(
        CHANNEL,
        lambda env: PolicyDecision.agent(session_key(), env.text, meta={"channel": CHANNEL}),
    )
    return Gateway(
        conversation=conversation,  # type: ignore[arg-type]
        policies={CHANNEL: policy},
        max_inflight=max_inflight,
        shutdown_timeout=1.0,
    )


class TestAssembly:
    def test_register_duplicate_channel(self):
        gateway = make_gateway()
        gateway.register(LoopbackAdapter(CHANNEL))

        with pytest.raises(ValueError, match="已注册"):
            gateway.register(LoopbackAdapter(CHANNEL))

    @pytest.mark.asyncio
    async def test_register_after_start_rejected(self):
        gateway = make_gateway()
        await gateway.start()

        with pytest.raises(RuntimeError, match="已启动"):
            gateway.register(LoopbackAdapter("late"))
        await gateway.stop()

    def test_implements_worker(self):
        gateway = make_gateway()
        assert isinstance(gateway.worker, AgentWorker)

    @pytest.mark.asyncio
    async def test_custom_bus_is_reused(self):
        bus = AsyncioQueueBus(maxsize=7)
        gateway = Gateway(conversation=StubConversation(), bus=bus)  # type: ignore[arg-type]

        assert gateway.bus is bus


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop_idempotent(self):
        gateway = make_gateway()
        await gateway.start()
        await gateway.start()
        assert gateway.started is True

        await gateway.stop()
        await gateway.stop()
        assert gateway.started is False

    @pytest.mark.asyncio
    async def test_stop_without_start_is_noop(self):
        gateway = make_gateway()
        await gateway.stop()  # 不应抛异常

    @pytest.mark.asyncio
    async def test_context_manager(self):
        gateway = make_gateway()
        async with gateway as entered:
            assert entered is gateway
            assert gateway.started is True
        assert gateway.started is False

    @pytest.mark.asyncio
    async def test_end_to_end_loopback(self):
        gateway = make_gateway(StubConversation(answer="端到端回答"))
        adapter = LoopbackAdapter(CHANNEL)

        await gateway.start(adapters=[adapter])
        assert await adapter.feed("你好") is True
        sent = await adapter.wait_sent(1, timeout=1)

        assert [item.text for item in sent] == ["端到端回答"]
        assert sent[0].target_channel == CHANNEL
        await gateway.stop()

    @pytest.mark.asyncio
    async def test_stop_drains_inflight_before_outbound_stop(self):
        conversation = StubConversation(answer="慢回答", delay=0.2)
        gateway = make_gateway(conversation)
        adapter = LoopbackAdapter(CHANNEL)
        await gateway.start(adapters=[adapter])

        await adapter.feed("hi")
        await asyncio.sleep(0.01)
        await gateway.stop(timeout=2)

        assert [item.text for item in adapter.sent] == ["慢回答"]

    @pytest.mark.asyncio
    async def test_push_routes_to_target_channel(self):
        gateway = make_gateway()
        adapter = LoopbackAdapter(CHANNEL)
        await gateway.start(adapters=[adapter])

        push = make_inbound(channel=CHANNEL, text="push", message_id=new_message_id())
        push = make_reply(push, "主动通知", kind="push")
        assert await gateway.push(push) is True

        sent = await adapter.wait_sent(1, timeout=1)
        assert [item.text for item in sent] == ["主动通知"]
        await gateway.stop()
