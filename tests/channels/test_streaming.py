"""流式渲染基础件测试(S4): TypingKeepAlive 的启动/续期/停止与静默降级."""

import asyncio
import time

import pytest

from meowmeowclaw.channels.streaming import BlockSegmenter, TypingKeepAlive, split_evenly


async def wait_until(predicate, timeout: float = 2.0, interval: float = 0.005) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


class Recorder:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls = 0
        self.fail_times = fail_times

    async def __call__(self) -> bool:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("napcat 不支持 set_input_status")
        return True


class TestTypingKeepAlive:
    @pytest.mark.asyncio
    async def test_start_sends_immediately_and_refreshes(self):
        recorder = Recorder()
        keepalive = TypingKeepAlive(recorder, label="qq:1", refresh_seconds=0.05)

        keepalive.start()
        assert await wait_until(lambda: recorder.calls >= 3)
        await keepalive.stop()

        assert keepalive.running is False
        assert recorder.calls >= 3

    @pytest.mark.asyncio
    async def test_stop_is_idempotent_and_freezes_calls(self):
        recorder = Recorder()
        keepalive = TypingKeepAlive(recorder, label="qq:1", refresh_seconds=0.02)
        keepalive.start()
        assert await wait_until(lambda: recorder.calls >= 1)

        await keepalive.stop()
        await keepalive.stop()  # 幂等
        calls_after_stop = recorder.calls
        await asyncio.sleep(0.1)

        assert recorder.calls == calls_after_stop
        assert keepalive.running is False

    @pytest.mark.asyncio
    async def test_failures_are_soft_and_retried(self):
        recorder = Recorder(fail_times=2)
        keepalive = TypingKeepAlive(recorder, label="qq:1", refresh_seconds=0.02)

        keepalive.start()
        # 前两次失败不应终止任务, 第三次成功后持续续期
        assert await wait_until(lambda: recorder.calls >= 4, timeout=2.0)
        await keepalive.stop()

        assert keepalive.sent_count >= 1

    @pytest.mark.asyncio
    async def test_start_twice_does_not_duplicate_task(self):
        recorder = Recorder()
        keepalive = TypingKeepAlive(recorder, label="qq:1", refresh_seconds=0.05)

        keepalive.start()
        keepalive.start()
        assert await wait_until(lambda: recorder.calls >= 1)
        first_task = keepalive._task
        keepalive.start()

        assert keepalive._task is first_task
        await keepalive.stop()

    @pytest.mark.asyncio
    async def test_stop_never_blocks_when_action_swallows_cancellation(self):
        """回归(S5): 底层 SDK 在 await 点吞掉取消时, stop() 也必须有限返回.

        真实场景: websockets 的 send()/动作等待期间取消可能不生效,
        而 stop() 是在适配器出站循环里调用的 —— 一旦无限等待会冻结整条 QQ 回复链路。
        """
        calls: list[int] = []

        async def stubborn() -> bool:
            calls.append(1)
            try:
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)  # 模拟"吞掉取消": 当前动作继续跑完
            return True

        keepalive = TypingKeepAlive(stubborn, label="stubborn", refresh_seconds=30, stop_timeout=0.1)
        keepalive.start()
        await asyncio.sleep(0.05)  # 确保进入 action

        started = time.monotonic()
        await keepalive.stop()
        elapsed = time.monotonic() - started

        assert elapsed < 0.5  # 有限等待, 不阻塞调用方
        await asyncio.sleep(0.5)  # 给被放弃的任务自然收尾
        assert calls == [1]  # 停止后不再发起新的动作
        assert keepalive.running is False

    @pytest.mark.asyncio
    async def test_stop_is_idempotent_after_cooperative_shutdown(self):
        recorder = Recorder()
        keepalive = TypingKeepAlive(recorder, label="x", refresh_seconds=30)

        await keepalive.stop()  # 未启动时也安全
        keepalive.start()
        assert await wait_until(lambda: recorder.calls >= 1)
        await keepalive.stop()
        await keepalive.stop()

        assert keepalive.running is False

    def test_invalid_refresh_interval_rejected(self):
        with pytest.raises(ValueError):
            TypingKeepAlive(Recorder(), refresh_seconds=0)

    @pytest.mark.asyncio
    async def test_repr_reports_state(self):
        keepalive = TypingKeepAlive(Recorder(), label="qq:1", refresh_seconds=1.0)

        assert "qq:1" in repr(keepalive)
        keepalive.start()
        assert await wait_until(lambda: keepalive.sent_count >= 1)
        assert "running=True" in repr(keepalive)
        await keepalive.stop()


# --------------------------------------------------------- 语义分段(S5)


