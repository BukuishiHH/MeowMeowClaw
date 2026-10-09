"""渠道适配器契约与基类(见 docs/GATEWAY_DESIGN.md §5).

适配器只做协议转换与收发:
- 入站: 平台事件 -> ``Envelope`` -> ``publish_inbound()``;
- 出站: 基类消费 ``outbound:<channel>``, 调用子类 ``send()``.

生命周期分两段, 保证优雅关闭: ``stop_inbound()`` 先停止接收, 等 dispatcher/in-flight 排空后,
再 ``stop()`` 排空本渠道出站队列并释放资源.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from contextlib import suppress
from typing import Optional, Protocol, Sequence, runtime_checkable

from .bus import INBOUND, GatewayClosedError, MessageBus, outbound_queue
from .envelope import Envelope, make_inbound

logger = logging.getLogger(__name__)


@runtime_checkable
class ChannelAdapter(Protocol):
    """渠道适配器契约(结构化类型)."""

    channel: str

    async def start(self, bus: MessageBus) -> None:
        """启动适配器(幂等); 必须先起出站消费再开始接收入站."""
        ...

    async def stop_inbound(self) -> None:
        """停止接收新入站(幂等); 已接收消息仍可继续派发."""
        ...

    async def stop(self, timeout: float = 5.0) -> None:
        """排空出站队列并释放资源(幂等)."""
        ...

    async def send(self, envelope: Envelope) -> None:
        """把出站信封发回平台; 单条失败由基类记录日志, 不杀循环."""
        ...


class BaseChannelAdapter(ABC):
    """出站循环 + 入站发布辅助的通用基类."""

    def __init__(self, *, channel: str) -> None:
        if not str(channel or "").strip():
            raise ValueError("adapter.channel 不能为空")
        self.channel = str(channel)
        self._bus: Optional[MessageBus] = None
        self._outbound_task: Optional[asyncio.Task] = None
        self._accepting = False
        self._stopped = False

    def __repr__(self) -> str:
        return f"<{type(self).__name__} channel={self.channel!r} accepting={self._accepting}>"

    # ------------------------------------------------------------------ 子类

    @abstractmethod
    async def send(self, envelope: Envelope) -> None:
        """发送一条出站信封; 实现方需自行处理平台错误(抛异常会被基类记录并丢弃该条)."""

    # ------------------------------------------------------------------ 生命周期

    async def start(self, bus: MessageBus) -> None:
        if self._outbound_task is not None:
            return
        self._bus = bus
        self._stopped = False
        self._accepting = True
        self._outbound_task = asyncio.create_task(
            self._outbound_loop(), name=f"gateway-out-{self.channel}"
        )
        logger.info("渠道适配器已启动: %s", self.channel)

    async def stop_inbound(self) -> None:
        self._accepting = False

    async def stop(self, timeout: float = 5.0) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._accepting = False
        bus, task = self._bus, self._outbound_task
        self._outbound_task = None
        if bus is not None and task is not None:
            try:
                await asyncio.wait_for(bus.join(outbound_queue(self.channel)), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("渠道 [%s] 出站队列排空超时, 丢弃剩余消息", self.channel)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        logger.info("渠道适配器已停止: %s", self.channel)

    async def _outbound_loop(self) -> None:
        assert self._bus is not None
        queue_key = outbound_queue(self.channel)
        while True:
            try:
                envelope = await self._bus.get(queue_key)
            except GatewayClosedError:
                return
            try:
                await self.send(envelope)
            except asyncio.CancelledError:
                raise
            # 单条发送失败不杀循环
            except Exception:  # noqa: BLE001
                logger.exception(
                    "渠道 [%s] 发送失败(单条丢弃): id=%s", self.channel, envelope.message_id
                )
            finally:
                self._bus.task_done(queue_key)

    # ------------------------------------------------------------------ 入站

    async def publish_inbound(self, envelope: Envelope) -> bool:
        """发布入站信封; 未启动/已停入站/总线关闭时返回 False 并告警."""
        if not self._accepting or self._bus is None:
            logger.warning(
                "渠道 [%s] 未启动或已停止入站, 丢弃消息: id=%s",
                self.channel,
                envelope.message_id,
            )
            return False
        try:
            return await self._bus.publish(INBOUND, envelope)
        except GatewayClosedError:
            logger.warning("渠道 [%s] 总线已关闭, 丢弃消息: id=%s", self.channel, envelope.message_id)
            return False


class LoopbackAdapter(BaseChannelAdapter):
    """进程内回环适配器: ``feed()`` 注入入站, ``send()`` 收集出站.

    用于骨架阶段的联调、手工验证与测试; 真实平台适配器后续实现同一契约.
    """

    def __init__(self, channel: str = "loopback") -> None:
        super().__init__(channel=channel)
        self.sent: list[Envelope] = []

    async def send(self, envelope: Envelope) -> None:
        self.sent.append(envelope)

    async def feed(
        self,
        text: str,
        *,
        scope: str = "private",
        conversation_id: str = "loopback",
        sender_id: str = "local",
        metadata: Optional[dict] = None,
    ) -> bool:
        envelope = make_inbound(
            channel=self.channel,
            text=text,
            scope=scope,
            conversation_id=conversation_id,
            sender_id=sender_id,
            metadata=metadata,
        )
        return await self.publish_inbound(envelope)

    async def wait_sent(self, count: int = 1, timeout: float = 5.0) -> Sequence[Envelope]:
        """测试便利: 轮询等待至少 ``count`` 条出站消息."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self.sent) < count and loop.time() < deadline:
            await asyncio.sleep(0.001)
        return list(self.sent)
