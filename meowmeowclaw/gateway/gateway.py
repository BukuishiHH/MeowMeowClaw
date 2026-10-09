"""Gateway: 总线 + 适配器 + 策略 + dispatcher/worker 的组装与生命周期.

见 docs/GATEWAY_DESIGN.md §2/§8. 单进程内运行; ``start()`` 必须在事件循环中调用.
"""

import asyncio
import logging
from typing import Iterable, Mapping, Optional

from meowmeowclaw.conversation import ConversationService

from .adapter import ChannelAdapter
from .bus import AsyncioQueueBus, MessageBus
from .dedup import DedupCache
from .dispatcher import GATEWAY_MAX_INFLIGHT, GatewayDispatcher
from .policy import ChannelPolicy
from .worker import AgentWorker

logger = logging.getLogger(__name__)


class Gateway:
    """多渠道网关(每进程一个实例)."""

    def __init__(
        self,
        *,
        conversation: ConversationService,
        bus: Optional[MessageBus] = None,
        policies: Optional[Mapping[str, ChannelPolicy]] = None,
        dedup: Optional[DedupCache] = None,
        bus_maxsize: int = 1000,
        publish_timeout: float = 5.0,
        shutdown_timeout: float = 10.0,
        max_inflight: int = GATEWAY_MAX_INFLIGHT,
        deadletter_max_keep: int = 128,
    ) -> None:
        self.shutdown_timeout = float(shutdown_timeout)
        self.bus: MessageBus = bus or AsyncioQueueBus(
            maxsize=bus_maxsize,
            publish_timeout=publish_timeout,
            deadletter_max_keep=deadletter_max_keep,
        )
        self.worker = AgentWorker(conversation)
        self.dispatcher = GatewayDispatcher(
            bus=self.bus,
            worker=self.worker,
            policies=policies,
            dedup=dedup,
            max_inflight=max_inflight,
            shutdown_timeout=self.shutdown_timeout,
        )
        self._adapters: dict[str, ChannelAdapter] = {}
        self._started = False

    def __repr__(self) -> str:
        return (
            f"<Gateway adapters={sorted(self._adapters)} policies={sorted(self.dispatcher.policies)} "
            f"started={self._started}>"
        )

    @property
    def started(self) -> bool:
        return self._started

    @property
    def adapters(self) -> tuple[ChannelAdapter, ...]:
        return tuple(self._adapters.values())

    def register(self, adapter: ChannelAdapter) -> ChannelAdapter:
        """注册适配器(必须在 start 之前); 同渠道只允许一个."""
        if self._started:
            raise RuntimeError("Gateway 已启动, 不能再注册适配器")
        channel = str(getattr(adapter, "channel", ""))
        if not channel:
            raise ValueError("adapter.channel 不能为空")
        if channel in self._adapters:
            raise ValueError(f"渠道适配器已注册: {channel}")
        self._adapters[channel] = adapter
        return adapter

    def add_policy(self, policy: ChannelPolicy) -> ChannelPolicy:
        self.dispatcher.add_policy(policy)
        return policy

    # ------------------------------------------------------------------ 生命周期

    async def start(self, adapters: Optional[Iterable[ChannelAdapter]] = None) -> None:
        """启动适配器与 dispatcher; 幂等."""
        if self._started:
            return
        for adapter in adapters or ():
            self.register(adapter)
        for adapter in self._adapters.values():
            await adapter.start(self.bus)
        await self.dispatcher.start()
        self._started = True
        logger.info("网关已启动: adapters=%s", sorted(self._adapters))

    async def stop(self, timeout: Optional[float] = None) -> None:
        """优雅关闭: 停入站 -> 排空 dispatcher/in-flight -> 排空各渠道出站 -> 关总线; 幂等."""
        if not self._started:
            return
        self._started = False
        budget = self.shutdown_timeout if timeout is None else float(timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, budget)

        for adapter in self._adapters.values():
            try:
                await adapter.stop_inbound()
            except Exception:  # noqa: BLE001 关闭阶段只日志
                logger.exception("停止入站失败: %s", adapter.channel)

        await self.dispatcher.stop(max(0.0, deadline - loop.time()))

        for adapter in self._adapters.values():
            try:
                await adapter.stop(max(0.0, deadline - loop.time()))
            except Exception:  # noqa: BLE001 关闭阶段只日志
                logger.exception("停止适配器失败: %s", adapter.channel)

        await self.bus.close()
        logger.info("网关已停止")

    async def __aenter__(self) -> "Gateway":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    # ------------------------------------------------------------------ 预留

    async def push(self, envelope) -> bool:
        """预留: 主动推送(无入站请求), 直接投递到目标渠道出站队列."""
        from .bus import outbound_queue  # 局部 import 避免顶部循环

        target = envelope.target_channel or envelope.channel
        return await self.bus.publish(outbound_queue(target), envelope)
