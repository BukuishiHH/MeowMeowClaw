"""流式渲染基础件测试(S4): TypingKeepAlive 的启动/续期/停止与静默降级."""

import asyncio

import pytest

from meowmeowclaw.channels.streaming import TypingKeepAlive


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
