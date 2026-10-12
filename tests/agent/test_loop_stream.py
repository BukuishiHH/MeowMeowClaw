"""AgentLoop 流式增量与"工具轮前导抑制"测试(S1).

覆盖 docs/STREAMING_DESIGN.md §4.3:
- ``on_delta=None`` 时完全走非流式(绝不调用 chat_stream);
- 宽限(字符数 / 时间)放行、短回答结束时一次性补发;
- 工具轮旁白(先文本后 tool_calls)默认抑制; 已放行后才发现工具调用 → 保留 + warning;
- ``suppress_tool_preamble=False`` 时旁白照发;
- 渲染回调异常 / 流式实现异常都不影响本轮结果与完成态。
"""

import asyncio
from typing import Any, AsyncIterator, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from meowmeowclaw.agent.context import ContextBuilder
from meowmeowclaw.agent.loop import AgentLoop
from meowmeowclaw.llm.base import (
    FINISH_REASON_ERROR,
    FINISH_REASON_STOP,
    FINISH_REASON_TOOL_CALLS,
    LLMProvider,
    LLMResponse,
    StreamEvent,
    StreamFinal,
    StreamTextDelta,
    ToolCallRequest,
)
from meowmeowclaw.tools.registry import ToolRegistry

TOOL_DEFS = [
    {
        "type": "function",
        "function": {"name": "read_file", "description": "读文件", "parameters": {"type": "object"}},
    }
]


# --------------------------------------------------------------------- 测试替身


class StreamScriptProvider(LLMProvider):
    """按脚本产出流式事件的 Provider.

    每个脚本元素是 ``list[StreamEvent]`` 或 ``Exception``(模拟实现层抛异常);
    ``chat()`` 默认直接 AssertionError, 用于证明该轮确实走的是流式路径。
    """

    def __init__(
        self,
        *scripts: Any,
        chat_response: Optional[LLMResponse] = None,
        chat_error: bool = True,
    ) -> None:
        self._scripts = list(scripts)
        self.chat_response = chat_response
        self.chat_error = chat_error
        self.chat_calls = 0
        self.stream_calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=None) -> LLMResponse:
        self.chat_calls += 1
        if self.chat_error:
            raise AssertionError("本轮不应调用非流式 chat()")
        return self.chat_response or LLMResponse(content="非流式回答")

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[StreamEvent]:
        self.stream_calls += 1
        if not self._scripts:
            raise AssertionError("流式脚本已用尽")
        script = self._scripts.pop(0)
        if isinstance(script, BaseException):
            raise script
        for event in script:
            yield event


class ChatOnlyProvider(LLMProvider):
    """只实现 chat(); 用于验证 on_delta=None 的旧路径."""

    def __init__(self, answer: str = "非流式回答") -> None:
        self.answer = answer
        self.stream_calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=None) -> LLMResponse:
        return LLMResponse(content=self.answer, finish_reason=FINISH_REASON_STOP)

    async def chat_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.stream_calls += 1
        raise AssertionError("on_delta=None 不应走流式")


def make_registry() -> MagicMock:
    registry = MagicMock(spec=ToolRegistry)
    registry.get_definitions.return_value = TOOL_DEFS
    registry.list_tools.return_value = ["read_file"]
    registry.execute = AsyncMock(side_effect=lambda name, args: f"工具结果:{name}")
    return registry


def make_context() -> MagicMock:
    context = MagicMock(spec=ContextBuilder)
    context.build_messages.side_effect = lambda history=None, current_message="": (
        [{"role": "system", "content": "SYS"}]
        + (list(history) if history else [])
        + ([{"role": "user", "content": current_message}] if current_message else [])
    )
    return context


def make_loop(provider: LLMProvider, **kwargs: Any) -> AgentLoop:
    return AgentLoop(
        provider=provider, tools=make_registry(), context=make_context(), **kwargs
    )


def collect_deltas() -> tuple[list[str], Any]:
    chunks: list[str] = []

    async def on_delta(text: str) -> None:
        chunks.append(text)

    return chunks, on_delta


