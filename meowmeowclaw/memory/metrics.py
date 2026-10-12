"""会话存储的轻量指标(A+ / A1): 有界延迟样本统计 + 三项主路径耗时.

设计见 ``docs/A_PLUS_OPTIMIZATION.md`` §3.1。硬约束:

- **纯观测**: 不参与任何业务判定, 默认不打印, 不改变既有行为;
- **有界**: 只用 ``collections.deque(maxlen=...)`` 保留最近样本, 不会随会话增长;
- **低开销**: 每次 ``record`` 是一次 deque append + 浮点加法(``perf_counter`` 纳秒级)。

用法::

    from meowmeowclaw.memory.metrics import StoreMetrics, measure

    metrics = StoreMetrics()
    with measure(metrics.load_recent):
        messages = await load(...)
    print(metrics.snapshot())
"""

import math
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Iterator, Optional

# 每个指标保留的样本数(分位统计用); 累计调用次数另外单独计数, 不受此上限影响
DEFAULT_MAX_SAMPLES = 256


class LatencyStats:
    """有界延迟样本统计: ``count / last / avg / p50 / p95``(单位毫秒).

    分位口径为**最近秩**(nearest-rank): 对 ``n`` 个样本排序后取第 ``ceil(q*n)`` 个
    (1-based, 越界取最后一个), **不做插值** —— 与常见延迟监控口径一致, 且实现无歧义。
    ``count`` 是累计调用次数, 与保留的样本数(最多 ``maxlen``)无关。
    """

    def __init__(self, maxlen: int = DEFAULT_MAX_SAMPLES) -> None:
        if maxlen <= 0:
            raise ValueError(f"maxlen 必须为正整数: {maxlen!r}")
        self.maxlen = int(maxlen)
        self._samples: "deque[float]" = deque(maxlen=self.maxlen)
        self._count = 0
        self._last: Optional[float] = None

    def __repr__(self) -> str:
        return (
            f"<LatencyStats count={self._count} samples={len(self._samples)} "
            f"maxlen={self.maxlen}>"
        )

    @property
    def count(self) -> int:
        """累计调用次数(含已被 maxlen 淘汰的样本)."""
        return self._count

    @property
    def sample_count(self) -> int:
        """当前保留的样本数(≤ maxlen)."""
        return len(self._samples)

    def record(self, elapsed_ms: float) -> None:
        """记录一次耗时(毫秒); 负值按 0 处理(时钟异常兜底)."""
        value = float(elapsed_ms)
        if value < 0:
            value = 0.0
        self._samples.append(value)
        self._count += 1
        self._last = value

    def percentile(self, q: float) -> Optional[float]:
        """最近秩分位; 无样本返回 None; ``q`` 必须落在 (0, 1]."""
        if not 0 < q <= 1:
            raise ValueError(f"分位 q 必须落在 (0, 1]: {q!r}")
        if not self._samples:
            return None
        ordered = sorted(self._samples)
        rank = max(1, math.ceil(q * len(ordered)))
        return round(ordered[min(rank, len(ordered)) - 1], 3)

    def snapshot(self) -> dict[str, Any]:
        """快照: ``{count, last, avg, p50, p95}``; 无样本时除 count 外均为 None."""
        if not self._samples:
            return {"count": self._count, "last": None, "avg": None, "p50": None, "p95": None}
        return {
            "count": self._count,
            "last": round(float(self._last or 0.0), 3),
            "avg": round(sum(self._samples) / len(self._samples), 3),
            "p50": self.percentile(0.5),
            "p95": self.percentile(0.95),
        }


class StoreMetrics:
    """``JsonlSessionStore`` 三项主路径的耗时与调用计数."""

    def __init__(self, maxlen: int = DEFAULT_MAX_SAMPLES) -> None:
        self.maxlen = int(maxlen)
        self.load_recent = LatencyStats(self.maxlen)
        self.get_meta = LatencyStats(self.maxlen)
        self.append_turn = LatencyStats(self.maxlen)

    def __repr__(self) -> str:
        return (
            f"<StoreMetrics load_recent={self.load_recent.count} "
            f"get_meta={self.get_meta.count} append_turn={self.append_turn.count}>"
        )

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            "load_recent": self.load_recent.snapshot(),
            "get_meta": self.get_meta.snapshot(),
            "append_turn": self.append_turn.snapshot(),
        }


@contextmanager
def measure(stats: LatencyStats) -> Iterator[None]:
    """给同步/异步代码块计时并写入 ``stats``(异常路径也会记录一次)."""
    started = time.perf_counter()
    try:
        yield
    finally:
        stats.record((time.perf_counter() - started) * 1000.0)


__all__ = ["DEFAULT_MAX_SAMPLES", "LatencyStats", "StoreMetrics", "measure"]
