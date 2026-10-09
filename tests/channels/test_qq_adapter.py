"""QqAdapter(Fake 传输) 契约测试."""

import asyncio

import pytest

from meowmeowclaw.channels.qq_adapter import QqAdapter
from meowmeowclaw.gateway import INBOUND, AsyncioQueueBus, make_reply

from _qq_helpers import qq_message


class TestQqAdapter:
    @pytest.mark.asyncio
    async def test_feed_converts_incoming_to_envelope(self):
        bus = AsyncioQueueBus()
        adapter = QqAdapter()
        await adapter.start(bus)

        assert await adapter.feed(qq_message("你好", message_id="m-1")) is True
        envelope = await asyncio.wait_for(bus.get(INBOUND), timeout=1)

        assert envelope.channel == "qq"
        assert envelope.text == "你好"
        assert envelope.message_id == "m-1"
        assert envelope.scope == "private"
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_send_collects_and_calls_transport(self):
        seen = []

        async def transport(envelope):
            seen.append(envelope)

        adapter = QqAdapter(transport=transport)
        bus = AsyncioQueueBus()
        await adapter.start(bus)
        request = await self._envelope(adapter, "hi")
        reply = make_reply(request, "答案")

        await bus.publish("outbound:qq", reply)
        sent = await adapter.wait_sent(1, timeout=1)

        assert [item.text for item in sent] == ["答案"]
        assert seen == list(adapter.sent)
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_stop_inbound_blocks_feed(self):
        bus = AsyncioQueueBus()
        adapter = QqAdapter()
        await adapter.start(bus)
        await adapter.stop_inbound()

        assert await adapter.feed(qq_message("late")) is False
        assert bus.qsize(INBOUND) == 0
        await adapter.stop()

    async def _envelope(self, adapter: QqAdapter, text: str):
        """通过 feed 走一遍入站, 从 bus 取出信封(不启动 dispatcher)."""
        from meowmeowclaw.channels.bridge import incoming_to_envelope

        return incoming_to_envelope(qq_message(text))