def tool_call(call_id: str = "call_1", name: str = "read_file") -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments={"file_path": "a.py"})


# --------------------------------------------------------------------- 用例


class TestNonStreamingPathUnchanged:
    @pytest.mark.asyncio
    async def test_on_delta_none_uses_chat_only(self):
        provider = ChatOnlyProvider("老路径回答")
        loop = make_loop(provider)

        turn = await loop.run_turn("问题", history=[])

        assert turn.answer == "老路径回答"
        assert turn.completed is True
        assert provider.stream_calls == 0  # 绝不触碰流式接口


class TestDeltaReleasing:
    @pytest.mark.asyncio
    async def test_long_answer_released_after_grace_chars(self):
        long_first = "A" * 50
        provider = StreamScriptProvider(
            [
                StreamTextDelta(long_first),
                StreamTextDelta("B" * 10),
                StreamFinal(LLMResponse(content=long_first + "B" * 10)),
            ]
        )
        loop = make_loop(provider, stream_preamble_grace_chars=40)
        chunks, on_delta = collect_deltas()

        turn = await loop.run_turn("问题", history=[], on_delta=on_delta)

        assert chunks == [long_first, "B" * 10]
        assert "".join(chunks) == turn.answer

    @pytest.mark.asyncio
    async def test_short_answer_flushed_once_at_end(self):
        provider = StreamScriptProvider(
            [StreamTextDelta("你好"), StreamTextDelta("呀"), StreamFinal(LLMResponse(content="你好呀"))]
        )
        loop = make_loop(provider)
        chunks, on_delta = collect_deltas()

        turn = await loop.run_turn("问题", history=[], on_delta=on_delta)

        assert chunks == ["你好呀"]  # 宽限内结束: 结束时一次性补发
        assert turn.answer == "你好呀"

    @pytest.mark.asyncio
    async def test_grace_ms_zero_releases_first_delta(self):
        provider = StreamScriptProvider(
            [StreamTextDelta("短"), StreamFinal(LLMResponse(content="短"))]
        )
        loop = make_loop(
            provider, stream_preamble_grace_chars=10**9, stream_preamble_grace_ms=0
        )
        chunks, on_delta = collect_deltas()

        await loop.run_turn("问题", history=[], on_delta=on_delta)

        assert chunks == ["短"]


