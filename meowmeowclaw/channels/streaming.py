"""流式渲染的通用基础设施(S2–S5): 水位线 + 前缀补发.

见 docs/STREAMING_DESIGN.md §4.6:

- ``StreamAssembler`` 按 ``correlation_id`` 记录"**已真正送达平台**的内容"(水位线);
- 最终 ``reply`` 到达时只补发剩余部分, 保证"已发送内容 ≙ 最终文本前缀"且不重复(I1);
- 前缀不一致(网关重试/内容改写)时回退整段重发并 warning;

约定: ``mark_sent()`` 必须在内容**真正送达平台之后**调用 ——
``typing`` 模式不产生气泡, 因此不调用它, 最终仍会整段发送。
"""

import asyncio
import logging
from collections import OrderedDict
from contextlib import suppress
from typing import Awaitable, Callable, Optional

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


class TypingKeepAlive:
    """QQ 专用: 周期性维持"对方正在输入"状态(``set_input_status``).

    - ``start()`` 立即发一次并起后台任务, 之后每 ``refresh_seconds`` 秒续期
      (QQ 的输入状态会过期, 长回答需要续期才能一直显示);
    - ``stop()`` 取消任务(幂等), 异常路径也不会外抛;
    - 单次发送失败只记 debug(静默降级, 见 S-D4), 不影响主流程。
    """

    def __init__(
        self,
        action: Callable[[], Awaitable[object]],
        *,
        label: str = "",
        refresh_seconds: float = 5.0,
    ) -> None:
        if refresh_seconds <= 0:
            raise ValueError(f"refresh_seconds 必须为正数: {refresh_seconds!r}")
        self.action = action
        self.label = label
        self.refresh_seconds = float(refresh_seconds)
        self._task: Optional[asyncio.Task] = None
        self._sent_count = 0

    def __repr__(self) -> str:
        return (
            f"<TypingKeepAlive label={self.label!r} refresh={self.refresh_seconds}s "
            f"running={self._task is not None} sent={self._sent_count}>"
        )

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def sent_count(self) -> int:
        return self._sent_count

    def start(self) -> None:
        """立即启动(不阻塞调用方; 首次发送在后台任务里完成)."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name=f"typing-keepalive-{self.label}")

    async def _run(self) -> None:
        while True:
            try:
                await self.action()
                self._sent_count += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 输入状态失败必须静默降级
                logger.debug("设置输入状态失败(已忽略): label=%s", self.label, exc_info=True)
            await asyncio.sleep(self.refresh_seconds)

    async def stop(self) -> None:
        """取消续期任务(幂等)."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_TEXT_CHARS",
    "StreamAssembler",
    "TypingKeepAlive",
    "chunk_text",
]
