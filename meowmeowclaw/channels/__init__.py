"""渠道适配层: 平台无关消息类型与各渠道业务服务."""

from .base import IncomingMessage, OutgoingMessage

__all__ = [
    "IncomingMessage",
    "OutgoingMessage",
]
