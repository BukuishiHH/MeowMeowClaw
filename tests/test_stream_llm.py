"""流式 LLM 契约测试(S1): ``LLMProvider.chat_stream()`` 默认回退 + OpenAI 兼容流式实现.

覆盖:
- 未覆写 ``chat_stream`` 的 Provider(含测试替身) 自动降级为"一个 StreamFinal";
- 流式请求参数(``stream=True`` / ``stream_options``)、400 去掉 stream_options 重试;
- 文本增量、``tool_calls`` 按 index 分片拼装、``reasoning_content`` 不外发、usage 尾块;
- 空流回退非流式、流中断保留已生成文本、缺模型名的统一错误响应。
"""

import asyncio
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from meowmeowclaw.llm.base import (
    FINISH_REASON_ERROR,
    FINISH_REASON_STOP,
    FINISH_REASON_TOOL_CALLS,
    LLMProvider,
    LLMResponse,
    StreamFinal,
    StreamTextDelta,
)
from meowmeowclaw.llm.openai_compat import OpenAICompatProvider

MODULE = "meowmeowclaw.llm.openai_compat"
API_BASE = "http://localhost:8000/v1"
MESSAGES = [{"role": "user", "content": "你好"}]
TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]
DEFAULT_USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


# --------------------------------------------------------------------- 构造工具


class _BadRequest(Exception):
    status_code = 400


class OnlyChatProvider(LLMProvider):
    """只实现 chat() 的最小 Provider: 用于验证 chat_stream 默认回退."""

    def __init__(self) -> None:
        self.response = LLMResponse(content="回退回答", finish_reason=FINISH_REASON_STOP)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, tools=None, model=None, max_tokens=None) -> LLMResponse:
        self.calls.append(
            {"messages": messages, "tools": tools, "model": model, "max_tokens": max_tokens}
        )
        return self.response


def make_chunk(
    delta: Optional[dict[str, Any]] = None,
    *,
    finish_reason: Optional[str] = None,
    usage: Optional[dict[str, Any]] = None,
    include_choices: bool = True,
) -> ChatCompletionChunk:
    payload: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "qwen3",
        "choices": (
            [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}]
            if include_choices
            else []
        ),
    }
    if usage is not None:
        payload["usage"] = usage
    return ChatCompletionChunk.model_validate(payload)


def text_chunk(text: str) -> ChatCompletionChunk:
    return make_chunk({"role": "assistant", "content": text})


def done_chunk(finish_reason: str = "stop") -> ChatCompletionChunk:
    return make_chunk({}, finish_reason=finish_reason)


def tool_chunk(
    index: int,
    *,
    call_id: Optional[str] = None,
    name: Optional[str] = None,
    arguments: Optional[str] = None,
    finish_reason: Optional[str] = None,
) -> ChatCompletionChunk:
    call: dict[str, Any] = {"index": index, "type": "function"}
    if call_id is not None:
        call["id"] = call_id
    function: dict[str, Any] = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    if function:
        call["function"] = function
    return make_chunk({"tool_calls": [call]}, finish_reason=finish_reason)


def completion(content: str = "回退回答", finish_reason: str = "stop") -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-2",
            "object": "chat.completion",
            "created": 1,
            "model": "qwen3",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": DEFAULT_USAGE,
        }
    )


async def stream_of(*chunks: ChatCompletionChunk):
    for chunk in chunks:
        yield chunk


def make_client(**kwargs: Any) -> MagicMock:
    client = MagicMock(name="AsyncOpenAI")
    client.chat.completions.create = AsyncMock(**kwargs)
    client.close = AsyncMock()
    return client


def make_provider(client: MagicMock, **kwargs: Any) -> OpenAICompatProvider:
    params: dict[str, Any] = {"api_key": "sk-test", "base_url": API_BASE, "model": "qwen3"}
    params.update(kwargs)
    with patch(f"{MODULE}.AsyncOpenAI", return_value=client):
        return OpenAICompatProvider(**params)


