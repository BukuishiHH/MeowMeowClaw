"""QQ 私聊渠道: active 指针存储 + 兼容 facade.

W2 起业务策略已抽到 ``channels/qq_policy.py``(QqPolicy), 本模块保留:

- ``QqPrivateActiveStore``/``ActiveSession``: active 指针的 JSONL 持久化(不变);
- ``QqPrivateService``: **兼容 facade**, 保持旧 ``handle_incoming(IncomingMessage)
  -> list[OutgoingMessage]`` 契约, 内部用 QqPolicy + 直连 ConversationService 实现,
  供尚未接入网关的调用方与既有测试使用;
- 常量(``DEFAULT_IDLE_TIMEOUT_MS`` / ``NEW_CONVERSATION_NOTICE`` / ``QQ_HELP_TEXT``)
  从 qq_policy 再导出, 保持旧导入路径可用。

网关路径请使用 ``QqPolicy`` + ``QqAdapter``(见 docs/GATEWAY_DESIGN.md §9)。
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Union

from meowmeowclaw.conversation import ConversationService
from meowmeowclaw.gateway import ACTION_IGNORE, ACTION_REPLY
from meowmeowclaw.memory import MemoryStoreError, SessionKey, SessionSummary, utc_now_ms

from .base import IncomingMessage, OutgoingMessage
from .bridge import incoming_to_envelope
from .qq_policy import (
    DEFAULT_IDLE_TIMEOUT_MS,
    NEW_CONVERSATION_NOTICE,
    QQ_HELP_TEXT,
    QqPolicy,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_IDLE_TIMEOUT_MS",
    "NEW_CONVERSATION_NOTICE",
    "QQ_HELP_TEXT",
    "ActiveSession",
    "QqPrivateActiveStore",
    "QqPrivateService",
    "QqPolicy",
]


@dataclass(frozen=True)
class ActiveSession:
    """某个 QQ 联系人当前活跃的会话指针."""

    session_id: str
    at_ms: int


class QqPrivateActiveStore:
    """QQ 私聊 active 指针的 JSONL 持久化.

    文件: ``<memory_dir>/active/qq_private.jsonl``, 追加事件、按联系人取最新:
    ``activate`` / ``activity`` / ``clear``。
    """

    def __init__(self, memory_dir: Union[str, Path]) -> None:
        self.path = Path(memory_dir).expanduser() / "active" / "qq_private.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._closed = False

    def __repr__(self) -> str:
        return f"<QqPrivateActiveStore path={str(self.path)!r} closed={self._closed}>"

    def _check_open(self) -> None:
        if self._closed:
            raise MemoryStoreError("QqPrivateActiveStore 已关闭")

    def _read_latest(self, contact: SessionKey) -> Optional[ActiveSession]:
        if not self.path.is_file():
            return None
        latest: Optional[ActiveSession] = None
        canonical = contact.canonical
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("跳过损坏的 QQ active 行: %s", self.path)
                        continue
                    if not isinstance(record, dict) or record.get("contact") != canonical:
                        continue
                    event_type = record.get("type")
                    if event_type == "clear":
                        latest = None
                    elif event_type in ("activate", "activity"):
                        session_id = str(record.get("session_id") or "")
                        if session_id:
                            latest = ActiveSession(
                                session_id=session_id,
                                at_ms=int(record.get("at_ms") or 0),
                            )
        except OSError as exc:
            logger.warning("读取 QQ active 指针失败: %s (%r)", self.path, exc)
            return None
        return latest

    async def _append(self, event: dict) -> None:
        async with self._lock:
            def _write() -> None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                    )
                    handle.flush()

            try:
                await asyncio.to_thread(_write)
            except OSError as exc:
                raise MemoryStoreError(f"写入 QQ active 指针失败: {exc!r}") from exc

    async def load(self, contact: SessionKey) -> Optional[ActiveSession]:
        self._check_open()
        async with self._lock:
            return await asyncio.to_thread(self._read_latest, contact)

    async def activate(self, contact: SessionKey, session_id: str, at_ms: int) -> None:
        self._check_open()
        await self._append(
            {
                "type": "activate",
                "contact": contact.canonical,
                "session_id": session_id,
                "at_ms": int(at_ms),
            }
        )

    async def touch(self, contact: SessionKey, session_id: str, at_ms: int) -> None:
        self._check_open()
        await self._append(
            {
                "type": "activity",
                "contact": contact.canonical,
                "session_id": session_id,
                "at_ms": int(at_ms),
            }
        )

    async def clear(self, contact: SessionKey, at_ms: int) -> None:
        self._check_open()
        await self._append(
            {"type": "clear", "contact": contact.canonical, "at_ms": int(at_ms)}
        )

    async def close(self) -> None:
        self._closed = True


class QqPrivateService:
    """QQ 私聊会话管理 + 命令处理的兼容 facade(网关路径请用 QqPolicy + QqAdapter).

    Args:
        conversation: 记忆/Agent 编排层
        active_store: active 指针存储(同一 memory_dir 可跨进程重启恢复)
        owner_id: 允许使用的 QQ 号(单用户); 其他来源消息一律忽略
        idle_timeout_ms: 闲置超时(默认 6 小时)
        clock: 可注入时钟(毫秒), 便于测试
    """

    def __init__(
        self,
        conversation: ConversationService,
        active_store: QqPrivateActiveStore,
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
        self.policy = QqPolicy(
            conversation,
            active_store,
            owner_id,
            idle_timeout_ms=idle_timeout_ms,
            clock=clock,
        )

    def __repr__(self) -> str:
        return (
            f"<QqPrivateService owner={self.owner_id!r} "
            f"timeout_ms={self.idle_timeout_ms}>"
        )

    # ------------------------------------------------------------------ 兼容工具

    def contact_key(self) -> SessionKey:
        """QQ 私聊的逻辑联系人键(不含 session 实例)."""
        return self.policy.contact_key()

    def _is_owner_private(self, message: IncomingMessage) -> bool:
        return self.policy.is_owner_private(incoming_to_envelope(message))

    def _now(self, message: IncomingMessage) -> int:
        return message.received_at_ms if message.received_at_ms is not None else self._clock()

    async def _archive_if_present(self, session: SessionKey) -> None:
        await self.policy._archive_if_present(session)  # noqa: SLF001 - 兼容旧内部调用

    async def _rotate(
        self, contact: SessionKey, now_ms: int, *, archive_old: bool
    ) -> SessionKey:
        return await self.policy._rotate(contact, now_ms, archive_old=archive_old)  # noqa: SLF001

    async def _resolve_active_session(
        self, contact: SessionKey, now_ms: int
    ) -> tuple[SessionKey, list[OutgoingMessage]]:
        session, notices = await self.policy._resolve_active_session(now_ms)  # noqa: SLF001
        return session, [OutgoingMessage(text) for text in notices]

    async def _resolve_summary(
        self, token: str
    ) -> tuple[Optional[SessionSummary], Optional[str]]:
        return await self.policy.resolve_summary(token)

    @staticmethod
    def _format_sessions(sessions: list[SessionSummary], active: Optional[ActiveSession]) -> str:
        return QqPolicy.format_sessions(sessions, active)

    # ------------------------------------------------------------------ 入口

    async def handle_incoming(self, message: IncomingMessage) -> list[OutgoingMessage]:
        """
        处理一条 QQ 私聊消息, 返回需要发送的回复列表.

        - 非本人 / 非私聊 / 空文本: 返回空列表(忽略);
        - 空闲超时: 先返回 "已开始新对话", 再返回本轮回答。
        """
        decision = await self.policy.resolve(incoming_to_envelope(message))
        if decision.action == ACTION_IGNORE:
            return []
        if decision.action == ACTION_REPLY:
            return [OutgoingMessage(envelope.text) for envelope in decision.replies]

        replies = [OutgoingMessage(envelope.text) for envelope in decision.pre_replies]
        session_key = decision.session_key
        if session_key is None:  # pragma: no cover - PolicyDecision 已保证
            raise ValueError("action=agent 必须携带 session_key")
        result = await self.conversation.handle_message(
            session_key,
            decision.text,
            meta=decision.meta,
        )
        replies.append(OutgoingMessage(result.answer, reply_to=message.message_id))
        return replies
