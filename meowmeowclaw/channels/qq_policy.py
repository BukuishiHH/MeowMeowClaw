"""QQ 私聊渠道策略(见 docs/GATEWAY_DESIGN.md §6).

从原 ``QqPrivateService`` 抽出的纯策略层: 身份过滤、active 指针、6h 惰性轮换、最小指令集。
本模块只产出 ``PolicyDecision``, 不直接调用 Agent/Provider; ``QqPrivateService`` 保留为兼容 facade。
"""

import logging
import uuid
from typing import Any, Callable, Optional

from meowmeowclaw.gateway import (
    Envelope,
    PolicyDecision,
    make_reply,
)
from meowmeowclaw.memory import MemoryStoreError, SessionKey, SessionSummary, utc_now_ms

from .commands import SessionCommandHelper

logger = logging.getLogger(__name__)

DEFAULT_IDLE_TIMEOUT_MS = 6 * 60 * 60 * 1000
NEW_CONVERSATION_NOTICE = "已开始新对话"

QQ_HELP_TEXT = """可用命令:
  /help                       查看本帮助
  /new                        新建对话(旧对话保留)
  /clear                      归档当前对话并新建
  /clear <会话ID>             归档指定会话
  /clear <会话ID> --purge     永久删除指定会话
  /sessions [--active|--archived]  查看会话列表"""