async def collect(provider: LLMProvider, **kwargs: Any) -> tuple[list[str], Optional[LLMResponse]]:
    """跑完一次 chat_stream, 返回 (文本增量列表, 最终响应)."""
    deltas: list[str] = []
    final: Optional[LLMResponse] = None
    async for event in provider.chat_stream(MESSAGES, **kwargs):
        if isinstance(event, StreamTextDelta):
            deltas.append(event.text)
        else:
            assert isinstance(event, StreamFinal)
            final = event.response
    return deltas, final


def call_kwargs(client: MagicMock) -> dict[str, Any]:
    return client.chat.completions.create.await_args.kwargs


# --------------------------------------------------------------- 默认回退契约


class TestDefaultFallback:
    @pytest.mark.asyncio
    async def test_provider_without_stream_override_yields_single_final(self):
        provider = OnlyChatProvider()

        deltas, final = await collect(provider, tools=TOOL_DEFS, model="m", max_tokens=64)

        assert deltas == []
        assert final is provider.response  # 同一个对象, 不做改写
        assert provider.calls == [
            {"messages": MESSAGES, "tools": TOOL_DEFS, "model": "m", "max_tokens": 64}
        ]

    @pytest.mark.asyncio
    async def test_stream_events_are_frozen_dataclasses(self):
        delta = StreamTextDelta("x")
        final = StreamFinal(LLMResponse(content="y"))

        with pytest.raises(Exception):
            delta.text = "changed"  # type: ignore[misc]
        assert final.response.content == "y"

    def test_chat_stream_is_not_abstract(self):
        assert hasattr(LLMProvider, "chat_stream")
        assert "chat_stream" not in LLMProvider.__abstractmethods__


# --------------------------------------------------------------- 请求侧契约


