"""AsyncioQueueBus 契约测试."""

import asyncio

import pytest

from meowmeowclaw.gateway import (
    DEADLETTER,
    INBOUND,
    AsyncioQueueBus,
    GatewayClosedError,
    MessageBus,
    make_inbound,
    outbound_queue,
)


def env(text: str = "t"):
    return make_inbound(channel="fake", text=text)


class TestPublishGet:
    @pytest.mark.asyncio
    async def test_fifo_per_queue(self):
        bus = AsyncioQueueBus()

        await bus.publish(INBOUND, env("1"))
        await bus.publish(INBOUND, env("2"))

        assert (await bus.get(INBOUND)).text == "1"
        assert (await bus.get(INBOUND)).text == "2"
        bus.task_done(INBOUND)
        bus.task_done(INBOUND)

    @pytest.mark.asyncio
    async def test_queues_are_isolated(self):
        bus = AsyncioQueueBus()

        await bus.publish(outbound_queue("a"), env("to-a"))
        await bus.publish(outbound_queue("b"), env("to-b"))

        assert (await bus.get(outbound_queue("a"))).text == "to-a"
        assert (await bus.get(outbound_queue("b"))).text == "to-b"

    @pytest.mark.asyncio
    async def test_get_blocks_until_publish(self):
        bus = AsyncioQueueBus()
        task = asyncio.create_task(bus.get(INBOUND))
        await asyncio.sleep(0.01)

        assert not task.done()
        await bus.publish(INBOUND, env("late"))
        assert (await asyncio.wait_for(task, timeout=1)).text == "late"


class TestBackpressureAndDeadletter:
    @pytest.mark.asyncio
    async def test_publish_timeout_goes_to_deadletter(self):
        bus = AsyncioQueueBus(maxsize=1, publish_timeout=0.05)

        assert await bus.publish(INBOUND, env("1")) is True
        assert await bus.publish(INBOUND, env("2")) is False

        dead = await asyncio.wait_for(bus.get(DEADLETTER), timeout=1)
        assert dead.text == "2"

    @pytest.mark.asyncio
    async def test_deadletter_queue_is_not_shared_with_inbound(self):
        bus = AsyncioQueueBus(maxsize=1, publish_timeout=0.05)
        await bus.publish(INBOUND, env("1"))
        await bus.publish(INBOUND, env("2"))

        assert (await bus.get(INBOUND)).text == "1"
        assert (await bus.get(DEADLETTER)).text == "2"


class TestObservability:
    @pytest.mark.asyncio
    async def test_queue_sizes_and_deadletter_count(self):
        bus = AsyncioQueueBus(maxsize=1, publish_timeout=0.01)

        await bus.publish(INBOUND, env("1"))
        await bus.publish(INBOUND, env("2"))  # 超时 -> 死信

        sizes = bus.queue_sizes()
        assert sizes[INBOUND] == 1
        assert sizes[DEADLETTER] == 1
        assert bus.deadletter_count == 1

    def test_empty_bus_snapshot(self):
        bus = AsyncioQueueBus()

        assert bus.queue_sizes() == {}
        assert bus.deadletter_count == 0


class TestCloseAndJoin:
    @pytest.mark.asyncio
    async def test_publish_after_close_raises(self):
        bus = AsyncioQueueBus()
        await bus.close()

        with pytest.raises(GatewayClosedError):
            await bus.publish(INBOUND, env())

    @pytest.mark.asyncio
    async def test_blocked_get_wakes_with_closed(self):
        bus = AsyncioQueueBus()
        task = asyncio.create_task(bus.get(INBOUND))
        await asyncio.sleep(0.01)
        await bus.close()

        with pytest.raises(GatewayClosedError):
            await asyncio.wait_for(task, timeout=1)

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self):
        bus = AsyncioQueueBus()
        await bus.close()
        await bus.close()

    @pytest.mark.asyncio
    async def test_join_waits_for_task_done(self):
        bus = AsyncioQueueBus()
        await bus.publish(INBOUND, env())
        await bus.get(INBOUND)

        join = asyncio.create_task(bus.join(INBOUND))
        await asyncio.sleep(0.01)
        assert not join.done()

        bus.task_done(INBOUND)
        await asyncio.wait_for(join, timeout=1)

    @pytest.mark.asyncio
    async def test_task_done_unknown_queue_is_noop(self):
        bus = AsyncioQueueBus()
        bus.task_done("no-such-queue")


class TestConstruction:
    def test_implements_protocol(self):
        assert isinstance(AsyncioQueueBus(), MessageBus)

    @pytest.mark.parametrize("kwargs", [
        {"maxsize": 0},
        {"maxsize": "x"},
        {"publish_timeout": 0},
        {"publish_timeout": "x"},
        {"deadletter_max_keep": 0},
    ])
    def test_invalid_args(self, kwargs):
        with pytest.raises(ValueError):
            AsyncioQueueBus(**kwargs)  # type: ignore[arg-type]
