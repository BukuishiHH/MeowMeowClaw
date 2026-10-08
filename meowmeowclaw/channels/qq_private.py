"""QQ 私聊渠道服务: active 指针 + 闲置 6 小时惰性轮换 + 最小指令集.

本模块只处理"标准消息 -> 标准回复"的业务逻辑, 不包含具体协议传输。
未来的 OneBot/NapCat 等适配器只需::

    incoming = IncomingMessage(channel="qq", scope="private", conversation_id=uin,
                               sender_id=uin, text=payload, received_at_ms=ts)
    for reply in await service.handle_incoming(incoming):
        await transport.send_text(uin, reply.text)

设计约束(见 docs/MEMORY_DESIGN.md):
- 仅私聊、仅本人可用;
- 最后活动时间以"用户消息到达时间"为准, 机器人回复不刷新;
- 超时/会话文件缺失 -> 归档旧会话, 新建 session 实例, 回复"已开始新对话";
- 所有会话不自动清理, 仅通过 /clear 手动归档/删除。
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Union

from meowmeowclaw.channels.base import IncomingMessage, OutgoingMessage
from meowmeowclaw.conversation import ConversationService
from meowmeowclaw.memory import (
    MemoryStoreError,
    SessionKey,
    SessionSummary,
    utc_now_ms,
)

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
    """QQ 私聊会话管理 + 命令处理.

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

    def __repr__(self) -> str:
        return (
            f"<QqPrivateService owner={self.owner_id!r} "
            f"timeout_ms={self.idle_timeout_ms}>"
        )

    # ------------------------------------------------------------------ 工具

    def contact_key(self) -> SessionKey:
        """QQ 私聊的逻辑联系人键(不含 session 实例)."""
        return SessionKey(channel="qq", scope="private", conversation_id=self.owner_id)

    def _is_owner_private(self, message: IncomingMessage) -> bool:
        return (
            message.channel == "qq"
            and message.scope == "private"
            and str(message.conversation_id) == self.owner_id
            and str(message.sender_id) == self.owner_id
        )

    def _now(self, message: IncomingMessage) -> int:
        return message.received_at_ms if message.received_at_ms is not None else self._clock()

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
        self, contact: SessionKey, now_ms: int
    ) -> tuple[SessionKey, list[OutgoingMessage]]:
        """
        解析当前活跃会话; 必要时惰性轮换.

        :return: (会话键, 需要先发出的提示消息)
        """
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
        return new_session, [OutgoingMessage(NEW_CONVERSATION_NOTICE)]

    async def _resolve_summary(
        self, token: str
    ) -> tuple[Optional[SessionSummary], Optional[str]]:
        """按短 ID 前缀解析会话; 返回 (summary, 错误文本)."""
        sessions = await self.conversation.list_sessions(include_archived=True)
        matches = [item for item in sessions if item.storage_id.startswith(token)]
        if not matches:
            return None, f"未找到会话: {token}. 可用 /sessions 查看."
        if len(matches) > 1:
            candidates = ", ".join(f"{item.short_id}({item.channel})" for item in matches)
            return None, f"会话 ID 前缀不唯一: {token}. 候选: {candidates}"
        return matches[0], None

    @staticmethod
    def _format_sessions(sessions: list[SessionSummary], active: Optional[ActiveSession]) -> str:
        if not sessions:
            return "暂无会话."
        lines = ["会话列表:"]
        for summary in sessions:
            status = "archived" if summary.archived else "active"
            marker = "  <- 当前" if active is not None and summary.session_key.session_id == active.session_id else ""
            lines.append(
                f"- {summary.short_id} [{status}] {summary.channel} "
                f"轮数={summary.turn_count} 最后活动={summary.updated_at_iso}{marker}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ 命令

    async def _handle_command(self, text: str, now_ms: int) -> list[OutgoingMessage]:
        parts = text.split()
        name = parts[0].lower()
        args = parts[1:]
        contact = self.contact_key()

        if name == "/help":
            return [OutgoingMessage(QQ_HELP_TEXT)]

        if name == "/new":
            if args:
                return [OutgoingMessage("用法: /new")]
            await self._rotate(contact, now_ms, archive_old=False)
            return [OutgoingMessage(NEW_CONVERSATION_NOTICE)]

        if name == "/sessions":
            include_archived = "--active" not in args
            sessions = await self.conversation.list_sessions(include_archived=include_archived)
            if "--active" in args:
                sessions = [item for item in sessions if not item.archived]
            if "--archived" in args:
                sessions = [item for item in sessions if item.archived]
            active = await self.active_store.load(contact)
            return [OutgoingMessage(self._format_sessions(sessions, active))]

        if name == "/clear":
            purge = "--purge" in args
            tokens = [item for item in args if item != "--purge"]
            if len(tokens) > 1:
                return [OutgoingMessage("用法: /clear [会话ID] [--purge]")]

            active = await self.active_store.load(contact)

            # /clear: 归档当前会话并新建; 没有任何会话时直接新建
            if not tokens:
                if active is None:
                    await self._rotate(contact, now_ms, archive_old=False)
                    return [OutgoingMessage(NEW_CONVERSATION_NOTICE)]
                target_key = contact.with_session(active.session_id)
                await self._archive_if_present(target_key)
                await self._rotate(contact, now_ms, archive_old=False)
                return [OutgoingMessage(f"已归档当前会话\n{NEW_CONVERSATION_NOTICE}")]

            target, error = await self._resolve_summary(tokens[0])
            if target is None:
                return [OutgoingMessage(error or "会话不存在")]

            label = f"{target.short_id}({target.channel})"
            if purge:
                await self.conversation.purge_session(target.session_key)
                action = f"已永久删除会话: {label}"
            else:
                if target.archived:
                    action = f"会话已处于归档状态: {label}"
                else:
                    await self.conversation.archive_session(target.session_key)
                    action = f"已归档会话: {label}"

            is_current = (
                active is not None
                and target.session_key.session_id == active.session_id
                and target.session_key.channel == contact.channel
                and target.session_key.conversation_id == contact.conversation_id
            )
            if is_current:
                await self._rotate(contact, now_ms, archive_old=False)
                return [OutgoingMessage(f"{action}\n{NEW_CONVERSATION_NOTICE}")]
            return [OutgoingMessage(action)]

        return [OutgoingMessage(f"未知命令: {name}. 输入 /help 查看用法.")]

    # ------------------------------------------------------------------ 入口

    async def handle_incoming(self, message: IncomingMessage) -> list[OutgoingMessage]:
        """
        处理一条 QQ 私聊消息, 返回需要发送的回复列表.

        - 非本人 / 非私聊 / 空文本: 返回空列表(忽略);
        - 空闲超时: 先返回 "已开始新对话", 再返回本轮回答。
        """
        if not self._is_owner_private(message):
            return []
        text = (message.text or "").strip()
        if not text:
            return []

        now_ms = self._now(message)
        if text.startswith("/"):
            return await self._handle_command(text, now_ms)

        session, notices = await self._resolve_active_session(self.contact_key(), now_ms)
        result = await self.conversation.handle_message(
            session,
            text,
            meta={"channel": "qq", "scope": "private", "sender_id": self.owner_id},
        )
        await self.active_store.touch(self.contact_key(), session.session_id, now_ms)

        replies = list(notices)
        replies.append(OutgoingMessage(result.answer, reply_to=message.message_id))
        return replies
