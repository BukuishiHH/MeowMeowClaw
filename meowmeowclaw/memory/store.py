"""SessionStore 抽象接口.

v1 只提供 JSONL 实现(``JsonlSessionStore``); 未来 SQLite/MySQL 后端实现同一协议即可替换。
所有方法均为 async, 以适配 aiomysql / redis.asyncio 等未来后端。
"""

from typing import Optional, Protocol, Sequence, runtime_checkable

from .models import (
    MemoryRecord,
    SessionKey,
    SessionMessage,
    SessionMeta,
    SessionSummary,
)


@runtime_checkable
class SessionStore(Protocol):
    """短期记忆仓储: 每个会话一条 turn 流水."""

    async def append_turn(
        self,
        key: SessionKey,
        messages: Sequence[SessionMessage],
        *,
        turn_id: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> SessionMeta:
        """原子追加一轮对话, 返回更新后的会话元数据."""
        ...

    async def load_recent(
        self,
        key: SessionKey,
        *,
        max_turns: Optional[int] = None,
        max_chars: Optional[int] = None,
    ) -> list[SessionMessage]:
        """从最近往前装载整轮消息; 会话不存在或已归档返回空列表."""
        ...

    async def get_meta(self, key: SessionKey) -> Optional[SessionMeta]:
        """读取会话元数据(活跃或归档); 不存在返回 None."""
        ...

    async def list_sessions(self, *, include_archived: bool = True) -> list[SessionSummary]:
        """列出会话摘要, 按最后活动时间倒序."""
        ...

    async def archive(self, key: SessionKey) -> None:
        """把会话移动到归档区(幂等); 对应 ``/clear``."""
        ...

    async def purge(self, key: SessionKey) -> None:
        """永久删除会话及其归档副本(幂等); 对应 ``/clear --purge``."""
        ...

    async def close(self) -> None:
        """释放资源; 关闭后调用其他方法应报错."""
        ...


@runtime_checkable
class LongTermStore(Protocol):
    """结构化长期记忆仓储(未来后端: JSONL/SQLite/MySQL...).

    v1 由 ``NoopLongTermStore`` 占位; 命名空间约定:
    ``user:default``(当前唯一用户) -> 未来 ``user:<id>`` / ``group:<id>``。
    """

    async def recall(
        self,
        namespace: str,
        *,
        query: Optional[str] = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        """按命名空间召回记忆; query 为空时返回最近的若干条."""
        ...

    async def remember(self, namespace: str, record: MemoryRecord) -> MemoryRecord:
        """写入/更新一条记忆, 返回最终记录(包含 store 可能补全的字段)."""
        ...

    async def forget(self, namespace: str, record_id: str) -> None:
        """按 id 删除一条记忆(幂等)."""
        ...

    async def list_namespaces(self) -> list[str]:
        """列出已有命名空间(调试/管理用)."""
        ...

    async def close(self) -> None:
        """释放资源."""
        ...
