"""记忆系统数据模型: SessionKey / SessionMessage / SessionMeta / SessionSummary.

设计约定见 ``docs/MEMORY_DESIGN.md``:
- 会话键在内存中用结构化对象表示, 序列化为 ``v1:<channel>:<scope>:<id>[:<session_id>]``;
- 物理文件名使用 ``storage_id``(sha256 + base32 前 26 位), 避免非法字符/越界/碰撞;
- 消息存储视角与 OpenAI messages 视角通过 ``to_llm_message()`` 显式投影。
"""

import base64
import copy
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Optional

from .errors import InvalidSessionKeyError

# 默认工具结果截断阈值与提示(契约层共享)
DEFAULT_MAX_TOOL_RESULT_CHARS = 8000
TOOL_RESULT_TRUNCATE_NOTICE = "\n...(工具结果过长, 已截断)"
# 短 ID 最少长度
MIN_SHORT_ID_LENGTH = 8

_CANONICAL_VERSION_RE = re.compile(r"^v([1-9]\d*)$")


def utc_now_ms() -> int:
    """当前 UTC 时间的 epoch milliseconds."""
    return int(time.time() * 1000)


def ms_to_iso(ms: int) -> str:
    """epoch milliseconds -> UTC ISO8601(毫秒精度, 以 Z 结尾)."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _require_component(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidSessionKeyError(f"{field} 不能为空")
    text = value.strip()
    if ":" in text:
        raise InvalidSessionKeyError(f"{field} 不能包含 ':' (canonical key 以 ':' 分隔)")
    return text


@dataclass(frozen=True)
class SessionKey:
    """会话的结构化标识.

    - CLI 会话: ``channel=cli, scope=session, conversation_id=<uuid>``
    - QQ 逻辑联系人: ``channel=qq, scope=private, conversation_id=<uin>``
    - QQ 会话实例: 在上者基础上增加 ``session_id=<uuid>``
    ``user_id`` 仅作为元数据, 不参与 canonical/storage_id。
    """

    channel: str
    scope: str
    conversation_id: str
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.version, int) or self.version < 1:
            raise InvalidSessionKeyError("version 必须是 >= 1 的整数")
        _require_component(self.channel, "channel")
        _require_component(self.scope, "scope")
        _require_component(self.conversation_id, "conversation_id")
        if self.session_id is not None:
            _require_component(self.session_id, "session_id")
        if self.user_id is not None:
            _require_component(self.user_id, "user_id")

    @property
    def canonical(self) -> str:
        """稳定、可读的规范字符串; 用作存储元数据与日志标识."""
        parts = [f"v{self.version}", self.channel, self.scope, self.conversation_id]
        if self.session_id is not None:
            parts.append(self.session_id)
        return ":".join(parts)

    @property
    def storage_id(self) -> str:
        """物理文件名使用的不透明 ID: sha256 后 base32, 小写, 长度 26."""
        digest = hashlib.sha256(self.canonical.encode("utf-8")).digest()
        encoded = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
        return encoded[:26]

    def contact_key(self) -> "SessionKey":
        """去掉会话实例 ID, 得到逻辑联系人键(如 QQ uin 级)."""
        return replace(self, session_id=None)

    def with_session(self, session_id: str) -> "SessionKey":
        """基于当前键派生一个新会话实例键."""
        return replace(self, session_id=session_id)

    @classmethod
    def from_canonical(cls, canonical: str) -> "SessionKey":
        """解析规范字符串; 不还原 ``user_id``(它不参与 canonical)."""
        if not isinstance(canonical, str) or not canonical.strip():
            raise InvalidSessionKeyError("canonical key 不能为空")
        parts = canonical.strip().split(":")
        if len(parts) not in (4, 5):
            raise InvalidSessionKeyError(f"canonical key 段数不合法: {canonical!r}")
        match = _CANONICAL_VERSION_RE.match(parts[0])
        if match is None:
            raise InvalidSessionKeyError(f"canonical key 版本前缀不合法: {parts[0]!r}")
        return cls(
            channel=parts[1],
            scope=parts[2],
            conversation_id=parts[3],
            session_id=parts[4] if len(parts) == 5 else None,
            version=int(match.group(1)),
        )

    def __str__(self) -> str:
        return self.canonical


@dataclass
class SessionMessage:
    """一条会话消息的存储视角; 通过 ``to_llm_message`` 投影回 OpenAI 格式."""

    role: str
    content: Optional[str] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None
    ts_ms: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("role 不能为空")
        self.role = self.role.strip()
        if self.tool_calls is not None and not isinstance(self.tool_calls, list):
            raise ValueError("tool_calls 必须是 list[dict] 或 None")

    def to_llm_message(self) -> dict[str, Any]:
        """投影为 OpenAI messages 条目, 去掉内部字段(ts_ms 等)."""
        message: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            message["content"] = self.content
        if self.name is not None:
            message["name"] = self.name
        if self.tool_call_id is not None:
            message["tool_call_id"] = self.tool_call_id
        if self.tool_calls is not None:
            message["tool_calls"] = copy.deepcopy(self.tool_calls)
        return message

    @classmethod
    def from_llm_message(
        cls, message: dict[str, Any], *, ts_ms: Optional[int] = None
    ) -> "SessionMessage":
        """从 OpenAI 格式消息构造存储对象."""
        if not isinstance(message, dict):
            raise ValueError("message 必须是 dict")
        return cls(
            role=str(message.get("role") or ""),
            content=message.get("content"),
            name=message.get("name"),
            tool_call_id=message.get("tool_call_id"),
            tool_calls=message.get("tool_calls"),
            ts_ms=ts_ms,
        )

    def to_storage_dict(self) -> dict[str, Any]:
        """JSONL 落盘用的 dict; content 始终保留(可 null)."""
        data: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name is not None:
            data["name"] = self.name
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        if self.tool_calls is not None:
            data["tool_calls"] = copy.deepcopy(self.tool_calls)
        if self.ts_ms is not None:
            data["ts_ms"] = self.ts_ms
        return data

    @classmethod
    def from_storage_dict(cls, data: dict[str, Any]) -> "SessionMessage":
        """从 JSONL 记录构造; 缺字段容错."""
        if not isinstance(data, dict):
            raise ValueError("存储记录必须是 dict")
        return cls(
            role=str(data.get("role") or ""),
            content=data.get("content"),
            name=data.get("name"),
            tool_call_id=data.get("tool_call_id"),
            tool_calls=data.get("tool_calls"),
            ts_ms=data.get("ts_ms"),
        )

    def char_size(self) -> int:
        """近似字符占用, 用于上下文窗口预算."""
        size = len(self.content or "")
        if self.tool_calls:
            size += len(json.dumps(self.tool_calls, ensure_ascii=False))
        return size


@dataclass(frozen=True)
class SessionMeta:
    """会话元数据."""

    storage_id: str
    session_key: SessionKey
    created_at_ms: int
    updated_at_ms: int
    turn_count: int
    message_count: int
    archived: bool = False

    @property
    def created_at_iso(self) -> str:
        return ms_to_iso(self.created_at_ms)

    @property
    def updated_at_iso(self) -> str:
        return ms_to_iso(self.updated_at_ms)

    @property
    def channel(self) -> str:
        return self.session_key.channel

    @property
    def scope(self) -> str:
        return self.session_key.scope

    @property
    def conversation_id(self) -> str:
        return self.session_key.conversation_id


@dataclass(frozen=True)
class SessionSummary:
    """``/sessions`` 列表用的会话摘要."""

    short_id: str
    storage_id: str
    session_key: SessionKey
    created_at_ms: int
    updated_at_ms: int
    turn_count: int
    message_count: int
    archived: bool = False

    @property
    def created_at_iso(self) -> str:
        return ms_to_iso(self.created_at_ms)

    @property
    def updated_at_iso(self) -> str:
        return ms_to_iso(self.updated_at_ms)

    @property
    def channel(self) -> str:
        return self.session_key.channel

    @property
    def scope(self) -> str:
        return self.session_key.scope


@dataclass(frozen=True)
class MemoryRecord:
    """结构化长期记忆记录(为未来 LongTermStore 预留).

    与 ``MEMORY.md`` 的关系: v1 的长期记忆由 ``MEMORY.md`` 承担, 本模型仅定义契约;
    未来 JSONL/SQLite/MySQL 后端实现 ``LongTermStore`` 时按此结构存取。
    """

    id: str
    namespace: str
    kind: str
    content: str
    tags: tuple[str, ...] = ()
    confidence: float = 1.0
    source_session: Optional[str] = None
    source_message_id: Optional[str] = None
    created_at_ms: int = 0
    updated_at_ms: int = 0
    expires_at_ms: Optional[int] = None

    def __post_init__(self) -> None:
        for field, value in (
            ("id", self.id),
            ("namespace", self.namespace),
            ("kind", self.kind),
            ("content", self.content),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} 不能为空")
        if not isinstance(self.confidence, (int, float)) or not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence 必须在 [0, 1] 区间")
        if self.created_at_ms < 0 or self.updated_at_ms < 0:
            raise ValueError("时间戳不能为负数")
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("updated_at_ms 不能早于 created_at_ms")
        if self.expires_at_ms is not None and self.expires_at_ms < self.created_at_ms:
            raise ValueError("expires_at_ms 不能早于 created_at_ms")

        clean_tags = tuple(
            str(tag).strip() for tag in self.tags if str(tag).strip()
        )
        object.__setattr__(self, "tags", clean_tags)

    @property
    def created_at_iso(self) -> str:
        return ms_to_iso(self.created_at_ms)

    @property
    def updated_at_iso(self) -> str:
        return ms_to_iso(self.updated_at_ms)

    def to_dict(self) -> dict[str, Any]:
        """可 JSON 序列化的 dict(tags 转 list)."""
        return {
            "id": self.id,
            "namespace": self.namespace,
            "kind": self.kind,
            "content": self.content,
            "tags": list(self.tags),
            "confidence": self.confidence,
            "source_session": self.source_session,
            "source_message_id": self.source_message_id,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "expires_at_ms": self.expires_at_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryRecord":
        """从 dict 恢复; 未知字段忽略, 缺省字段使用默认值."""
        if not isinstance(data, dict):
            raise ValueError("MemoryRecord 记录必须是 dict")
        return cls(
            id=str(data.get("id") or ""),
            namespace=str(data.get("namespace") or ""),
            kind=str(data.get("kind") or ""),
            content=str(data.get("content") or ""),
            tags=tuple(data.get("tags") or ()),
            confidence=float(data.get("confidence", 1.0)),
            source_session=data.get("source_session"),
            source_message_id=data.get("source_message_id"),
            created_at_ms=int(data.get("created_at_ms") or 0),
            updated_at_ms=int(data.get("updated_at_ms") or 0),
            expires_at_ms=data.get("expires_at_ms"),
        )


def new_memory_record(
    namespace: str,
    content: str,
    *,
    kind: str = "fact",
    tags: tuple[str, ...] = (),
    confidence: float = 1.0,
    source_session: Optional[str] = None,
    source_message_id: Optional[str] = None,
    now_ms: Optional[int] = None,
) -> MemoryRecord:
    """构造一条带 UUID 与当前时间戳的长期记忆记录."""
    timestamp = utc_now_ms() if now_ms is None else int(now_ms)
    return MemoryRecord(
        id=uuid.uuid4().hex,
        namespace=namespace,
        kind=kind,
        content=content,
        tags=tuple(tags),
        confidence=confidence,
        source_session=source_session,
        source_message_id=source_message_id,
        created_at_ms=timestamp,
        updated_at_ms=timestamp,
    )
