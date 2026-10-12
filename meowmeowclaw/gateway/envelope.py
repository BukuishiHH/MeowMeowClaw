"""网关内部统一消息信封(见 docs/GATEWAY_DESIGN.md §3).

- ``Envelope`` 只在网关内部流转; 适配器与平台的边界仍可用
  ``channels.base.IncomingMessage / OutgoingMessage``;
- 不可变对象; ``message_id`` 是幂等键, ``correlation_id`` 只做请求-回复配对.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from meowmeowclaw.memory.models import utc_now_ms

KIND_REQUEST = "request"   # 适配器 -> dispatcher
KIND_REPLY = "reply"       # dispatcher/worker -> 适配器
KIND_COMMAND = "command"   # 预留: 渠道指令内部流转
KIND_PUSH = "push"         # 预留: 主动推送(无入站请求)
KIND_ERROR = "error"       # 预留: 错误回执
KIND_STREAM = "stream"     # 流式控制/数据相位(start/delta/end, 见 docs/STREAMING_DESIGN.md §4.5)

VALID_KINDS = frozenset(
    {KIND_REQUEST, KIND_REPLY, KIND_COMMAND, KIND_PUSH, KIND_ERROR, KIND_STREAM}
)
# 需要 target_channel(或 channel)才能路由出去的 kind
_OUTBOUND_KINDS = frozenset({KIND_REPLY, KIND_PUSH, KIND_ERROR, KIND_STREAM})

# KIND_STREAM 的 metadata["phase"] 取值
STREAM_PHASE_START = "start"   # agent 派发前: 渠道可据此开启"正在输入"等状态
STREAM_PHASE_DELTA = "delta"   # 文本增量(best-effort, 可丢)
STREAM_PHASE_END = "end"       # agent 结束: 渠道据此停止状态提示
STREAM_PHASES = frozenset({STREAM_PHASE_START, STREAM_PHASE_DELTA, STREAM_PHASE_END})


def new_message_id() -> str:
    """生成网关内部消息 ID(平台未提供 message_id 时使用)."""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class Envelope:
    """网关统一信封; 入站/出站共用, 由 ``kind`` 与 ``target_channel`` 区分方向."""

    kind: str
    channel: str
    text: str = ""
    scope: str = "private"
    conversation_id: str = ""
    session_id: Optional[str] = None
    sender_id: str = ""
    message_id: str = ""
    correlation_id: str = ""
    reply_to: Optional[str] = None
    received_at_ms: int = 0
    created_at_ms: int = 0
    target_channel: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    delta_seq: int = 0            # KIND_STREAM delta 相位: 同一 correlation 内单调递增(排障/乱序断言)

    def __post_init__(self) -> None:
        if self.kind not in VALID_KINDS:
            raise ValueError(f"Envelope.kind 不合法: {self.kind!r}")
        if not str(self.channel or "").strip():
            raise ValueError("Envelope.channel 不能为空")
        if not isinstance(self.metadata, dict):
            raise ValueError("Envelope.metadata 必须是 dict")
        if self.kind in _OUTBOUND_KINDS and not (self.target_channel or self.channel):
            raise ValueError(f"{self.kind} 需要 target_channel 或 channel")
        if self.message_id is None or self.correlation_id is None:
            raise ValueError("message_id/correlation_id 不能为 None")
        if not isinstance(self.received_at_ms, int) or not isinstance(self.created_at_ms, int):
            raise ValueError("received_at_ms/created_at_ms 必须是 int")
        if (
            not isinstance(self.delta_seq, int)
            or isinstance(self.delta_seq, bool)
            or self.delta_seq < 0
        ):
            raise ValueError(f"delta_seq 必须是非负整数: {self.delta_seq!r}")


def make_inbound(
    *,
    channel: str,
    text: str,
    scope: str = "private",
    conversation_id: str = "",
    sender_id: str = "",
    message_id: Optional[str] = None,
    reply_to: Optional[str] = None,
    received_at_ms: Optional[int] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> Envelope:
    """构造入站请求信封; 缺 message_id 时生成内部 ID, 缺时间戳时取当前时间."""
    now = utc_now_ms()
    return Envelope(
        kind=KIND_REQUEST,
        channel=channel,
        text=text,
        scope=scope,
        conversation_id=conversation_id,
        sender_id=sender_id,
        message_id=message_id or new_message_id(),
        reply_to=reply_to,
        received_at_ms=now if received_at_ms is None else int(received_at_ms),
        created_at_ms=now,
        metadata=dict(metadata or {}),
    )


def make_stream_event(
    request: Envelope,
    *,
    phase: str,
    text: str = "",
    seq: int = 0,
) -> Envelope:
    """基于入站请求构造流式信封(start/delta/end), 见 docs/STREAMING_DESIGN.md §4.5.

    - 复用入站请求的渠道/会话/发送者/时间, 并建立 correlation;
    - ``phase`` 写入 ``metadata["phase"]``, delta 的增量文本放 ``text``;
    - 不参与入站去重(dedup 只作用于 INBOUND)。
    """
    if phase not in STREAM_PHASES:
        raise ValueError(f"phase 不合法: {phase!r}")
    now = utc_now_ms()
    return Envelope(
        kind=KIND_STREAM,
        channel=request.channel,
        target_channel=request.target_channel or request.channel,
        text=text,
        scope=request.scope,
        conversation_id=request.conversation_id,
        session_id=request.session_id,
        sender_id=request.sender_id,
        message_id=new_message_id(),
        correlation_id=request.message_id,
        reply_to=request.message_id,
        received_at_ms=request.received_at_ms,
        created_at_ms=now,
        delta_seq=int(seq),
        metadata={"source": "stream", "phase": phase},
    )


def make_reply(
    request: Envelope,
    text: str,
    *,
    kind: str = KIND_REPLY,
    session_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> Envelope:
    """基于入站请求构造回程信封: 带回渠道/会话/回复目标, 并建立 correlation."""
    meta = {"source": "agent"}
    if metadata:
        meta.update(metadata)
    return Envelope(
        kind=kind,
        channel=request.channel,
        target_channel=request.target_channel or request.channel,
        text=text,
        scope=request.scope,
        conversation_id=request.conversation_id,
        session_id=session_id if session_id is not None else request.session_id,
        sender_id=request.sender_id,
        correlation_id=request.message_id,
        reply_to=request.message_id,
        received_at_ms=request.received_at_ms,
        created_at_ms=utc_now_ms(),
        metadata=meta,
    )
