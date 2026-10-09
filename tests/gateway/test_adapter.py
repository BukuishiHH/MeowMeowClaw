"""BaseChannelAdapter / LoopbackAdapter 契约测试."""

import asyncio

import pytest

from meowmeowclaw.gateway import (
    INBOUND,
    AsyncioQueueBus,
    BaseChannelAdapter,
    ChannelAdapter,
    LoopbackAdapter,
    make_reply,
    outbound_queue,
)


def envelope(channel: str, text: str = "t"):
    from meowmeowclaw.gateway import make_inbound
    return make_inbound(channel=channel, text=text)


class FailingOnceAdapter(BaseChannelAdapter):
    """首条发送抛错, 第二条成功; 验证出站循环不杀."""

    def __init__(self, channel: str = "flaky") -> None:
        super().__init__(channel=channel)
        self.sent: list = []
        self.failures = 0

    async def send(self, envelope) -> None:
        if self.failures == 0:
            self.failures += 1
            raise RuntimeError("boom")
        self.sent.append(envelope)


class TestContract:
    def test_loopback_satisfies_protocol(self):
        assert isinstance(LoopbackAdapter(), ChannelAdapter)


class TestLoopback:
    @pytest.mark.asyncio
    async def test_feed_publishes_inbound(self):
        bus = AsyncioQueueBus()
        adapter = LoopbackAdapter(channel="fake")
        await adapter.start(bus)

        assert await adapter.feed("你好") is True
        inbound = await asyncio.wait_for(bus.get(INBOUND), timeout=1)
        assert inbound.text == "你好"
        assert inbound.channel == "fake"
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_outbound_is_collected(self):
        bus = AsyncioQueueBus()
        adapter = LoopbackAdapter(channel="fake")
        await adapter.start(bus)
        request = envelope("fake")
        reply = make_reply(request, "答案")

        await bus.publish(outbound_queue("fake"), reply)

        sent = await adapter.wait_sent(1, timeout=1)
        assert [item.text for item in sent] == ["答案"]
        assert adapter.sent[0].correlation_id == request.message_id
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_stop_inbound_blocks_feed(self):
        bus = AsyncioQueueBus()
        adapter = LoopbackAdapter(channel="fake")
        await adapter.start(bus)

        await adapter.stop_inbound()

        assert await adapter.feed("late") is False
        assert bus.qsize(INBOUND) == 0
        await adapter.stop()


class TestBaseAdapter:
    @pytest.mark.asyncio
    async def test_start_is_idempotent(self):
        bus = AsyncioQueueBus()
        adapter = LoopbackAdapter(channel="fake")

        await adapter.start(bus)
        task = adapter._outbound_task  # noqa: SLF001 - 契约测试
        await adapter.start(bus)

        assert adapter._outbound_task is task  # noqa: SLF001
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_send_failure_does_not_kill_loop(self):
        bus = AsyncioQueueBus()
        adapter = FailingOnceAdapter()
        await adapter.start(bus)

        await bus.publish(outbound_queue("flaky"), envelope("flaky", "1"))
        await bus.publish(outbound_queue("flaky"), envelope("flaky", "2"))
        await asyncio.sleep(0.05)

        assert adapter.failures == 1
        assert [item.text for item in adapter.sent] == ["2"]
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_stop_drains_outbound_queue(self):
        bus = AsyncioQueueBus()
        adapter = LoopbackAdapter(channel="fake")
        await adapter.start(bus)
        await bus.publish(outbound_queue("fake"), envelope("fake", "1"))
        await bus.publish(outbound_queue("fake"), envelope("fake", "2"))

        await adapter.stop(timeout=1)

        assert [item.text for item in adapter.sent] == ["1", "2"]

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self):
        bus = AsyncioQueueBus()
        adapter = LoopbackAdapter(channel="fake")
        await adapter.start(bus)

        await adapter.stop()
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_publish_before_start_returns_false(self):
        adapter = LoopbackAdapter(channel="fake")

        assert await adapter.publish_inbound(envelope("fake")) is False
