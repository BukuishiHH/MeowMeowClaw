"""QQ 渠道适配器: 平台传输未接入前的占位实现.

真实 OneBot/NapCat 适配器后续只需注入 ``transport``(或继承覆写 ``send``):
- 入站: 平台事件 -> ``IncomingMessage`` -> ``feed()``;
- 出站: ``send(envelope)`` 交给 transport; 未注入时收集到 ``sent`` 供测试/手工验证.
"""

import asyncio
from typing import Awaitable, Callable, Optional, Sequence

from meowmeowclaw.gateway import BaseChannelAdapter, Envelope

from .base import IncomingMessage
from .bridge import incoming_to_envelope

Transport = Callable[[Envelope], Awaitable[None]]


class QqAdapter(BaseChannelAdapter):
    """QQ 私聊适配器(Fake 传输版)."""

    def __init__(
        self,
        *,
        channel: str = "qq",
        transport: Optional[Transport] = None,
    ) -> None:
        super().__init__(channel=channel)
        self.transport = transport
        self.sent: list[Envelope] = []

    async def send(self, envelope: Envelope) -> None:
        self.sent.append(envelope)
        if self.transport is not None:
            await self.transport(envelope)

    async def feed(self, message: IncomingMessage) -> bool:
        """平台消息 -> 信封 -> inbound 队列."""
        return await self.publish_inbound(incoming_to_envelope(message))

    async def wait_sent(self, count: int = 1, timeout: float = 5.0) -> Sequence[Envelope]:
        """测试便利: 轮询等待至少 ``count`` 条出站消息."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self.sent) < count and loop.time() < deadline:
            await asyncio.sleep(0.001)
        return list(self.sent)
