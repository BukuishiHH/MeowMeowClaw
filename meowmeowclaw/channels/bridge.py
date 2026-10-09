"""平台 DTO 与网关 Envelope 的互转(见 docs/GATEWAY_DESIGN.md §5).

``channels.base`` 的 ``IncomingMessage/OutgoingMessage`` 是适配器与平台的稳定边界;
网关内部统一使用 ``Envelope``。转换只做字段映射, 不带业务语义。
"""

from typing import Optional

from meowmeowclaw.gateway.envelope import Envelope, make_inbound

from .base import IncomingMessage, OutgoingMessage


def incoming_to_envelope(message: IncomingMessage) -> Envelope:
    """入站: 平台标准消息 -> 网关信封(保留 message_id / 平台时间戳)."""
    return make_inbound(
        channel=message.channel,
        text=message.text or "",
        scope=message.scope,
        conversation_id=message.conversation_id,
        sender_id=message.sender_id,
        message_id=message.message_id,
        received_at_ms=message.received_at_ms,
    )


def outgoing_from_envelope(envelope: Envelope, *, reply_to: Optional[str] = None) -> OutgoingMessage:
    """出站: 网关信封 -> 平台标准消息; ``reply_to`` 由调用方按平台语义决定."""
    return OutgoingMessage(text=envelope.text, reply_to=reply_to)
