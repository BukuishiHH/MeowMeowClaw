"""渠道策略契约(见 docs/GATEWAY_DESIGN.md §6).

策略层负责: 身份过滤、触发规则、会话轮换、指令、群聊权限;
它产出 ``PolicyDecision``, 由 dispatcher 决定直发回复还是交给 Agent.
"""

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from meowmeowclaw.memory import SessionKey

from .envelope import Envelope

ACTION_IGNORE = "ignore"   # 丢弃(非目标用户/空消息/触发规则未命中)
ACTION_REPLY = "reply"     # 策略直接回复(指令/权限提示), 不进 Agent
ACTION_AGENT = "agent"     # 交给 AgentWorker 处理

VALID_ACTIONS = frozenset({ACTION_IGNORE, ACTION_REPLY, ACTION_AGENT})


@dataclass(frozen=True)
class PolicyDecision:
    """渠道策略裁决; 三种 action 的字段约束由 ``__post_init__`` 校验."""

    action: str
    replies: tuple[Envelope, ...] = ()
    pre_replies: tuple[Envelope, ...] = ()
    session_key: Optional[SessionKey] = None
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.action not in VALID_ACTIONS:
            raise ValueError(f"PolicyDecision.action 不合法: {self.action!r}")
        if self.action == ACTION_AGENT and self.session_key is None:
            raise ValueError("action=agent 必须携带 session_key")
        if self.action == ACTION_REPLY and not self.replies:
            raise ValueError("action=reply 至少需要一条回复信封")
        if not isinstance(self.meta, dict):
            raise ValueError("PolicyDecision.meta 必须是 dict")

    @classmethod
    def ignore(cls) -> "PolicyDecision":
        return cls(action=ACTION_IGNORE)

    @classmethod
    def reply(cls, *envelopes: Envelope) -> "PolicyDecision":
        return cls(action=ACTION_REPLY, replies=tuple(envelopes))

    @classmethod
    def agent(
        cls,
        session_key: SessionKey,
        text: str,
        *,
        meta: Optional[dict[str, Any]] = None,
        pre_replies: tuple[Envelope, ...] = (),
    ) -> "PolicyDecision":
        return cls(
            action=ACTION_AGENT,
            session_key=session_key,
            text=text,
            meta=dict(meta or {}),
            pre_replies=tuple(pre_replies),
        )


@runtime_checkable
class ChannelPolicy(Protocol):
    """每个渠道一个策略实例; 由 Gateway 按 ``envelope.channel`` 路由."""

    channel: str

    async def resolve(self, envelope: Envelope) -> PolicyDecision:
        """把标准信封裁决为 ignore / reply / agent."""
        ...
