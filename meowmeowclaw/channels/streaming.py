"""流式渲染的通用基础设施(S2–S5): 水位线 + 前缀补发.

见 docs/STREAMING_DESIGN.md §4.6:

- ``StreamAssembler`` 按 ``correlation_id`` 记录"**已真正送达平台**的内容"(水位线);
- 最终 ``reply`` 到达时只补发剩余部分, 保证"已发送内容 ≙ 最终文本前缀"且不重复(I1);
- 前缀不一致(网关重试/内容改写)时回退整段重发并 warning;

约定: ``mark_sent()`` 必须在内容**真正送达平台之后**调用 ——
``typing`` 模式不产生气泡, 因此不调用它, 最终仍会整段发送。
"""

import logging
from collections import OrderedDict

logger = logging.getLogger(__name__)

# 单条平台消息的字符上限(QQ 侧默认 3000; CLI 无此限制但沿用同一水位线)
DEFAULT_MAX_TEXT_CHARS = 3000
# 未收到最终回复的 correlation 最多保留多少个(LRU 淘汰, 防止内存泄漏)
DEFAULT_MAX_ENTRIES = 64


def chunk_text(text: str, max_chars: int = DEFAULT_MAX_TEXT_CHARS) -> list[str]:
    """按字符数切分长文本(超长回复分片发送); ``max_chars<=0`` 时不切分."""
    if not text:
        return []
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]
    return [text[start : start + max_chars] for start in range(0, len(text), max_chars)]


class StreamAssembler:
    """按 correlation 维护"已发送前缀"的水位线(进程内, 单实例)."""

    def __init__(
        self,
        *,
        max_chars: int = DEFAULT_MAX_TEXT_CHARS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        if max_entries <= 0:
            raise ValueError(f"max_entries 必须为正整数: {max_entries!r}")
        self.max_chars = int(max_chars)
        self.max_entries = int(max_entries)
        self._sent: "OrderedDict[str, list[str]]" = OrderedDict()

    def __repr__(self) -> str:
        return f"<StreamAssembler entries={len(self._sent)} max_chars={self.max_chars}>"

    # ------------------------------------------------------------------ 内部

    def _entry(self, correlation_id: str) -> list[str]:
        entry = self._sent.get(correlation_id)
        if entry is None:
            entry = []
            self._sent[correlation_id] = entry
            # LRU 淘汰: 清掉最久没被更新且没有最终回复的 correlation
            while len(self._sent) > self.max_entries:
                evicted, _ = self._sent.popitem(last=False)
                logger.debug("流式水位线 LRU 淘汰: correlation=%s", evicted)
        else:
            self._sent.move_to_end(correlation_id)
        return entry

    # ------------------------------------------------------------------ 契约

    def register(self, correlation_id: str) -> None:
        """登记一个 correlation(已存在则刷新 LRU); 供适配器在 start 相位调用."""
        self._entry(correlation_id)

    def mark_sent(self, correlation_id: str, text: str) -> None:
        """记录一段**已经送达平台**的内容."""
        if not text:
            return
        self._entry(correlation_id).append(text)

    def sent_text(self, correlation_id: str) -> str:
        """当前水位线内容(已发送前缀)."""
        entry = self._sent.get(correlation_id)
        return "".join(entry) if entry else ""

    def plan_final(self, correlation_id: str, full_text: str) -> list[str]:
        """最终回复到达: 返回仍需发送的分片(通常只有"剩余后缀").

        - 正常: ``full_text`` 以已发送内容开头 → 只补发剩余部分, 绝不重复;
        - 异常(前缀不一致): 记录 warning 并整段重发(接受一次重复, 概率极低);
        - 未登记/未发送过任何内容: 返回完整 ``full_text`` 的分片。
        """
        prefix = self.sent_text(correlation_id)
        if prefix and not full_text.startswith(prefix):
            logger.warning(
                "流式前缀与最终文本不一致, 回退整段重发: correlation=%s sent=%d final=%d",
                correlation_id,
                len(prefix),
                len(full_text),
            )
            remaining = full_text
        else:
            remaining = full_text[len(prefix) :]
        return chunk_text(remaining, self.max_chars)

    def forget(self, correlation_id: str) -> None:
        """最终回复处理完毕后清理该 correlation 的状态."""
        self._sent.pop(correlation_id, None)

    def clear(self) -> None:
        self._sent.clear()

    @property
    def size(self) -> int:
        return len(self._sent)


__all__ = ["DEFAULT_MAX_ENTRIES", "DEFAULT_MAX_TEXT_CHARS", "StreamAssembler", "chunk_text"]