class TestBlockSegmenter:
    def test_returns_none_below_min_chars(self):
        seg = BlockSegmenter(min_chars=80, target_chars=150, max_chars=260)
        seg.push("短句。")

        assert seg.has_boundary() is False
        assert seg.take() is None
        assert seg.buffered_length == len("短句。")

    def test_block_size_stays_within_window_near_target(self):
        """核心回归: 每块都落在 [min, max] 且贴近 target(不再"小头 + 巨尾")."""
        seg = BlockSegmenter(min_chars=80, target_chars=150, max_chars=260)
        seg.push("一二三四五。" * 100)  # 每句 6 字, 共 600 字

        blocks: list[str] = []
        while True:
            block = seg.take()
            if block is None:
                break
            blocks.append(block)

        assert blocks
        assert all(80 <= len(block) <= 260 for block in blocks)
        # 目标 150 → 除最后一块外都应贴近 target, 而不是一路贴到 260 上限
        assert all(len(block) <= 165 for block in blocks[:-1])

    def test_picks_boundary_closest_to_target(self):
        seg = BlockSegmenter(min_chars=80, target_chars=150, max_chars=260)
        # 句末切点: 85 / 186 / 287(>max) → 目标 150 → 取 186
        seg.push("甲" * 84 + "。" + "乙" * 100 + "。" + "丙" * 100)

        assert len(seg.take()) == 186

    def test_paragraph_boundary_has_priority(self):
        seg = BlockSegmenter(min_chars=10, target_chars=100, max_chars=300)
        seg.push("甲" * 30 + "\n\n" + "乙" * 100 + "。" + "丙" * 100)

        block = seg.take()

        assert block.endswith("\n\n")  # 段落边界优先于更靠后的句末边界
        assert len(block) == 32

    def test_comma_only_when_window_long_enough(self):
        seg = BlockSegmenter(min_chars=10, target_chars=100, max_chars=300)
        seg.push("甲" * 10 + "，")  # 11 字 < min*1.5=15, 且无其他边界
        assert seg.take() is None

        seg.push("乙" * 10)  # 21 字 >= 15 → 逗号可作为边界
        assert seg.take() == "甲" * 10 + "，"

    def test_hard_cut_when_no_boundary_at_max(self):
        seg = BlockSegmenter(min_chars=10, target_chars=40, max_chars=50)
        seg.push("x" * 120)

        block = seg.take()

        assert block is not None and len(block) == 50

    def test_hard_cut_prefers_whitespace(self):
        seg = BlockSegmenter(min_chars=10, target_chars=30, max_chars=30)
        seg.push("x" * 20 + " " + "x" * 20)

        block = seg.take()

        assert block == "x" * 20 + " "  # 上限处回退到空白, 不切断长串
        assert len(block) <= 30

    def test_unclosed_code_fence_blocks_cutting_inside(self):
        seg = BlockSegmenter(min_chars=10, target_chars=40, max_chars=1000)
        seg.push("```python\nprint('hi')\nprint('x')")

        assert seg.has_boundary() is False
        assert seg.take() is None

    def test_cut_before_fence_keeps_block_intact(self):
        seg = BlockSegmenter(min_chars=4, target_chars=20, max_chars=1000)
        seg.push("说明：\n```python\nprint('hi')")

        assert seg.take() == "说明：\n"
        assert seg.flush() == "```python\nprint('hi')"

    def test_flush_returns_remaining_and_clears(self):
        seg = BlockSegmenter(min_chars=10, target_chars=40, max_chars=200)
        seg.push("没有边界的剩余内容")

        assert seg.flush() == "没有边界的剩余内容"
        assert seg.buffered_length == 0
        assert seg.flush() == ""

    def test_push_ignores_empty_text(self):
        seg = BlockSegmenter()
        seg.push("")
        assert seg.buffered_length == 0

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"min_chars": 0},
            {"target_chars": 0},
            {"max_chars": 0},
        ],
    )
    def test_invalid_params_rejected(self, kwargs):
        with pytest.raises(ValueError):
            BlockSegmenter(**kwargs)

    def test_target_and_max_are_clamped_to_min(self):
        seg = BlockSegmenter(min_chars=100, target_chars=20, max_chars=30)

        assert seg.target_chars == 100
        assert seg.max_chars == 100

    def test_repr_reports_window(self):
        text = repr(BlockSegmenter(min_chars=80, target_chars=150, max_chars=260))

        assert "min=80" in text and "target=150" in text and "max=260" in text


class TestSplitEvenly:
    def test_even_pieces(self):
        blocks = split_evenly(
            "字" * 722, pieces=5, target_chars=150, max_chars=260, min_chars=80
        )

        assert len(blocks) == 5
        assert all(abs(len(block) - 145) <= 1 for block in blocks)
        assert "".join(blocks) == "字" * 722

    def test_prefers_natural_boundary(self):
        text = "第一句。第二句。第三句。第四句。第五句。第六句。第七句。第八句。"
        blocks = split_evenly(text, pieces=2, target_chars=16, max_chars=260, min_chars=4)

        assert "".join(blocks) == text
        assert all(block.endswith("。") for block in blocks)  # 都切在句末

    def test_single_piece_keeps_text_whole(self):
        assert split_evenly("短回答。", pieces=1, target_chars=150, max_chars=260) == ["短回答。"]

    def test_empty_text(self):
        assert split_evenly("", pieces=3, target_chars=150, max_chars=260) == []

    def test_respects_max_chars(self):
        blocks = split_evenly(
            "x" * 2000, pieces=3, target_chars=150, max_chars=260, min_chars=80
        )

        assert "".join(blocks) == "x" * 2000
        assert all(len(block) <= 260 for block in blocks[:-1])  # 末块承载安全阀剩余
