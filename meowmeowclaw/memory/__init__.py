"""记忆系统: 会话数据模型、SessionStore 抽象接口与 JSONL 实现.

v1 范围见 ``docs/MEMORY_DESIGN.md``: 只实现短期记忆(按会话隔离);
长期记忆暂由 ``MEMORY.md`` 文件承担, LongTermStore 为后续里程碑的预留接口。
"""

from .errors import InvalidSessionKeyError, MemoryStoreError, SessionStoreError
from .jsonl import JsonlSessionStore
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
from .store import SessionStore

__all__ = [
    "DEFAULT_MAX_TOOL_RESULT_CHARS",
    "MIN_SHORT_ID_LENGTH",
    "TOOL_RESULT_TRUNCATE_NOTICE",
    "InvalidSessionKeyError",
    "JsonlSessionStore",
    "MemoryStoreError",
    "SessionKey",
    "SessionMessage",
    "SessionMeta",
    "SessionStore",
    "SessionStoreError",
    "SessionSummary",
    "ms_to_iso",
    "utc_now_ms",
]
