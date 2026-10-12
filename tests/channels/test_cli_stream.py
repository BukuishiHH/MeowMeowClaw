"""CLI 流式渲染测试(S3): delta 逐字写出 + 最终只补后缀 + 关闭开关零变化.

对照 docs/STREAMING_DESIGN.md §4.8:
- ``stream=True`` 时 delta 立即写出(首块带 ``\\nMeowMeowClaw > `` 前缀), 最终 reply 只补剩余;
- 拼接结果必须等于完整回答(不重复、不缺失);
- ``stream=False`` 或没有 delta 时, 输出与旧版逐字节一致;
- delta 信封不唤醒 ``ask()`` 的 pending future, 只有 reply 才唤醒。
"""

import asyncio

import pytest

from meowmeowclaw.channels.cli_adapter import CliAdapter
from meowmeowclaw.channels.cli_policy import CLI_APP_NAME, CliPolicy
from meowmeowclaw.gateway import (
    INBOUND,
    STREAM_PHASE_DELTA,
    STREAM_PHASE_END,
    STREAM_PHASE_START,
    AsyncioQueueBus,
    Gateway,
    make_inbound,
    make_reply,
    make_stream_event,
    outbound_queue,
)
from meowmeowclaw.llm.base import (
    FINISH_REASON_STOP,
    LLMProvider,
    LLMResponse,
    StreamFinal,
    StreamTextDelta,
)
from meowmeowclaw.memory import SessionKey

from _qq_helpers import build_conversation


# --------------------------------------------------------------------- 替身


class StreamingProvider(LLMProvider):
    """按 parts 吐增量, 最终回答为 parts 拼接."""

    def __init__(self, parts: list[str]) -> None:
        self.parts = parts
        self.stream_calls = 0
        self.chat_calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=None) -> LLMResponse:
        self.chat_calls += 1
        return LLMResponse(content="".join(self.parts), finish_reason=FINISH_REASON_STOP)

    async def chat_stream(self, messages, tools=None, model=None, max_tokens=None):
        self.stream_calls += 1
        for part in self.parts:
            yield StreamTextDelta(part)
        yield StreamFinal(
            LLMResponse(content="".join(self.parts), finish_reason=FINISH_REASON_STOP)
        )


def make_adapter(**kwargs) -> tuple[CliAdapter, list[str], list[str]]:
    writes: list[str] = []
    lines: list[str] = []
    adapter = CliAdapter(
        write_func=writes.append,
        print_func=lines.append,
        **kwargs,
    )
    return adapter, writes, lines


def request(message_id: str = "m-1"):
    return make_inbound(channel="cli", text="问题", conversation_id="cli-1", message_id=message_id)


# ------------------------------------------------------------ 直接渲染契约


