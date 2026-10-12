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


# 语义边界(从高到低优先级; 见 docs/STREAMING_DESIGN.md §4.6)
PARAGRAPH_BOUNDARY = "\n\n"
LINE_BOUNDARY = "\n"
SENTENCE_ENDINGS = "。！？!?；;…"
COMMA_ENDINGS = "，,、"
CODE_FENCE = "```"
# 强制硬切前允许回退寻找自然断点的窗口(避免切断 URL/数字/长单词)
_HARD_CUT_LOOKBACK = 40
_HARD_CUT_SAFE_CHARS = " \t\n，。,.、；;：:!?！？)]}\n"


def _fence_safe_end(text: str) -> int:
    """返回"可安全切割"的上界: 未闭合 ``` 围栏之后的内容不切(避免把代码块劈成两条气泡)."""
    open_at = -1
    cursor = 0
    while True:
        found = text.find(CODE_FENCE, cursor)
        if found < 0:
            break
        open_at = -1 if open_at >= 0 else found
        cursor = found + len(CODE_FENCE)
    return len(text) if open_at < 0 else open_at


class BlockSegmenter:
    """把流式增量文本按"自然边界"切成可发送的块(纯逻辑, 可 100% 单测).

    边界优先级: 段落(空行) > 换行 > 句末标点 > 逗号(仅当缓冲足够长) > 硬切(兜底);
    ``` 代码围栏未闭合时不切; 硬切会回退到最近的空白/标点, 避免切断 URL 或数字。
    """

    def __init__(self, *, soft_chars: int = 80, hard_chars: int = 900) -> None:
        if soft_chars <= 0:
            raise ValueError(f"soft_chars 必须为正整数: {soft_chars!r}")
        if hard_chars <= 0:
            raise ValueError(f"hard_chars 必须为正整数: {hard_chars!r}")
        self.soft_chars = int(soft_chars)
        self.hard_chars = max(int(hard_chars), self.soft_chars)
        self._buffer = ""

    def __repr__(self) -> str:
        return (
            f"<BlockSegmenter buffered={len(self._buffer)} soft={self.soft_chars} "
            f"hard={self.hard_chars}>"
        )

    @property
    def buffered_length(self) -> int:
        return len(self._buffer)

    def push(self, text: str) -> None:
        if text:
            self._buffer += text

    def has_boundary(self) -> bool:
        """当前缓冲里是否已有可切边界(含硬切兜底)."""
        return self._find_cut() is not None

    def take(self) -> Optional[str]:
        """取出一块(按边界切); 无可切边界时返回 None 并保持缓冲不变."""
        cut = self._find_cut()
        if cut is None:
            return None
        block, self._buffer = self._buffer[:cut], self._buffer[cut:]
        return block

    def flush(self) -> str:
        """取出全部剩余缓冲(结束/强制收尾时使用)."""
        remaining, self._buffer = self._buffer, ""
        return remaining

    # ------------------------------------------------------------------ 内部

    def _find_cut(self) -> Optional[int]:
        text = self._buffer
        if not text:
            return None
        # 未闭合代码围栏之后的内容不参与切割
        region = text[: _fence_safe_end(text)]

        index = region.rfind(PARAGRAPH_BOUNDARY)
        if index >= 0:
            return index + len(PARAGRAPH_BOUNDARY)
        index = region.rfind(LINE_BOUNDARY)
        if index >= 0:
            return index + len(LINE_BOUNDARY)

        index = max((region.rfind(char) for char in SENTENCE_ENDINGS), default=-1)
        if index >= 0:
            return index + 1

        # 逗号只在缓冲已经明显偏长时才作为边界, 避免切得太碎
        if len(text) >= self.soft_chars * 1.5:
            index = max((region.rfind(char) for char in COMMA_ENDINGS), default=-1)
            if index >= 0:
                return index + 1

        if len(text) >= self.hard_chars:
            return self._hard_cut(text)
        return None

    def _hard_cut(self, text: str) -> int:
        """无自然边界且超长时的兜底: 在 hard_chars 附近回退到最近的空白/标点."""
        limit = min(self.hard_chars, len(text))
        floor = max(1, limit - _HARD_CUT_LOOKBACK)
        for probe in range(limit - 1, floor - 1, -1):
            if text[probe] in _HARD_CUT_SAFE_CHARS:
                return probe + 1
        return limit


class TypingKeepAlive:
    """QQ 专用: 周期性维持"对方正在输入"状态(``set_input_status``).

    - ``start()`` 立即发一次并起后台任务, 之后每 ``refresh_seconds`` 秒续期
      (QQ 的输入状态会过期, 长回答需要续期才能一直显示);
    - 单次发送失败只记 debug(静默降级, 见 S-D4), 不影响主流程;
    - **协作式停止**: ``stop()`` 先置停止标志, 再取消任务并**有限等待**;
      若任务仍在跑(例如底层 SDK 在某个 await 点吞掉了取消), 直接放弃等待并返回 ——
      任务会在当前动作结束后自行退出。这样 ``stop()`` 永不阻塞调用方
      (它是从适配器出站循环里调的, 一旦卡住会冻结整条 QQ 回复链路)。
    """

    def __init__(
        self,
        action: Callable[[], Awaitable[object]],
        *,
        label: str = "",
        refresh_seconds: float = 5.0,
        stop_timeout: float = 0.25,
    ) -> None:
        if refresh_seconds <= 0:
            raise ValueError(f"refresh_seconds 必须为正数: {refresh_seconds!r}")
        if stop_timeout <= 0:
            raise ValueError(f"stop_timeout 必须为正数: {stop_timeout!r}")
        self.action = action
        self.label = label
        self.refresh_seconds = float(refresh_seconds)
        self.stop_timeout = float(stop_timeout)
        self._stop_event = asyncio.Event()
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
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name=f"typing-keepalive-{self.label}")

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.action()
                self._sent_count += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 输入状态失败必须静默降级
                logger.debug("设置输入状态失败(已忽略): label=%s", self.label, exc_info=True)
            if self._stop_event.is_set():
                return
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.refresh_seconds)

    async def stop(self, timeout: Optional[float] = None) -> None:
        """协作式停止(幂等, 永不阻塞): 置标志 → 取消 → 有限等待 → 必要时放弃等待."""
        self._stop_event.set()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        budget = self.stop_timeout if timeout is None else float(timeout)
        _, pending = await asyncio.wait({task}, timeout=budget)
        if pending:
            logger.debug(
                "输入状态任务未在 %.2fs 内退出, 已放弃等待(任务将在当前动作结束后自行退出): label=%s",
                budget,
                self.label,
            )


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_TEXT_CHARS",
    "BlockSegmenter",
    "StreamAssembler",
    "TypingKeepAlive",
    "chunk_text",
]
