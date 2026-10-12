"""A+ A1 指标单元测试: LatencyStats / StoreMetrics / measure.

对照 docs/A_PLUS_OPTIMIZATION.md §3.1 与 §4 测试策略:
分位口径(最近秩)、空样本、有界 deque(样本淘汰但计数累计)、快照字段、异常路径也记录。
"""

import pytest

from meowmeowclaw.memory.metrics import (
    DEFAULT_MAX_SAMPLES,
    LatencyStats,
    StoreMetrics,
    measure,
)


class TestLatencyStats:
    def test_empty_snapshot(self):
        assert LatencyStats().snapshot() == {
            "count": 0,
            "last": None,
            "avg": None,
            "p50": None,
            "p95": None,
        }

    def test_record_and_snapshot(self):
        stats = LatencyStats()
        for value in (10.0, 20.0, 30.0, 40.0):
            stats.record(value)

        snapshot = stats.snapshot()

        assert snapshot["count"] == 4
        assert snapshot["last"] == 40.0
        assert snapshot["avg"] == 25.0
        assert snapshot["p50"] == 20.0  # 最近秩: ceil(0.5*4)=2 → 第 2 个
        assert snapshot["p95"] == 40.0  # ceil(0.95*4)=4 → 第 4 个

    def test_percentile_nearest_rank(self):
        stats = LatencyStats()
        for value in range(1, 11):
            stats.record(float(value))

        assert stats.percentile(0.5) == 5.0
        assert stats.percentile(0.95) == 10.0  # ceil(9.5)=10
        assert stats.percentile(1.0) == 10.0
        assert stats.percentile(0.01) == 1.0

    def test_bounded_samples_but_count_keeps_growing(self):
        stats = LatencyStats(maxlen=3)
        for value in (1.0, 2.0, 3.0, 4.0, 5.0):
            stats.record(value)

        assert stats.count == 5
        assert stats.sample_count == 3
        assert stats.snapshot()["avg"] == 4.0  # 只统计最近 3 个样本

    def test_negative_value_clamped_to_zero(self):
        stats = LatencyStats()
        stats.record(-5.0)

        assert stats.snapshot()["last"] == 0.0

    def test_snapshot_values_are_rounded(self):
        stats = LatencyStats()
        stats.record(1.23456)

        assert stats.snapshot()["last"] == 1.235

    @pytest.mark.parametrize("q", [0.0, -0.1, 1.01])
    def test_invalid_quantile_rejected(self, q):
        stats = LatencyStats()
        stats.record(1.0)

        with pytest.raises(ValueError):
            stats.percentile(q)

    @pytest.mark.parametrize("maxlen", [0, -1])
    def test_invalid_maxlen_rejected(self, maxlen):
        with pytest.raises(ValueError):
            LatencyStats(maxlen=maxlen)

    def test_repr(self):
        stats = LatencyStats(maxlen=8)
        stats.record(1.0)

        text = repr(stats)

        assert "count=1" in text and "maxlen=8" in text


class TestStoreMetrics:
    def test_snapshot_has_three_paths(self):
        snapshot = StoreMetrics().snapshot()

        assert set(snapshot) == {"load_recent", "get_meta", "append_turn"}
        assert all(snapshot[name]["count"] == 0 for name in snapshot)

    def test_paths_are_independent(self):
        metrics = StoreMetrics()
        metrics.load_recent.record(1.0)
        metrics.get_meta.record(2.0)

        snapshot = metrics.snapshot()

        assert snapshot["load_recent"]["count"] == 1
        assert snapshot["get_meta"]["last"] == 2.0
        assert snapshot["append_turn"]["count"] == 0

    def test_default_maxlen(self):
        assert StoreMetrics().maxlen == DEFAULT_MAX_SAMPLES

    def test_repr_reports_counts(self):
        metrics = StoreMetrics()
        metrics.append_turn.record(1.0)

        assert "append_turn=1" in repr(metrics)


class TestMeasure:
    def test_records_elapsed(self):
        stats = LatencyStats()

        with measure(stats):
            pass

        assert stats.count == 1
        assert stats.snapshot()["last"] is not None

    def test_records_even_when_block_raises(self):
        stats = LatencyStats()

        with pytest.raises(RuntimeError):
            with measure(stats):
                raise RuntimeError("boom")

        assert stats.count == 1