class TestCliStreamRendering:
    @pytest.mark.asyncio
    async def test_deltas_then_reply_write_answer_exactly_once(self):
        adapter, writes, lines = make_adapter(stream=True)
        req = request()

        await adapter.send(make_stream_event(req, phase=STREAM_PHASE_START))
        await adapter.send(make_stream_event(req, phase=STREAM_PHASE_DELTA, text="你好", seq=1))
        await adapter.send(make_stream_event(req, phase=STREAM_PHASE_DELTA, text="呀世界", seq=2))
        await adapter.send(make_stream_event(req, phase=STREAM_PHASE_END))
        await adapter.send(make_reply(req, "你好呀世界"))

        assert writes == [f"\n{CLI_APP_NAME} > ", "你好", "呀世界", "\n"]
        assert "".join(writes) == f"\n{CLI_APP_NAME} > 你好呀世界\n"
        assert lines == []  # 流式路径不再走整行打印
        assert adapter.assembler.size == 0  # reply 后清理水位线

    @pytest.mark.asyncio
    async def test_reply_only_sends_missing_suffix(self):
        adapter, writes, _ = make_adapter(stream=True)
        req = request()

        await adapter.send(make_stream_event(req, phase=STREAM_PHASE_DELTA, text="前缀", seq=1))
        await adapter.send(make_reply(req, "前缀+后缀"))

        assert "".join(writes) == f"\n{CLI_APP_NAME} > 前缀+后缀\n"
        assert writes[-2] == "+后缀"

    @pytest.mark.asyncio
    async def test_stream_disabled_ignores_deltas_and_keeps_legacy_output(self):
        adapter, writes, lines = make_adapter(stream=False)
        req = request()

        await adapter.send(make_stream_event(req, phase=STREAM_PHASE_START))
        await adapter.send(make_stream_event(req, phase=STREAM_PHASE_DELTA, text="你好", seq=1))
        await adapter.send(make_reply(req, "你好"))

        assert writes == []
        assert lines == [f"\n{CLI_APP_NAME} > 你好"]  # 与旧版逐字节一致

    @pytest.mark.asyncio
    async def test_stream_enabled_without_deltas_keeps_legacy_output(self):
        adapter, writes, lines = make_adapter(stream=True)
        req = request()

        await adapter.send(make_reply(req, "整段回答"))

        assert writes == []
        assert lines == [f"\n{CLI_APP_NAME} > 整段回答"]

    @pytest.mark.asyncio
    async def test_prefix_mismatch_resends_full_text(self, caplog):
        adapter, writes, _ = make_adapter(stream=True)
        req = request()

        await adapter.send(make_stream_event(req, phase=STREAM_PHASE_DELTA, text="旁白", seq=1))
        with caplog.at_level("WARNING", logger="meowmeowclaw.channels.streaming"):
            await adapter.send(make_reply(req, "最终回答"))

        # 兜底: 整段重发(接受一次重复), 保证内容不缺失
        assert "".join(writes) == f"\n{CLI_APP_NAME} > 旁白最终回答\n"
        assert any("前缀与最终文本不一致" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_empty_delta_text_ignored(self):
        adapter, writes, lines = make_adapter(stream=True)
        req = request()

        await adapter.send(make_stream_event(req, phase=STREAM_PHASE_DELTA, text="", seq=1))
        await adapter.send(make_reply(req, "回答"))

        assert writes == []
        assert lines == [f"\n{CLI_APP_NAME} > 回答"]


# ------------------------------------------------------------ ask() 配对语义


class TestCliAskWithStreaming:
    @pytest.mark.asyncio
    async def test_only_reply_resolves_pending_future(self):
        bus = AsyncioQueueBus()
        adapter, writes, lines = make_adapter(stream=True)
        await adapter.start(bus)
        try:
            task = asyncio.create_task(adapter.ask("你好"))
            request_envelope = await asyncio.wait_for(bus.get(INBOUND), timeout=1)
            bus.task_done(INBOUND)

            # delta 不唤醒 future
            await bus.publish(
                outbound_queue("cli"),
                make_stream_event(request_envelope, phase=STREAM_PHASE_DELTA, text="你", seq=1),
            )
            await asyncio.sleep(0.05)
            assert not task.done()

            await bus.publish(outbound_queue("cli"), make_reply(request_envelope, "你好"))
            reply = await asyncio.wait_for(task, timeout=2)
            assert reply is not None and reply.text == "你好"
            assert "".join(writes) == f"\n{CLI_APP_NAME} > 你好\n"
        finally:
            await adapter.stop()
            await bus.close()


# --------------------------------------------------------- 网关端到端(S3)


async def build_stream_stack(tmp_path, provider, *, outputs=None, writes=None, inputs=None):
    conversation = build_conversation(
        tmp_path / "memory", provider, stream_preamble_grace_chars=3
    )
    counter = {"n": 0}

    def factory() -> SessionKey:
        counter["n"] += 1
        return SessionKey(channel="cli", scope="session", conversation_id=f"cli-{counter['n']}")

    policy = CliPolicy(conversation, session_factory=factory)
    input_iter = iter(inputs or [])
    adapter = CliAdapter(
        input_func=(lambda prompt: next(input_iter)) if inputs is not None else None,
        print_func=(outputs.append if outputs is not None else (lambda text: None)),
        write_func=(writes.append if writes is not None else (lambda text: None)),
        stream=True,
    )
    gateway = Gateway(conversation=conversation, policies={"cli": policy}, stream_enabled=True)
    await gateway.start(adapters=[adapter])
    return gateway, adapter, policy, conversation


class TestCliGatewayStreamingEndToEnd:
    @pytest.mark.asyncio
    async def test_streaming_round_trip_writes_answer_once(self, tmp_path):
        provider = StreamingProvider(["一二三", "四五六"])
        writes: list[str] = []
        outputs: list[str] = []
        gateway, adapter, _, _ = await build_stream_stack(
            tmp_path, provider, writes=writes, outputs=outputs
        )
        try:
            reply = await adapter.ask("问题")
        finally:
            await gateway.stop()

        assert reply is not None and reply.text == "一二三四五六"
        assert provider.stream_calls == 1  # 确实走了 Provider 流式
        assert "".join(writes) == f"\n{CLI_APP_NAME} > 一二三四五六\n"
        assert outputs == []  # 未走整段打印
        assert writes[1] == "一二三" and writes[2] == "四五六"  # 真·增量(宽限=3)

    @pytest.mark.asyncio
    async def test_gateway_stream_disabled_keeps_legacy_path(self, tmp_path):
        provider = StreamingProvider(["整段回答"])
        writes: list[str] = []
        outputs: list[str] = []
        conversation = build_conversation(tmp_path / "memory", provider)
        policy = CliPolicy(
            conversation,
            session_factory=lambda: SessionKey(channel="cli", scope="session", conversation_id="c1"),
        )
        adapter = CliAdapter(
            print_func=outputs.append,
            write_func=writes.append,
            stream=True,  # 适配器开着, 但网关没开
        )
        gateway = Gateway(conversation=conversation, policies={"cli": policy})  # stream_enabled=False
        await gateway.start(adapters=[adapter])
        try:
            reply = await adapter.ask("问题")
        finally:
            await gateway.stop()

        assert reply is not None and reply.text == "整段回答"
        assert provider.stream_calls == 0  # 未走流式
        assert writes == []
        assert outputs == [f"\n{CLI_APP_NAME} > 整段回答"]
