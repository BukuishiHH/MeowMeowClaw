"""单进程 asyncio 消息总线(见 docs/GATEWAY_DESIGN.md §4).

队列拓扑:
    ``inbound``             适配器 -> dispatcher
    ``outbound:<channel>``  dispatcher/worker -> 各渠道适配器
    ``deadletter``          publish 超时(背压)消息, 仅排障用
"""

import asyncio
import logging
from typing import Protocol, runtime_checkable

from .envelope import Envelope

logger = logging.getLogger(__name__)

INBOUND = "inbound"
DEADLETTER = "deadletter"
# 每个队列 put 一个哨兵, 用于唤醒阻塞中的 get
_CLOSE = object()


def outbound_queue(channel: str) -> str:
    """出站队列键; 每个渠道一个, 慢渠道不会拖累其他渠道."""
    return f"outbound:{channel}"


class GatewayClosedError(RuntimeError):
    """总线已关闭; 阻塞中的 get/新 publish 都会收到该异常."""


@runtime_checkable
class MessageBus(Protocol):
    """消息总线契约; 未来可替换为 Redis Streams/NATS 实现."""

    async def publish(self, queue_key: str, envelope: Envelope) -> bool:
        """入队; 背压超时返回 False(消息进死信)."""
        ...

    async def get(self, queue_key: str) -> Envelope:
        """阻塞取出一条消息; 总线关闭时抛 :class:`GatewayClosedError`."""
        ...

    def task_done(self, queue_key: str) -> None:
        """标记一条已取消息处理完毕(供 join 排空判断)."""
        ...

    async def join(self, queue_key: str) -> None:
        """等待该队列中已取消息全部 task_done."""
        ...

    async def close(self) -> None:
        """拒绝新 publish, 唤醒所有阻塞中的 get."""
        ...


class AsyncioQueueBus:
    """基于 ``dict[str, asyncio.Queue]`` 的单进程实现."""

    def __init__(
        self,
        *,
        maxsize: int = 1000,
        publish_timeout: float = 5.0,
        deadletter_max_keep: int = 128,
    ) -> None:
        if not isinstance(maxsize, int) or isinstance(maxsize, bool) or maxsize <= 0:
            raise ValueError(f"maxsize 必须是正整数: {maxsize!r}")
        if not isinstance(publish_timeout, (int, float)) or isinstance(publish_timeout, bool):
            raise ValueError(f"publish_timeout 必须是正数: {publish_timeout!r}")
        if publish_timeout <= 0:
            raise ValueError(f"publish_timeout 必须是正数: {publish_timeout!r}")
        if (
            not isinstance(deadletter_max_keep, int)
            or isinstance(deadletter_max_keep, bool)
            or deadletter_max_keep <= 0
        ):
            raise ValueError(f"deadletter_max_keep 必须是正整数: {deadletter_max_keep!r}")

        self.maxsize = maxsize
        self.publish_timeout = float(publish_timeout)
        self.deadletter_max_keep = deadletter_max_keep
        self._queues: dict[str, asyncio.Queue] = {}
        self._closed = False

    def __repr__(self) -> str:
        return (
            f"<AsyncioQueueBus queues={sorted(self._queues)} maxsize={self.maxsize} "
            f"closed={self._closed}>"
        )

    # ------------------------------------------------------------------ 内部

    def _queue(self, queue_key: str) -> asyncio.Queue:
        if self._closed:
            raise GatewayClosedError("消息总线已关闭")
        queue = self._queues.get(queue_key)
        if queue is None:
            maxsize = self.deadletter_max_keep if queue_key == DEADLETTER else self.maxsize
            queue = asyncio.Queue(maxsize=maxsize)
            self._queues[queue_key] = queue
        return queue

    def _remember_deadletter(self, envelope: Envelope) -> None:
        try:
            self._queue(DEADLETTER).put_nowait(envelope)
        except (asyncio.QueueFull, GatewayClosedError):
            logger.warning("死信队列已满/已关闭, 丢弃消息: id=%s", envelope.message_id)

    @property
    def closed(self) -> bool:
        return self._closed

    def qsize(self, queue_key: str) -> int:
        queue = self._queues.get(queue_key)
        return queue.qsize() if queue is not None else 0

    def queue_sizes(self) -> dict[str, int]:
        """当前各队列积压快照(供 Gateway.stats 观测)."""
        return {key: queue.qsize() for key, queue in sorted(self._queues.items())}

    @property
    def deadletter_count(self) -> int:
        """死信条数(仅排障; v1 不自动重放)."""
        queue = self._queues.get(DEADLETTER)
        return queue.qsize() if queue is not None else 0

    # ------------------------------------------------------------------ 契约

    async def publish(self, queue_key: str, envelope: Envelope) -> bool:
        queue = self._queue(queue_key)
        try:
            await asyncio.wait_for(queue.put(envelope), timeout=self.publish_timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "消息总线背压超时(%.1fs), 消息进死信: queue=%s id=%s",
                self.publish_timeout,
                queue_key,
                envelope.message_id,
            )
            self._remember_deadletter(envelope)
            return False
        return True

    async def get(self, queue_key: str) -> Envelope:
        queue = self._queue(queue_key)
        while True:
            item = await queue.get()
            if item is _CLOSE:
                raise GatewayClosedError("消息总线已关闭")
            return item

    def task_done(self, queue_key: str) -> None:
        queue = self._queues.get(queue_key)
        if queue is not None:
            queue.task_done()

    async def join(self, queue_key: str) -> None:
        queue = self._queues.get(queue_key)
        if queue is not None:
            await queue.join()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for queue_key, queue in self._queues.items():
            while True:
                try:
                    queue.put_nowait(_CLOSE)
                    break
                except asyncio.QueueFull:
                    # 队列已满: 丢最旧一条给哨兵让位, 唤醒阻塞的 get
                    try:
                        queue.get_nowait()
                        queue.task_done()
                    except asyncio.QueueEmpty:  # pragma: no cover - 竞态兜底
                        break
            logger.debug("关闭消息总线队列: %s qsize=%d", queue_key, queue.qsize())
