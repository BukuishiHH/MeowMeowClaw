"""CliAdapter 契约测试(同步 REPL + 异步总线)."""

import asyncio

import pytest

from meowmeowclaw.channels.cli_adapter import CliAdapter
from meowmeowclaw.gateway import (
    INBOUND,
    AsyncioQueueBus,
    make_reply,
    outbound_queue,
)


async def start_bus_pair():
    bus = AsyncioQueueBus()
    adapter = CliAdapter(print_func=lambda text: None)
    await adapter.start(bus)
    return bus, adapter


class TestAsk:
    @pytest.mark.asyncio
    async def test_ask_round_trip(self):
        bus, adapter = await start_bus_pair()

        async def responder():
            request = await bus.get(INBOUND)
            await bus.publish(outbound_queue("cli"), make_reply(request, "回答"))
            bus.task_done(INBOUND)

        task = asyncio.create_task(responder())
        reply = await asyncio.wait_for(adapter.ask("你好"), timeout=1)

        assert reply is not None and reply.text == "回答"
        assert adapter.sent[0].text == "回答"
        await task
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_ask_timeout_returns_none(self):
        bus, adapter = await start_bus_pair()
        adapter.reply_timeout = 0.05

        assert await adapter.ask("无人回复") is None
        assert adapter._pending == {}  # noqa: SLF001
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_ask_before_start_returns_none(self):
        adapter = CliAdapter()

        assert await adapter.ask("hi") is None

    @pytest.mark.asyncio
    async def test_stop_cancels_pending_ask(self):
        bus, adapter = await start_bus_pair()
        task = asyncio.create_task(adapter.ask("挂起"))
        await asyncio.sleep(0.01)

        await adapter.stop()

        with pytest.raises(asyncio.CancelledError):
            await task


class TestRunRepl:
    @pytest.mark.asyncio
    async def test_exit_and_eof(self):
        bus = AsyncioQueueBus()
        outputs: list[str] = []
        inputs = iter(["/exit"])
        adapter = CliAdapter(
            input_func=lambda prompt: next(inputs),
            print_func=outputs.append,
        )
        await adapter.start(bus)

        assert await adapter.run_repl() == "exit"
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_eof_returns_eof(self):
        bus = AsyncioQueueBus()
        adapter = CliAdapter(
            input_func=lambda prompt: (_ for _ in ()).throw(EOFError()),
            print_func=lambda text: None,
        )
        await adapter.start(bus)

        assert await adapter.run_repl() == "eof"
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_blank_input_skipped_then_exit(self):
        bus = AsyncioQueueBus()
        outputs: list[str] = []
        inputs = iter(["   ", "/quit"])
        adapter = CliAdapter(
            input_func=lambda prompt: next(inputs),
            print_func=outputs.append,
        )
        await adapter.start(bus)

        assert await adapter.run_repl() == "exit"
        assert outputs == []
        await adapter.stop()
