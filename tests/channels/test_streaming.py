"""流式渲染基础件测试(S4): TypingKeepAlive 的启动/续期/停止与静默降级."""

import asyncio
import time

import pytest

from meowmeowclaw.channels.streaming import BlockSegmenter, TypingKeepAlive


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
    def test_paragraph_boundary_has_highest_priority(self):
        seg = BlockSegmenter(soft_chars=10, hard_chars=200)
        seg.push("第一段。\n\n第二段")

        assert seg.take() == "第一段。\n\n"
        assert seg.buffered_length == len("第二段")

    def test_line_boundary(self):
        seg = BlockSegmenter(soft_chars=10, hard_chars=200)
        seg.push("标题\n正文还在继续")

        assert seg.take() == "标题\n"

    def test_sentence_boundary(self):
        seg = BlockSegmenter(soft_chars=10, hard_chars=200)
        seg.push("第一句。第二句还没说完")

        assert seg.take() == "第一句。"

    def test_comma_only_when_buffer_long_enough(self):
        short = BlockSegmenter(soft_chars=10, hard_chars=200)
        short.push("一个短句，后面还有")
        assert short.take() is None  # 13 < 10*1.5

        long = BlockSegmenter(soft_chars=10, hard_chars=200)
        long.push("一二三四五六七八，九十十一十二十三")
        assert long.take() == "一二三四五六七八，"

    def test_no_boundary_returns_none(self):
        seg = BlockSegmenter(soft_chars=10, hard_chars=200)
        seg.push("没有任何标点的长文本")

        assert seg.has_boundary() is False
        assert seg.take() is None
        assert seg.buffered_length == len("没有任何标点的长文本")

    def test_hard_cut_when_too_long(self):
        seg = BlockSegmenter(soft_chars=10, hard_chars=20)
        seg.push("x" * 50)

        block = seg.take()

        assert block is not None and len(block) == 20
        assert seg.buffered_length == 30

    def test_hard_cut_prefers_whitespace(self):
        seg = BlockSegmenter(soft_chars=10, hard_chars=20)
        seg.push("aaaa bbbb cccc dddd eeee")

        block = seg.take()

        assert block == "aaaa bbbb cccc dddd "
        assert len(block) <= 20

    def test_unclosed_code_fence_blocks_cutting_inside(self):
        # 唯一边界(换行)位于未闭合围栏内部 → 不允许切
        seg = BlockSegmenter(soft_chars=10, hard_chars=1000)
        seg.push("```python\nprint('hi')\nprint('x')")

        assert seg.has_boundary() is False
        assert seg.take() is None

    def test_cut_before_fence_keeps_block_intact(self):
        # 允许在围栏**之前**的换行处切, 但围栏整体留在缓冲里, 不会被劈开
        seg = BlockSegmenter(soft_chars=10, hard_chars=1000)
        seg.push("说明：\n```python\nprint('hi')")

        assert seg.take() == "说明：\n"
        assert seg.flush() == "```python\nprint('hi')"

    def test_closed_code_fence_allows_cutting(self):
        seg = BlockSegmenter(soft_chars=10, hard_chars=1000)
        seg.push("说明：\n```python\nprint('hi')\n```\n后续")
        block = seg.take()

        assert block is not None and block.endswith("\n")
        assert "```" in block or "```" in seg.flush()  # 围栏前后不丢内容

    def test_flush_returns_remaining_and_clears(self):
        seg = BlockSegmenter(soft_chars=10, hard_chars=200)
        seg.push("没有边界的剩余内容")

        assert seg.flush() == "没有边界的剩余内容"
        assert seg.buffered_length == 0
        assert seg.flush() == ""

    def test_push_ignores_empty_text(self):
        seg = BlockSegmenter()
        seg.push("")
        assert seg.buffered_length == 0

    @pytest.mark.parametrize("kwargs", [{"soft_chars": 0}, {"soft_chars": -1}, {"hard_chars": 0}])
    def test_invalid_params_rejected(self, kwargs):
        with pytest.raises(ValueError):
            BlockSegmenter(**kwargs)

    def test_hard_chars_never_below_soft(self):
        assert BlockSegmenter(soft_chars=100, hard_chars=20).hard_chars == 100