class TestStreamRequestContract:
    @pytest.mark.asyncio
    async def test_stream_request_kwargs(self):
        client = make_client(return_value=stream_of(text_chunk("好"), done_chunk()))
        provider = make_provider(client)

        await collect(provider, tools=TOOL_DEFS)

        kwargs = call_kwargs(client)
        assert kwargs["stream"] is True
        assert kwargs["stream_options"] == {"include_usage": True}
        assert kwargs["model"] == "qwen3"
        assert kwargs["messages"] is MESSAGES
        assert kwargs["tools"] is TOOL_DEFS
        assert kwargs["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_no_tools_kwargs_when_tools_empty(self):
        client = make_client(return_value=stream_of(text_chunk("好"), done_chunk()))
        provider = make_provider(client)

        await collect(provider)

        assert set(call_kwargs(client)) == {
            "model",
            "messages",
            "stream",
            "stream_options",
        }

    @pytest.mark.asyncio
    async def test_bad_request_retries_without_stream_options(self):
        client = make_client(
            side_effect=[
                _BadRequest("unknown parameter"),
                stream_of(text_chunk("好"), done_chunk()),
            ]
        )
        provider = make_provider(client)

        deltas, final = await collect(provider)

        assert deltas == ["好"]
        assert final is not None and final.content == "好"
        assert client.chat.completions.create.await_count == 2
        retry_kwargs = client.chat.completions.create.await_args.kwargs
        assert "stream_options" not in retry_kwargs
        assert retry_kwargs["stream"] is True

    @pytest.mark.asyncio
    async def test_missing_model_yields_error_final(self):
        client = make_client(return_value=stream_of())
        provider = make_provider(client, model=None)

        deltas, final = await collect(provider)

        assert deltas == []
        assert final is not None and final.finish_reason == FINISH_REASON_ERROR
        client.chat.completions.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_open_failure_yields_error_final(self):
        client = make_client(side_effect=RuntimeError("connect failed"))
        provider = make_provider(client)

        deltas, final = await collect(provider)

        assert deltas == []
        assert final is not None
        assert final.finish_reason == FINISH_REASON_ERROR
        assert "connect failed" in (final.content or "")


# --------------------------------------------------------------- 响应解析


class TestStreamParsing:
    @pytest.mark.asyncio
    async def test_text_deltas_and_final_content(self):
        client = make_client(
            return_value=stream_of(text_chunk("你"), text_chunk("好"), done_chunk("stop"))
        )
        provider = make_provider(client)

        deltas, final = await collect(provider)

        assert deltas == ["你", "好"]
        assert final is not None
        assert final.content == "你好"
        assert final.finish_reason == FINISH_REASON_STOP
        assert final.has_tool_calls is False

    @pytest.mark.asyncio
    async def test_usage_tail_chunk_is_extracted(self):
        client = make_client(
            return_value=stream_of(
                text_chunk("好"),
                done_chunk("stop"),
                make_chunk(include_choices=False, usage=DEFAULT_USAGE),
            )
        )
        provider = make_provider(client)

        _, final = await collect(provider)

        assert final is not None and final.usage == DEFAULT_USAGE

    @pytest.mark.asyncio
    async def test_reasoning_content_not_emitted_but_kept(self):
        client = make_client(
            return_value=stream_of(
                make_chunk({"role": "assistant", "reasoning_content": "先想一下"}),
                text_chunk("答案"),
                done_chunk("stop"),
            )
        )
        provider = make_provider(client)

        deltas, final = await collect(provider)

        assert deltas == ["答案"]  # 思考过程不外发
        assert final is not None
        assert final.reasoning_content == "先想一下"
        assert final.content == "答案"

    @pytest.mark.asyncio
    async def test_tool_calls_split_across_chunks(self):
        client = make_client(
            return_value=stream_of(
                tool_chunk(0, call_id="call_1", name="read_file", arguments='{"file_'),
                tool_chunk(0, arguments='path": "a.py"}'),
                tool_chunk(1, call_id="call_2", name="list_dir"),
                tool_chunk(1, arguments='{"path": "."}', finish_reason="tool_calls"),
            )
        )
        provider = make_provider(client)

        deltas, final = await collect(provider, tools=TOOL_DEFS)

        assert deltas == []
        assert final is not None
        assert final.finish_reason == FINISH_REASON_TOOL_CALLS
        assert [call.id for call in final.tool_calls] == ["call_1", "call_2"]
        assert [call.name for call in final.tool_calls] == ["read_file", "list_dir"]
        assert final.tool_calls[0].arguments == {"file_path": "a.py"}
        assert final.tool_calls[1].arguments == {"path": "."}

    @pytest.mark.asyncio
    async def test_tool_call_without_finish_reason_defaults_to_tool_calls(self):
        client = make_client(
            return_value=stream_of(
                tool_chunk(0, call_id="call_1", name="read_file", arguments="{}"),
            )
        )
        provider = make_provider(client)

        _, final = await collect(provider)

        assert final is not None and final.finish_reason == FINISH_REASON_TOOL_CALLS

    @pytest.mark.asyncio
    async def test_invalid_tool_arguments_degrade_to_empty_dict(self):
        client = make_client(
            return_value=stream_of(tool_chunk(0, call_id="c1", name="read_file", arguments="{not json"))
        )
        provider = make_provider(client)

        _, final = await collect(provider)

        assert final is not None
        assert final.tool_calls[0].arguments == {}

    @pytest.mark.asyncio
    async def test_empty_stream_falls_back_to_non_streaming_chat(self):
        client = make_client(side_effect=[stream_of(), completion("回退回答")])
        provider = make_provider(client)

        deltas, final = await collect(provider)

        assert deltas == []
        assert final is not None and final.content == "回退回答"
        assert client.chat.completions.create.await_count == 2
        assert "stream" not in client.chat.completions.create.await_args.kwargs

    @pytest.mark.asyncio
    async def test_mid_stream_error_keeps_partial_text(self):
        async def broken_stream():
            yield text_chunk("已经生成的部分")
            raise RuntimeError("connection reset")

        client = make_client(return_value=broken_stream())
        provider = make_provider(client)

        deltas, final = await collect(provider)

        assert deltas == ["已经生成的部分"]
        assert final is not None
        assert final.finish_reason == FINISH_REASON_ERROR
        assert final.content is not None
        assert final.content.startswith("已经生成的部分")
        assert "流式输出中断" in final.content

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self):
        async def endless_stream():
            yield text_chunk("a")
            await asyncio.sleep(30)

        client = make_client(return_value=endless_stream())
        provider = make_provider(client)

        async def run() -> None:
            async for _ in provider.chat_stream(MESSAGES):
                pass

        task = asyncio.create_task(run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