class TestToolPreambleSuppression:
    @pytest.mark.asyncio
    async def test_preamble_before_tool_call_is_dropped(self):
        provider = StreamScriptProvider(
            # 第一轮: 短旁白 + 工具调用(旁白应被抑制)
            [
                StreamTextDelta("我先查一下"),
                StreamFinal(
                    LLMResponse(
                        content="我先查一下",
                        tool_calls=[tool_call()],
                        finish_reason=FINISH_REASON_TOOL_CALLS,
                    )
                ),
            ],
            # 第二轮: 最终回答
            [
                StreamTextDelta("答案是"),
                StreamTextDelta("42"),
                StreamFinal(LLMResponse(content="答案是42")),
            ],
        )
        loop = make_loop(provider)
        chunks, on_delta = collect_deltas()

        turn = await loop.run_turn("问题", history=[], on_delta=on_delta)

        assert turn.answer == "答案是42"
        assert turn.iterations == 2
        assert chunks == ["答案是42"]  # 旁白被丢弃, 只外发最终回答
        assert provider.stream_calls == 2

    @pytest.mark.asyncio
    async def test_long_preamble_then_tool_call_keeps_sent_text_and_warns(self, caplog):
        preamble = "查" * 60  # 超过宽限字符数, 已放行
        provider = StreamScriptProvider(
            [
                StreamTextDelta(preamble),
                StreamFinal(
                    LLMResponse(
                        content=preamble,
                        tool_calls=[tool_call()],
                        finish_reason=FINISH_REASON_TOOL_CALLS,
                    )
                ),
            ],
            [StreamTextDelta("最终"), StreamFinal(LLMResponse(content="最终"))],
        )
        loop = make_loop(provider, stream_preamble_grace_chars=40)
        chunks, on_delta = collect_deltas()

        with caplog.at_level("WARNING", logger="meowmeowclaw.agent.loop"):
            turn = await loop.run_turn("问题", history=[], on_delta=on_delta)

        assert chunks == [preamble, "最终"]  # 已放行的部分保留(QQ 无法编辑, 已知残余风险)
        assert turn.answer == "最终"
        assert any("前导文本已外发" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_suppression_disabled_sends_preamble(self):
        provider = StreamScriptProvider(
            [
                StreamTextDelta("我先查一下"),
                StreamFinal(
                    LLMResponse(
                        content="我先查一下",
                        tool_calls=[tool_call()],
                        finish_reason=FINISH_REASON_TOOL_CALLS,
                    )
                ),
            ],
            [StreamTextDelta("答案"), StreamFinal(LLMResponse(content="答案"))],
        )
        loop = make_loop(provider, suppress_tool_preamble=False)
        chunks, on_delta = collect_deltas()

        await loop.run_turn("问题", history=[], on_delta=on_delta)

        assert chunks == ["我先查一下", "答案"]


class TestStreamingFailureIsolation:
    @pytest.mark.asyncio
    async def test_callback_exception_does_not_break_turn(self):
        provider = StreamScriptProvider(
            [StreamTextDelta("A" * 50), StreamFinal(LLMResponse(content="A" * 50))]
        )
        loop = make_loop(provider, stream_preamble_grace_chars=10)

        async def broken_on_delta(text: str) -> None:
            raise RuntimeError("渲染层炸了")

        turn = await loop.run_turn("问题", history=[], on_delta=broken_on_delta)

        assert turn.completed is True
        assert turn.answer == "A" * 50

    @pytest.mark.asyncio
    async def test_stream_implementation_error_falls_back_to_chat(self):
        provider = StreamScriptProvider(
            RuntimeError("stream boom"),
            chat_response=LLMResponse(content="非流式兜底"),
            chat_error=False,
        )
        loop = make_loop(provider)
        chunks, on_delta = collect_deltas()

        turn = await loop.run_turn("问题", history=[], on_delta=on_delta)

        assert turn.answer == "非流式兜底"
        assert provider.chat_calls == 1

    @pytest.mark.asyncio
    async def test_error_final_keeps_prefix_invariant(self):
        provider = StreamScriptProvider(
            [
                StreamTextDelta("部分输出"),
                StreamFinal(
                    LLMResponse(
                        content="部分输出\n\n[流式输出中断: boom]", finish_reason=FINISH_REASON_ERROR
                    )
                ),
            ]
        )
        loop = make_loop(provider)
        chunks, on_delta = collect_deltas()

        turn = await loop.run_turn("问题", history=[], on_delta=on_delta)

        assert turn.completed is False
        assert turn.finish_reason == FINISH_REASON_ERROR
        # 已外发内容必须仍是最终文本的前缀(渲染层据此只补发中断标记)
        assert chunks == ["部分输出"]
        assert turn.answer.startswith("".join(chunks))
        assert "[流式输出中断" in turn.answer

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self):
        async def never_finishes() -> AsyncIterator[StreamEvent]:
            yield StreamTextDelta("开始")
            await asyncio.sleep(30)
            yield StreamFinal(LLMResponse(content="永不返回"))

        class SlowProvider(LLMProvider):
            async def chat(self, messages, tools=None, model=None, max_tokens=None) -> LLMResponse:
                raise AssertionError("不应调用")

            async def chat_stream(self, messages, tools=None, model=None, max_tokens=None):
                async for event in never_finishes():
                    yield event

        loop = make_loop(SlowProvider())
        chunks, on_delta = collect_deltas()

        task = asyncio.create_task(loop.run_turn("问题", history=[], on_delta=on_delta))
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
