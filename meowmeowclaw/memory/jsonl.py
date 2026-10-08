"""JSONL 版 SessionStore 实现.

存储布局::

    <root>/
    ├── sessions/<storage_id>.jsonl     # 活跃会话: 首行 header + 每行一个 turn
    └── archive/<storage_id>.jsonl      # 归档会话(结构相同)

实现要点:
- 一轮一次 ``write()`` 调用, 保证 turn 原子落盘;
- ``message_total`` 冗余记录累计消息数, 读元数据时无需全量扫描;
- 同一实例内按 storage_id 加 ``asyncio.Lock``; 跨进程文件锁留待后续里程碑;
- 格式损坏的行读取时跳过并告警, 不影响其余数据。
"""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from .errors import InvalidSessionKeyError, SessionStoreError
from .filelock import async_file_lock
from .models import (
    DEFAULT_MAX_TOOL_RESULT_CHARS,
    MIN_SHORT_ID_LENGTH,
    TOOL_RESULT_TRUNCATE_NOTICE,
    SessionKey,
    SessionMessage,
    SessionMeta,
    SessionSummary,
    ms_to_iso,
    utc_now_ms,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
HEADER_TYPE = "header"
TURN_TYPE = "turn"


@dataclass
class _FileState:
    header: dict[str, Any]
    last_turn: Optional[dict[str, Any]]
    turn_count: int
    message_count: int


@dataclass
class _SessionEntry:
    storage_id: str
    key: SessionKey
    created_at_ms: int
    updated_at_ms: int
    turn_count: int
    message_count: int
    archived: bool


class JsonlSessionStore:
    """基于 JSONL 文件的 SessionStore 实现(与 ``SessionStore`` 协议结构兼容)."""

    def __init__(
        self,
        root: Union[str, Path],
        *,
        max_tool_result_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS,
        fsync: bool = False,
        short_id_min_length: int = MIN_SHORT_ID_LENGTH,
        lock_timeout: float = 5.0,
    ) -> None:
        self.root = Path(root).expanduser()
        self.sessions_dir = self.root / "sessions"
        self.archive_dir = self.root / "archive"
        self.max_tool_result_chars = int(max_tool_result_chars)
        self.fsync = bool(fsync)
        self.short_id_min_length = max(4, int(short_id_min_length))
        self.lock_timeout = float(lock_timeout)
        self._locks: dict[str, asyncio.Lock] = {}
        self._closed = False
        self._ensure_dirs()

    def __repr__(self) -> str:
        return f"<JsonlSessionStore root={str(self.root)!r} closed={self._closed}>"

    # ------------------------------------------------------------------ 基础设施

    def _ensure_dirs(self) -> None:
        try:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            self.archive_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SessionStoreError(f"创建记忆目录失败: {self.root} ({exc!r})") from exc

    def _check_open(self) -> None:
        if self._closed:
            raise SessionStoreError("SessionStore 已关闭")

    def _lock_for(self, storage_id: str) -> asyncio.Lock:
        lock = self._locks.get(storage_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[storage_id] = lock
        return lock

    def _lock_path(self, storage_id: str) -> Path:
        return self.sessions_dir / f"{storage_id}.jsonl.lock"

    @asynccontextmanager
    async def _session_guard(self, storage_id: str):
        """会话级写保护: 进程内 asyncio.Lock + 跨进程文件锁."""
        async with self._lock_for(storage_id):
            async with async_file_lock(
                self._lock_path(storage_id), timeout=self.lock_timeout
            ):
                yield

    def _active_path(self, key: SessionKey) -> Path:
        return self.sessions_dir / f"{key.storage_id}.jsonl"

    def _archive_path(self, key: SessionKey) -> Path:
        return self.archive_dir / f"{key.storage_id}.jsonl"

    @staticmethod
    def _dumps(record: dict[str, Any]) -> str:
        return json.dumps(record, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _loads(line: str) -> Optional[dict[str, Any]]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return None
        return record if isinstance(record, dict) else None

    @staticmethod
    def _normalize_message(message: Any) -> SessionMessage:
        if isinstance(message, SessionMessage):
            return message
        if isinstance(message, dict):
            return SessionMessage.from_llm_message(message)
        raise SessionStoreError("消息必须是 SessionMessage 或 OpenAI 消息 dict")

    # ------------------------------------------------------------------ 读取

    @classmethod
    def _read_file(cls, path: Path) -> Optional[_FileState]:
        """读取文件头与末轮统计; 文件不存在/无 header 返回 None."""
        if not path.is_file():
            return None
        header: Optional[dict[str, Any]] = None
        last_turn: Optional[dict[str, Any]] = None
        turn_count = 0
        message_count = 0
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    record = cls._loads(line)
                    if record is None:
                        logger.warning("跳过损坏的 JSONL 行: %s", path)
                        continue
                    if record.get("type") == HEADER_TYPE:
                        header = record
                    elif record.get("type") == TURN_TYPE:
                        last_turn = record
                        turn_count += 1
                        messages = record.get("messages") or []
                        message_count = int(
                            record.get("message_total", message_count + len(messages))
                        )
        except OSError as exc:
            logger.warning("读取会话文件失败: %s (%r)", path, exc)
            return None
        if header is None:
            return None
        return _FileState(
            header=header,
            last_turn=last_turn,
            turn_count=turn_count,
            message_count=message_count,
        )

    @classmethod
    def _read_turns(cls, path: Path) -> list[dict[str, Any]]:
        """读取全部 turn 记录; 损坏行跳过."""
        turns: list[dict[str, Any]] = []
        if not path.is_file():
            return turns
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    record = cls._loads(line)
                    if record is None:
                        logger.warning("跳过损坏的 JSONL 行: %s", path)
                        continue
                    if record.get("type") == TURN_TYPE:
                        turns.append(record)
        except OSError as exc:
            logger.warning("读取会话文件失败: %s (%r)", path, exc)
        return turns

    @staticmethod
    def _key_from_header(header: dict[str, Any], fallback: SessionKey) -> SessionKey:
        canonical = header.get("session_key")
        if isinstance(canonical, str):
            try:
                return SessionKey.from_canonical(canonical)
            except InvalidSessionKeyError:
                logger.warning("header 中的 session_key 非法, 使用请求键回退: %r", canonical)
        return fallback

    # ------------------------------------------------------------------ 写入

    def _append_records(
        self, path: Path, header: Optional[dict[str, Any]], turn: dict[str, Any]
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists()
        needs_newline = False
        if not is_new:
            # 崩溃可能留下没有换行的半行; 追加前先补一个换行, 避免与新记录粘连
            with open(path, "rb") as probe:
                probe.seek(-1, os.SEEK_END)
                needs_newline = probe.read(1) != b"\n"
        with open(path, "a", encoding="utf-8") as handle:
            if is_new and header is not None:
                handle.write(self._dumps(header) + "\n")
            elif needs_newline:
                handle.write("\n")
            handle.write(self._dumps(turn) + "\n")
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())

    @staticmethod
    def _header_record(key: SessionKey, created_at_ms: int) -> dict[str, Any]:
        return {
            "type": HEADER_TYPE,
            "schema_version": SCHEMA_VERSION,
            "storage_id": key.storage_id,
            "session_key": key.canonical,
            "channel": key.channel,
            "scope": key.scope,
            "conversation_id": key.conversation_id,
            "session_id": key.session_id,
            "user_id": key.user_id,
            "created_at_ms": created_at_ms,
            "created_at_iso": ms_to_iso(created_at_ms),
        }

    # ------------------------------------------------------------------ 对外接口

    async def append_turn(
        self,
        key: SessionKey,
        messages: Sequence[SessionMessage],
        *,
        turn_id: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> SessionMeta:
        self._check_open()
        if not isinstance(key, SessionKey):
            raise SessionStoreError("key 必须是 SessionKey")
        normalized = [self._normalize_message(message) for message in messages]
        if not normalized:
            raise SessionStoreError("append_turn 至少需要一条消息")

        async with self._session_guard(key.storage_id):
            path = self._active_path(key)
            state = await asyncio.to_thread(self._read_file, path)
            now = utc_now_ms()
            created_at_ms = int(state.header.get("created_at_ms", now)) if state else now
            previous_seq = int(state.last_turn.get("seq", 0)) if state and state.last_turn else 0
            previous_total = state.message_count if state else 0

            stored_messages: list[dict[str, Any]] = []
            truncated = 0
            for message in normalized:
                content = message.content
                if (
                    message.role == "tool"
                    and content is not None
                    and len(content) > self.max_tool_result_chars
                ):
                    content = content[: self.max_tool_result_chars] + TOOL_RESULT_TRUNCATE_NOTICE
                    truncated += 1
                stamped = replace(
                    message,
                    content=content,
                    ts_ms=message.ts_ms if message.ts_ms is not None else now,
                )
                stored_messages.append(stamped.to_storage_dict())

            turn_meta = dict(meta) if meta else {}
            if truncated:
                turn_meta["truncated_tool_results"] = truncated

            turn_record: dict[str, Any] = {
                "type": TURN_TYPE,
                "schema_version": SCHEMA_VERSION,
                "seq": previous_seq + 1,
                "turn_id": turn_id or uuid.uuid4().hex,
                "ts_ms": now,
                "ts_iso": ms_to_iso(now),
                "message_total": previous_total + len(stored_messages),
                "messages": stored_messages,
            }
            if turn_meta:
                turn_record["meta"] = turn_meta

            header = None if state else self._header_record(key, created_at_ms)
            try:
                await asyncio.to_thread(self._append_records, path, header, turn_record)
            except (OSError, TypeError, ValueError) as exc:
                raise SessionStoreError(f"写入会话失败: {path} ({exc!r})") from exc

            return SessionMeta(
                storage_id=key.storage_id,
                session_key=key,
                created_at_ms=created_at_ms,
                updated_at_ms=now,
                turn_count=previous_seq + 1,
                message_count=previous_total + len(stored_messages),
            )

    async def load_recent(
        self,
        key: SessionKey,
        *,
        max_turns: Optional[int] = None,
        max_chars: Optional[int] = None,
    ) -> list[SessionMessage]:
        self._check_open()
        if not isinstance(key, SessionKey):
            raise SessionStoreError("key 必须是 SessionKey")
        if max_turns is not None and max_turns < 0:
            raise SessionStoreError("max_turns 不能为负数")
        if max_chars is not None and max_chars < 0:
            raise SessionStoreError("max_chars 不能为负数")

        turns = await asyncio.to_thread(self._read_turns, self._active_path(key))
        if max_turns is not None:
            turns = turns[-max_turns:] if max_turns > 0 else []
        if max_chars is not None:
            turns = self._trim_by_chars(turns, max_chars)

        messages: list[SessionMessage] = []
        for turn in turns:
            for raw in turn.get("messages") or []:
                try:
                    messages.append(SessionMessage.from_storage_dict(raw))
                except ValueError as exc:
                    logger.warning("跳过损坏的消息记录: %r", exc)
        return messages

    @staticmethod
    def _turn_char_size(turn: dict[str, Any]) -> int:
        size = 0
        for message in turn.get("messages") or []:
            content = message.get("content")
            if isinstance(content, str):
                size += len(content)
            tool_calls = message.get("tool_calls")
            if tool_calls:
                size += len(json.dumps(tool_calls, ensure_ascii=False))
        return size

    @classmethod
    def _trim_by_chars(
        cls, turns: list[dict[str, Any]], max_chars: int
    ) -> list[dict[str, Any]]:
        if max_chars <= 0 or not turns:
            return []
        selected: list[dict[str, Any]] = []
        total = 0
        for turn in reversed(turns):
            size = cls._turn_char_size(turn)
            if selected and total + size > max_chars:
                break
            selected.append(turn)
            total += size
        selected.reverse()
        return selected

    async def get_meta(self, key: SessionKey) -> Optional[SessionMeta]:
        self._check_open()
        if not isinstance(key, SessionKey):
            raise SessionStoreError("key 必须是 SessionKey")
        archived = False
        state = await asyncio.to_thread(self._read_file, self._active_path(key))
        if state is None:
            state = await asyncio.to_thread(self._read_file, self._archive_path(key))
            archived = state is not None
        if state is None:
            return None
        created_at_ms = int(state.header.get("created_at_ms", 0))
        updated_at_ms = (
            int(state.last_turn.get("ts_ms", created_at_ms))
            if state.last_turn
            else created_at_ms
        )
        return SessionMeta(
            storage_id=key.storage_id,
            session_key=self._key_from_header(state.header, key),
            created_at_ms=created_at_ms,
            updated_at_ms=updated_at_ms,
            turn_count=state.turn_count,
            message_count=state.message_count,
            archived=archived,
        )

    async def list_sessions(self, *, include_archived: bool = True) -> list[SessionSummary]:
        self._check_open()
        paths = sorted(self.sessions_dir.glob("*.jsonl"), key=lambda item: item.name)
        if include_archived:
            paths.extend(sorted(self.archive_dir.glob("*.jsonl"), key=lambda item: item.name))

        entries: dict[str, _SessionEntry] = {}
        for path in paths:
            state = await asyncio.to_thread(self._read_file, path)
            if state is None:
                continue
            storage_id = str(state.header.get("storage_id") or path.stem)
            archived = path.parent == self.archive_dir
            existing = entries.get(storage_id)
            # 活跃优先: 已存在活跃记录时不覆盖; 已存在归档时仅允许活跃记录覆盖
            if existing is not None and (not existing.archived or archived):
                continue
            created_at_ms = int(state.header.get("created_at_ms", 0))
            updated_at_ms = (
                int(state.last_turn.get("ts_ms", created_at_ms))
                if state.last_turn
                else created_at_ms
            )
            entries[storage_id] = _SessionEntry(
                storage_id=storage_id,
                key=self._key_from_header(state.header, self._placeholder_key(storage_id)),
                created_at_ms=created_at_ms,
                updated_at_ms=updated_at_ms,
                turn_count=state.turn_count,
                message_count=state.message_count,
                archived=archived,
            )

        short_ids = self._short_ids(list(entries))
        summaries = [
            SessionSummary(
                short_id=short_ids[entry.storage_id],
                storage_id=entry.storage_id,
                session_key=entry.key,
                created_at_ms=entry.created_at_ms,
                updated_at_ms=entry.updated_at_ms,
                turn_count=entry.turn_count,
                message_count=entry.message_count,
                archived=entry.archived,
            )
            for entry in entries.values()
        ]
        summaries.sort(key=lambda item: (item.updated_at_ms, item.created_at_ms), reverse=True)
        return summaries

    @staticmethod
    def _placeholder_key(storage_id: str) -> SessionKey:
        """header 损坏时用于展示的占位键; 保证不抛异常."""
        return SessionKey(channel="unknown", scope="unknown", conversation_id=storage_id)

    def _short_ids(self, storage_ids: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for storage_id in storage_ids:
            length = min(self.short_id_min_length, len(storage_id)) or 1
            while length < len(storage_id):
                prefix = storage_id[:length]
                collides = any(
                    other != storage_id and other[:length] == prefix
                    for other in storage_ids
                )
                if not collides:
                    break
                length += 1
            result[storage_id] = storage_id[:length]
        return result

    async def archive(self, key: SessionKey) -> None:
        self._check_open()
        if not isinstance(key, SessionKey):
            raise SessionStoreError("key 必须是 SessionKey")
        source = self._active_path(key)
        target = self._archive_path(key)

        async with self._session_guard(key.storage_id):
            def _move() -> None:
                if not source.exists():
                    return
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)

            try:
                await asyncio.to_thread(_move)
            except OSError as exc:
                raise SessionStoreError(f"归档会话失败: {key.canonical} ({exc!r})") from exc

    async def purge(self, key: SessionKey) -> None:
        self._check_open()
        if not isinstance(key, SessionKey):
            raise SessionStoreError("key 必须是 SessionKey")
        paths = (self._active_path(key), self._archive_path(key))

        async with self._session_guard(key.storage_id):
            def _purge() -> None:
                for path in paths:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass

            try:
                await asyncio.to_thread(_purge)
            except OSError as exc:
                raise SessionStoreError(f"删除会话失败: {key.canonical} ({exc!r})") from exc

    async def close(self) -> None:
        self._closed = True
        self._locks.clear()
