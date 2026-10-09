"""单进程 asyncio 消息总线多渠道网关(设计见 docs/GATEWAY_DESIGN.md).

对外只导出稳定契约; 具体实现分散在各子模块, 避免包级 import 产生副作用.
"""

from .adapter import BaseChannelAdapter, ChannelAdapter, LoopbackAdapter
from .bus import (
    DEADLETTER,
    INBOUND,
    AsyncioQueueBus,
    GatewayClosedError,
    MessageBus,
    outbound_queue,
)
from .dedup import DedupCache
from .dispatcher import GatewayDispatcher
from .envelope import (
    KIND_ERROR,
    KIND_PUSH,
    KIND_REPLY,
    KIND_REQUEST,
    Envelope,
    make_inbound,
    make_reply,
    new_message_id,
)
from .gateway import Gateway
from .policy import (
    ACTION_AGENT,
    ACTION_IGNORE,
    ACTION_REPLY,
    ChannelPolicy,
    PolicyDecision,
)
from .worker import AgentWorker

__all__ = [
    "ACTION_AGENT",
    "ACTION_IGNORE",
    "ACTION_REPLY",
    "AgentWorker",
    "AsyncioQueueBus",
    "BaseChannelAdapter",
    "ChannelAdapter",
    "ChannelPolicy",
    "DEADLETTER",
    "DedupCache",
    "Envelope",
    "Gateway",
    "GatewayClosedError",
    "GatewayDispatcher",
    "INBOUND",
    "KIND_ERROR",
    "KIND_PUSH",
    "KIND_REPLY",
    "KIND_REQUEST",
    "LoopbackAdapter",
    "MessageBus",
    "PolicyDecision",
    "make_inbound",
    "make_reply",
    "new_message_id",
    "outbound_queue",
]
