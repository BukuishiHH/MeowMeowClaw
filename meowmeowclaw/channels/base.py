"""渠道适配层的通用消息类型.

传输层(OneBot/NapCat/飞书 SDK 等)负责把平台事件转换成 ``IncomingMessage``,
再把 ``OutgoingMessage`` 发回平台; 渠道业务层只处理标准类型, 为将来群聊/多渠道预留。
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class IncomingMessage:
    """平台无关的入站消息."""

    channel: str                 # qq / feishu / cli ...
    scope: str                   # private / group / session ...
    conversation_id: str         # 平台会话标识(QQ 私聊为 uin, 群聊为 group_id)
    sender_id: str               # 发送者标识(QQ 为 uin)
    text: str                    # 文本内容(v1 仅支持文本)
    message_id: Optional[str] = None      # 平台消息 ID, 便于去重/回复
    received_at_ms: Optional[int] = None  # 平台时间戳; None 时由渠道服务取当前时间


@dataclass(frozen=True)
class OutgoingMessage:
    """平台无关的出站消息(v1 仅文本)."""

    text: str
    reply_to: Optional[str] = None  # 可选: 回复某条消息的平台 ID
