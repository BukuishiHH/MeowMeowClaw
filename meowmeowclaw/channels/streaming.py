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
import math
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

    def remaining_text(self, correlation_id: str, full_text: str) -> str:
        """最终文本里**尚未送达**的部分(块级流式按它做收尾均分).

        - 正常: ``full_text`` 以已发送内容开头 → 返回剩余后缀, 绝不重复;
        - 异常(前缀不一致): 记录 warning 并返回整段(兜底重发, 接受一次重复, 概率极低);
        - 未登记/未发送过任何内容: 返回完整 ``full_text``。
        """
        prefix = self.sent_text(correlation_id)
        if prefix and not full_text.startswith(prefix):
            logger.warning(
                "流式前缀与最终文本不一致, 回退整段重发: correlation=%s sent=%d final=%d",
                correlation_id,
                len(prefix),
                len(full_text),
            )
            return full_text
        return full_text[len(prefix) :]

    def plan_final(self, correlation_id: str, full_text: str) -> list[str]:
        """最终回复到达: 返回仍需发送的分片(按单条消息上限切).

        用于 CLI 等"整段收尾"的渠道; QQ block 模式请用 :meth:`remaining_text` 配合
        ``split_evenly`` 做均分。
        """
        return chunk_text(self.remaining_text(correlation_id, full_text), self.max_chars)

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
    """把流式增量文本按"自然边界"切成**大小可控**的块(纯逻辑, 可 100% 单测).

    切块规则(见 docs/STREAMING_DESIGN.md §4.6, S5.1 修订):

    1. 只在 ``[min_chars, max_chars]`` 窗口内选切点(窗口还受未闭合 ``` 围栏限制);
    2. 边界优先级: 段落(空行) > 换行 > 句末标点 > 逗号(仅当窗口足够长);
       **同级内取最接近 ``target_chars`` 的那个**, 保证块大小接近目标、分布均匀;
    3. 窗口内没有任何边界且缓冲已达 ``max_chars`` → 硬切(回退到最近空白/标点, 不切断 URL/数字);
    4. 缓冲不足 ``min_chars`` → 继续攒, 不切。

    这样每个块都落在 ``[min_chars, max_chars]`` 内 —— 既不会出现几十字的"小头",
    也不会出现一次几百字的"巨块"(旧实现在整段缓冲里取"最后一个边界", 两者都会发生)。
    """

    def __init__(
        self,
        *,
        min_chars: int = 80,
        target_chars: int = 150,
        max_chars: int = 260,
    ) -> None:
        if min_chars <= 0:
            raise ValueError(f"min_chars 必须为正整数: {min_chars!r}")
        if target_chars <= 0:
            raise ValueError(f"target_chars 必须为正整数: {target_chars!r}")
        if max_chars <= 0:
            raise ValueError(f"max_chars 必须为正整数: {max_chars!r}")
        self.min_chars = int(min_chars)
        # target/max 不允许小于下限, 否则规则自相矛盾; 就地夹取
        self.target_chars = max(self.min_chars, int(target_chars))
        self.max_chars = max(self.target_chars, int(max_chars))
        self._buffer = ""

    def __repr__(self) -> str:
        return (
            f"<BlockSegmenter buffered={len(self._buffer)} min={self.min_chars} "
            f"target={self.target_chars} max={self.max_chars}>"
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
        # 未闭合围栏之后不允许切; 同时窗口不超过 max_chars
        safe_end = min(len(text), self.max_chars, _fence_safe_end(text))
        if safe_end < self.min_chars:
            return None
        cut = self._best_boundary(text[:safe_end])
        if cut is not None:
            return cut
        if len(text) >= self.max_chars:
            return self._hard_cut(text)
        return None

    def _best_boundary(self, window: str) -> Optional[int]:
        """按优先级在窗口内选边界; 每一级里取最贴近 target 的切点."""
        for token, extra in ((PARAGRAPH_BOUNDARY, len(PARAGRAPH_BOUNDARY)), (LINE_BOUNDARY, 1)):
            cut = self._closest_token(window, token, extra)
            if cut is not None:
                return cut
        cut = self._closest_chars(window, SENTENCE_ENDINGS)
        if cut is not None:
            return cut
        # 逗号只在窗口明显偏长时才作为边界, 避免切得太碎
        if len(window) >= self.min_chars * 1.5:
            cut = self._closest_chars(window, COMMA_ENDINGS)
            if cut is not None:
                return cut
        return None

    def _closest_token(self, window: str, token: str, extra: int) -> Optional[int]:
        positions: list[int] = []
        start = 0
        while True:
            index = window.find(token, start)
            if index < 0:
                break
            positions.append(index + extra)
            start = index + 1
        return self._pick_closest(positions)

    def _closest_chars(self, window: str, chars: str) -> Optional[int]:
        return self._pick_closest([index + 1 for index, char in enumerate(window) if char in chars])

    def _pick_closest(self, positions: list[int]) -> Optional[int]:
        valid = [pos for pos in positions if self.min_chars <= pos <= self.max_chars]
        if not valid:
            return None
        # 先看谁离 target 最近; 距离相同时取更靠后的(块更饱满)
        return min(valid, key=lambda pos: (abs(pos - self.target_chars), -pos))

    def _hard_cut(self, text: str) -> int:
        """无自然边界且超长时的兜底: 在 max_chars 附近回退到最近的空白/标点."""
        limit = min(self.max_chars, len(text))
        floor = max(self.min_chars, limit - _HARD_CUT_LOOKBACK)
        for probe in range(limit - 1, floor - 1, -1):
            if text[probe] in _HARD_CUT_SAFE_CHARS:
                return probe + 1
        return limit


def _even_cut(text: str, goal: int) -> int:
    """在 ``goal`` 附近找自然边界作为收尾分块切点; 找不到就按 goal 硬切(回退空白/标点)."""
    if goal <= 0:
        return len(text)
    upper = min(len(text), int(goal))
    if upper <= 0:
        return len(text)
    lower = max(1, upper - _HARD_CUT_LOOKBACK)
    candidates: list[int] = []
    start = 0
    while True:
        index = text.find(LINE_BOUNDARY, start)
        if index < 0 or index >= upper:
            break
        candidates.append(index + 1)
        start = index + 1
    candidates.extend(
        index + 1
        for index, char in enumerate(text[:upper])
        if char in SENTENCE_ENDINGS or char in COMMA_ENDINGS
    )
    valid = [pos for pos in candidates if lower <= pos <= upper]
    if valid:
        return max(valid)  # 最贴近 goal 的自然边界
    for probe in range(upper - 1, lower - 1, -1):
        if text[probe] in _HARD_CUT_SAFE_CHARS:
            return probe + 1
    return upper


def split_evenly(
    text: str,
    *,
    pieces: int,
    target_chars: int,
    max_chars: int,
    min_chars: int = 1,
) -> list[str]:
    """把收尾剩余文本**尽量均分**成 ``pieces`` 块(优先自然边界; 每块 ≤ ``max_chars``).

    块大小贴着 ``len(text)/pieces`` 走, 避免"最后一条塞进一大段"。
    ``target_chars`` 只用于决定 pieces 的上界(调用方据此限流), 这里的均分以
    "平均长度"为准; 当总量远超上限时, 前 ``pieces-1`` 块各取 ``max_chars``,
    其余并入最后一块(显式上限带来的安全阀)。
    """
    if not text:
        return []
    pieces = max(1, int(pieces))
    if pieces == 1:
        # 安全阀: 配额用尽时剩余内容整体作为最后一条(不再按 max_chars 拆分,
        # 否则会突破 block_max_messages 的"总气泡上限")
        return [text]

    blocks: list[str] = []
    buffer = text
    while buffer and len(blocks) < pieces - 1:
        remaining_pieces = pieces - len(blocks)
        goal = max(1, math.ceil(len(buffer) / remaining_pieces))
        goal = max(min_chars, min(max_chars, goal))
        cut = _even_cut(buffer, goal)
        cut = max(1, min(cut, len(buffer)))
        blocks.append(buffer[:cut])
        buffer = buffer[cut:]
    if buffer:
        blocks.append(buffer)
    return blocks


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
    "split_evenly",
]
