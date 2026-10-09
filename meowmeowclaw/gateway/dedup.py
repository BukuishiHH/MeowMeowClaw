"""网关幂等去重(见 docs/GATEWAY_DESIGN.md §7.4).

仅进程内 LRU: 重启后丢失, 平台重推可能重复处理; v1 接受该代价.
"""

from collections import OrderedDict
from typing import Optional

from .envelope import Envelope


class DedupCache:
    """按 ``(channel, message_id)`` 记录已见消息; 容量满时淘汰最旧."""

    def __init__(self, maxsize: int = 4096) -> None:
        if not isinstance(maxsize, int) or isinstance(maxsize, bool) or maxsize <= 0:
            raise ValueError(f"maxsize 必须是正整数: {maxsize!r}")
        self.maxsize = maxsize
        self._seen: OrderedDict[str, None] = OrderedDict()

    def __len__(self) -> int:
        return len(self._seen)

    def __repr__(self) -> str:
        return f"<DedupCache size={len(self._seen)}/{self.maxsize}>"

    @staticmethod
    def key_for(envelope: Envelope) -> str:
        """message_id 缺失时返回空串(= 不去重)."""
        if not envelope.message_id:
            return ""
        return f"{envelope.channel}:{envelope.message_id}"

    def seen(self, key: Optional[str]) -> bool:
        """返回 True 表示之前已见过; 首次调用返回 False 并记账(容量满淘汰最旧)."""
        if not key:
            return False
        if key in self._seen:
            self._seen.move_to_end(key)
            return True
        self._seen[key] = None
        if len(self._seen) > self.maxsize:
            self._seen.popitem(last=False)
        return False

    def clear(self) -> None:
        self._seen.clear()
