"""长期记忆的空实现(占位).

v1 的长期记忆由 ``MEMORY.md`` 文件承担; 本实现只保证 ``LongTermStore`` 接口可用,
为将来结构化长期记忆预留装配位置。所有方法不落盘、不改变任何状态。
"""

import logging
from typing import Optional

from .models import MemoryRecord

logger = logging.getLogger(__name__)


class NoopLongTermStore:
    """不做任何存储的 ``LongTermStore`` 实现."""

    def __repr__(self) -> str:
        return "<NoopLongTermStore (v1 占位, 长期记忆由 MEMORY.md 承担)>"

    async def recall(
        self,
        namespace: str,
        *,
        query: Optional[str] = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        logger.debug("NoopLongTermStore.recall(namespace=%r, query=%r)", namespace, query)
        return []

    async def remember(self, namespace: str, record: MemoryRecord) -> MemoryRecord:
        logger.debug("NoopLongTermStore.remember(namespace=%r, id=%r)", namespace, record.id)
        return record

    async def forget(self, namespace: str, record_id: str) -> None:
        logger.debug("NoopLongTermStore.forget(namespace=%r, id=%r)", namespace, record_id)

    async def list_namespaces(self) -> list[str]:
        return []

    async def close(self) -> None:
        return None