class QqPolicy:
    """QQ 私聊策略; 每个 owner 一个实例, 由 Gateway 按 channel 路由."""

    channel = "qq"

    def __init__(
        self,
        conversation: Any,
        active_store: Any,
        owner_id: str,
        *,
        idle_timeout_ms: int = DEFAULT_IDLE_TIMEOUT_MS,
        clock: Callable[[], int] = utc_now_ms,
    ) -> None:
        self.conversation = conversation
        self.active_store = active_store
        self.owner_id = str(owner_id)
        self.idle_timeout_ms = int(idle_timeout_ms)
        self._clock = clock
        self._commands = SessionCommandHelper(conversation)

    def __repr__(self) -> str:
        return (
            f"<QqPolicy owner={self.owner_id!r} timeout_ms={self.idle_timeout_ms}>"
        )

    # ------------------------------------------------------------------ 工具

    def contact_key(self) -> SessionKey:
        """QQ 私聊的逻辑联系人键(不含 session 实例)."""
        return SessionKey(channel="qq", scope="private", conversation_id=self.owner_id)

    def is_owner_private(self, envelope: Envelope) -> bool:
        """仅本人私聊消息有效; 群聊/他人/其他渠道一律忽略."""
        return (
            envelope.channel == "qq"
            and envelope.scope == "private"
            and str(envelope.conversation_id) == self.owner_id
            and str(envelope.sender_id) == self.owner_id
        )

    def _now(self, envelope: Envelope) -> int:
        return envelope.received_at_ms or self._clock()

    async def resolve_summary(
        self, token: str
    ) -> tuple[Optional[SessionSummary], Optional[str]]:
        return await self._commands.resolve_summary(token)

    @staticmethod
    def format_sessions(
        sessions: list[SessionSummary], active: Optional[Any] = None
    ) -> str:
        return SessionCommandHelper.format_sessions(
            sessions,
            active_session_id=active.session_id if active is not None else None,
        )

    # ------------------------------------------------------------------ 会话

    async def _archive_if_present(self, session: SessionKey) -> None:
        try:
            meta = await self.conversation.get_meta(session)
            if meta is not None and not meta.archived:
                await self.conversation.archive_session(session)
        except MemoryStoreError as exc:
            logger.warning("归档旧 QQ 会话失败(fail-soft): %s (%r)", session.canonical, exc)

    async def _rotate(
        self, contact: SessionKey, now_ms: int, *, archive_old: bool
    ) -> SessionKey:
        """归档旧会话(可选)并创建/激活新会话实例."""
        active = await self.active_store.load(contact)
        if active is not None and archive_old:
            await self._archive_if_present(contact.with_session(active.session_id))
        new_session = contact.with_session(uuid.uuid4().hex)
        await self.active_store.activate(contact, new_session.session_id, now_ms)
        return new_session

    async def _resolve_active_session(
        self, now_ms: int
    ) -> tuple[SessionKey, list[str]]:
        """
        解析当前活跃会话; 必要时惰性轮换.

        :return: (会话键, 需要先发出的提示文本列表)
        """
        contact = self.contact_key()
        active = await self.active_store.load(contact)
        if active is None:
            new_session = await self._rotate(contact, now_ms, archive_old=False)
            return new_session, []

        session = contact.with_session(active.session_id)
        expired = now_ms - active.at_ms > self.idle_timeout_ms
        try:
            meta = await self.conversation.get_meta(session)
        except MemoryStoreError as exc:
            # 读取失败时沿用旧会话(handle_message 内部会 fail-soft), 不轮换以免丢上下文
            logger.warning("读取 QQ 会话元数据失败, 本次沿用旧会话: %r", exc)
            return session, []

        if not expired and meta is not None and not meta.archived:
            return session, []

        new_session = await self._rotate(contact, now_ms, archive_old=True)
        return new_session, [NEW_CONVERSATION_NOTICE]

    # ------------------------------------------------------------------ 命令

    async def _handle_command(
        self, request: Envelope, text: str, now_ms: int
    ) -> list[Envelope]:
        parts = text.split()
        name = parts[0].lower()
        args = parts[1:]
        contact = self.contact_key()

        if name == "/help":
            return [make_reply(request, QQ_HELP_TEXT)]

        if name == "/new":
            if args:
                return [make_reply(request, "用法: /new")]
            await self._rotate(contact, now_ms, archive_old=False)
            return [make_reply(request, NEW_CONVERSATION_NOTICE)]

        if name == "/sessions":
            include_archived = "--active" not in args
            sessions = await self.conversation.list_sessions(include_archived=include_archived)
            if "--active" in args:
                sessions = [item for item in sessions if not item.archived]
            if "--archived" in args:
                sessions = [item for item in sessions if item.archived]
            active = await self.active_store.load(contact)
            return [make_reply(request, self.format_sessions(sessions, active))]

        if name == "/clear":
            purge = "--purge" in args
            tokens = [item for item in args if item != "--purge"]
            if len(tokens) > 1:
                return [make_reply(request, "用法: /clear [会话ID] [--purge]")]

            active = await self.active_store.load(contact)

            # /clear: 归档当前会话并新建; 没有任何会话时直接新建
            if not tokens:
                if active is None:
                    await self._rotate(contact, now_ms, archive_old=False)
                    return [make_reply(request, NEW_CONVERSATION_NOTICE)]
                target_key = contact.with_session(active.session_id)
                await self._archive_if_present(target_key)
                await self._rotate(contact, now_ms, archive_old=False)
                return [make_reply(request, f"已归档当前会话\n{NEW_CONVERSATION_NOTICE}")]

            target, error = await self.resolve_summary(tokens[0])
            if target is None:
                return [make_reply(request, error or "会话不存在")]

            action = await self._commands.archive_or_purge(target, purge=purge)

            is_current = (
                active is not None
                and target.session_key.session_id == active.session_id
                and target.session_key.channel == contact.channel
                and target.session_key.conversation_id == contact.conversation_id
            )
            if is_current:
                await self._rotate(contact, now_ms, archive_old=False)
                return [make_reply(request, f"{action}\n{NEW_CONVERSATION_NOTICE}")]
            return [make_reply(request, action)]

        return [make_reply(request, f"未知命令: {name}. 输入 /help 查看用法.")]

    # ------------------------------------------------------------------ 入口

    async def resolve(self, envelope: Envelope) -> PolicyDecision:
        """渠道策略入口: ignore / reply(指令) / agent(交给 AgentWorker)."""
        if not self.is_owner_private(envelope):
            return PolicyDecision.ignore()
        text = (envelope.text or "").strip()
        if not text:
            return PolicyDecision.ignore()

        now_ms = self._now(envelope)
        if text.startswith("/"):
            replies = await self._handle_command(envelope, text, now_ms)
            if not replies:
                return PolicyDecision.ignore()
            return PolicyDecision.reply(*replies)

        session, notices = await self._resolve_active_session(now_ms)
        # activity 锚点 = 用户消息到达时间(机器人回复不刷新)
        await self.active_store.touch(self.contact_key(), session.session_id, now_ms)
        pre_replies = tuple(make_reply(envelope, notice) for notice in notices)
        return PolicyDecision.agent(
            session,
            text,
            meta={"channel": "qq", "scope": "private", "sender_id": self.owner_id},
            pre_replies=pre_replies,
        )
