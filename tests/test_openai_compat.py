"""meowmeowclaw/llm/openai_compat.py 的 Mock 单元测试.

测试策略:
- 用 ``MagicMock`` 顶替 ``AsyncOpenAI`` 客户端(``client.chat.completions.create`` 为 ``AsyncMock``),
  从而在不发真实网络请求的前提下断言"请求侧契约"(传了哪些参数)与"异常路径";
- 响应侧不手工造假的 dict, 而是用真实 SDK 的 ``ChatCompletion.model_validate()`` 构造响应对象,
  保证被测代码面对的是真正的 SDK 数据结构(pydantic extra="allow" 等行为都是真的);
  只有 SDK 校验不通过的畸形响应(如 finish_reason=None / 缺 message)才用 SimpleNamespace 模拟网关异常返回;
- 端到端: 把 LLM 返回的 tool_call 经 ToolCallRequest 送进真实 ToolRegistry + 真实 ReadFileTool,
  验证 "LLM JSON -> ToolCallRequest -> registry -> 工具" 整条链路.

运行: pytest tests/test_openai_compat.py -v
"""

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import openai
import pytest
from openai.types.chat import ChatCompletion

from meowmeowclaw.llm.base import (
    FINISH_REASON_ERROR,
    FINISH_REASON_STOP,
    FINISH_REASON_TOOL_CALLS,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
)
from meowmeowclaw.llm.openai_compat import OpenAICompatProvider
from meowmeowclaw.tools.filesystem import ReadFileTool
from meowmeowclaw.tools.registry import ToolRegistry

MODULE = "meowmeowclaw.llm.openai_compat"
API_BASE = "http://localhost:8000/v1"
MESSAGES = [{"role": "user", "content": "帮我读一下 a.py"}]
TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            "strict": False,
        },
    }
]
DEFAULT_USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


# ------------------------------------------------------- 用真实 SDK 类型造响应


def openai_tool_call(
    call_id: str = "call_1",
    name: str = "read_file",
    arguments: Any = '{"file_path": "a.py"}',
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """构造 OpenAI 响应里的 tool_call 原始结构(可附加 reasoning_content 等私有字段)."""
    payload: dict[str, Any] = {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }
    if extra:
        payload.update(extra)
    return payload


def build_completion(
    *,
    content: Optional[str] = None,
    tool_calls: Optional[list[dict[str, Any]]] = None,
    finish_reason: str = "stop",
    usage: Optional[dict[str, Any]] = None,
    message_extra: Optional[dict[str, Any]] = None,
) -> ChatCompletion:
    """用真实 SDK 校验器构造 ChatCompletion, 确保响应的数据结构与线上完全一致."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if message_extra:
        message.update(message_extra)

    payload: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "qwen3",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
    }
    if usage is not None:
        payload["usage"] = usage
    return ChatCompletion.model_validate(payload)


def make_client(
    *, completion: Optional[ChatCompletion] = None, error: Optional[BaseException] = None
) -> MagicMock:
    """构造替身 AsyncOpenAI 客户端, create() 是 AsyncMock, 可直接断言/注入返回值或异常."""
    client = MagicMock(name="AsyncOpenAI")
    client.chat.completions.create = AsyncMock(return_value=completion, side_effect=error)
    client.close = AsyncMock()
    return client


def make_provider(client: MagicMock, **kwargs: Any) -> OpenAICompatProvider:
    """在 AsyncOpenAI 被替换成 Mock 的前提下构造 Provider."""
    params: dict[str, Any] = {"api_key": "sk-test", "base_url": API_BASE, "model": "qwen3"}
    params.update(kwargs)
    with patch(f"{MODULE}.AsyncOpenAI", return_value=client):
        return OpenAICompatProvider(**params)


@pytest.fixture
def client() -> MagicMock:
    return make_client()


@pytest.fixture
def provider(client) -> OpenAICompatProvider:
    return make_provider(client)


def call_kwargs(client: MagicMock) -> dict[str, Any]:
    """取出最近一次 create() 收到的关键字实参."""
    return client.chat.completions.create.await_args.kwargs


# ------------------------------------------------------------------ 构造与客户端


class TestConstruction:
    def test_inherits_llm_provider(self, provider):
        assert isinstance(provider, LLMProvider)
        assert OpenAICompatProvider.__abstractmethods__ == frozenset()

    def test_passes_api_key_and_base_url_to_sdk_client(self, client):
        with patch(f"{MODULE}.AsyncOpenAI", return_value=client) as ctor:
            OpenAICompatProvider(api_key="sk-a", base_url="http://x/v1", model="m")

        ctor.assert_called_once_with(api_key="sk-a", base_url="http://x/v1")

    def test_none_credentials_are_forwarded_so_sdk_env_fallback_works(self, client):
        with patch(f"{MODULE}.AsyncOpenAI", return_value=client) as ctor:
            OpenAICompatProvider()

        ctor.assert_called_once_with(api_key=None, base_url=None)

    def test_model_is_kept_as_default(self, provider):
        assert provider.model == "qwen3"

    def test_missing_credentials_fail_fast_at_construction(self, monkeypatch):
        # 真实 SDK 行为: 既无 api_key 参数也无 OPENAI_API_KEY 环境变量时, 构造阶段即报错
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        with pytest.raises(openai.OpenAIError):
            OpenAICompatProvider(api_key=None, base_url=API_BASE, model="qwen3")

    def test_repr_masks_api_key(self, client):
        text = repr(make_provider(client, api_key="sk-super-secret"))

        assert "sk-super-secret" not in text  # 密钥绝不外泄
        assert "***" in text
        assert "qwen3" in text

    def test_repr_without_api_key(self, client):
        assert "api_key=None" in repr(make_provider(client, api_key=None))

    @pytest.mark.asyncio
    async def test_aclose_releases_client(self, provider, client):
        await provider.aclose()

        client.close.assert_awaited_once_with()


# -------------------------------------------------- 请求契约(第 3 条: tools 传参)


class TestRequestContract:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("empty_tools", [None, []])
    async def test_no_tools_kwargs_when_tools_empty(self, provider, client, empty_tools):
        client.chat.completions.create.return_value = build_completion(content="hi")

        await provider.chat(MESSAGES, tools=empty_tools)

        kwargs = call_kwargs(client)
        assert set(kwargs) == {"model", "messages"}  # 空工具时绝不带 tools/tool_choice
        assert kwargs["model"] == "qwen3"
        assert kwargs["messages"] is MESSAGES  # 消息原样透传, 不做拷贝/改写

    @pytest.mark.asyncio
    async def test_tools_and_tool_choice_passed_when_tools_present(self, provider, client):
        client.chat.completions.create.return_value = build_completion(content="hi")

        await provider.chat(MESSAGES, tools=TOOL_DEFS)

        kwargs = call_kwargs(client)
        assert set(kwargs) == {"model", "messages", "tools", "tool_choice"}
        assert kwargs["tools"] is TOOL_DEFS
        assert kwargs["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_per_call_model_overrides_default(self, provider, client):
        client.chat.completions.create.return_value = build_completion(content="hi")

        await provider.chat(MESSAGES, model="qwen3-max")

        assert call_kwargs(client)["model"] == "qwen3-max"

    @pytest.mark.asyncio
    async def test_max_tokens_forwarded_when_present(self, provider, client):
        client.chat.completions.create.return_value = build_completion(content="hi")

        await provider.chat(MESSAGES, max_tokens=128)

        assert call_kwargs(client)["max_tokens"] == 128

    @pytest.mark.asyncio
    async def test_max_tokens_absent_by_default(self, provider, client):
        client.chat.completions.create.return_value = build_completion(content="hi")

        await provider.chat(MESSAGES)

        assert "max_tokens" not in call_kwargs(client)

    @pytest.mark.asyncio
    async def test_missing_model_returns_error_without_calling_api(self, client):
        provider = make_provider(client, model=None)

        result = await provider.chat(MESSAGES)

        assert result.finish_reason == FINISH_REASON_ERROR
        assert result.content.startswith("[LLM调用失败] ValueError")
        client.chat.completions.create.assert_not_awaited()  # 参数不全就不该发请求

    @pytest.mark.asyncio
    async def test_chat_returns_llm_response(self, provider, client):
        client.chat.completions.create.return_value = build_completion(content="你好")

        result = await provider.chat(MESSAGES)

        assert isinstance(result, LLMResponse)
        assert result.content == "你好"
        assert result.finish_reason == FINISH_REASON_STOP


# ------------------------------------------- tool_calls 转换(第 4 条: reasoning)


class TestToolCallConversion:
    @pytest.mark.asyncio
    async def test_single_tool_call_is_converted(self, provider, client):
        client.chat.completions.create.return_value = build_completion(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[openai_tool_call()],
        )

        result = await provider.chat(MESSAGES, tools=TOOL_DEFS)

        assert result.has_tool_calls is True
        assert len(result.tool_calls) == 1
        call = result.tool_calls[0]
        assert isinstance(call, ToolCallRequest)
        assert call.id == "call_1"
        assert call.name == "read_file"
        assert call.arguments == {"file_path": "a.py"}  # JSON 字符串已解析成 dict
        assert call.reasoning_content is None

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_keep_order(self, provider, client):
        client.chat.completions.create.return_value = build_completion(
            finish_reason="tool_calls",
            tool_calls=[
                openai_tool_call("c1", "read_file", '{"file_path": "a.py"}'),
                openai_tool_call("c2", "list_dir", '{"dir_path": "src"}'),
            ],
        )

        result = await provider.chat(MESSAGES, tools=TOOL_DEFS)

        assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [
            ("c1", "read_file", {"file_path": "a.py"}),
            ("c2", "list_dir", {"dir_path": "src"}),
        ]

    @pytest.mark.asyncio
    async def test_no_tool_calls_gives_empty_list(self, provider, client):
        client.chat.completions.create.return_value = build_completion(content="纯文本")

        result = await provider.chat(MESSAGES)

        assert result.tool_calls == []
        assert result.has_tool_calls is False

    @pytest.mark.asyncio
    async def test_tool_call_level_reasoning_content(self, provider, client):
        client.chat.completions.create.return_value = build_completion(
            finish_reason="tool_calls",
            tool_calls=[openai_tool_call(extra={"reasoning_content": "tool_call 级思考"})],
        )

        result = await provider.chat(MESSAGES)

        assert result.tool_calls[0].reasoning_content == "tool_call 级思考"

    @pytest.mark.asyncio
    async def test_message_level_reasoning_content_is_used_as_fallback(self, provider, client):
        # 有些推理模型把思考过程放在 message 顶层, 而不是 tool_call 上
        client.chat.completions.create.return_value = build_completion(
            finish_reason="tool_calls",
            tool_calls=[openai_tool_call(), openai_tool_call("c2", "list_dir", "{}")],
            message_extra={"reasoning_content": "message 级思考"},
        )

        result = await provider.chat(MESSAGES)

        assert [c.reasoning_content for c in result.tool_calls] == ["message 级思考", "message 级思考"]

    @pytest.mark.asyncio
    async def test_tool_call_level_reasoning_wins_over_message_level(self, provider, client):
        client.chat.completions.create.return_value = build_completion(
            finish_reason="tool_calls",
            tool_calls=[
                openai_tool_call(extra={"reasoning_content": "tool_call 级"}),
                openai_tool_call("c2", "list_dir", "{}"),
            ],
            message_extra={"reasoning_content": "message 级"},
        )

        result = await provider.chat(MESSAGES)

        assert [c.reasoning_content for c in result.tool_calls] == ["tool_call 级", "message 级"]

    @pytest.mark.asyncio
    async def test_pure_text_reasoning_reply_keeps_reasoning_at_response_level(self, provider, client):
        # 无工具调用的推理回复: 思考文本挂到 LLMResponse.reasoning_content, 不再丢失
        client.chat.completions.create.return_value = build_completion(
            content="答案是 42",
            message_extra={"reasoning_content": "先算 6*7"},
        )

        result = await provider.chat(MESSAGES)

        assert result.content == "答案是 42"
        assert result.reasoning_content == "先算 6*7"
        assert result.has_tool_calls is False
        assert result.finish_reason == FINISH_REASON_STOP

    @pytest.mark.asyncio
    async def test_tool_call_reply_keeps_both_reasoning_levels(self, provider, client):
        client.chat.completions.create.return_value = build_completion(
            finish_reason="tool_calls",
            tool_calls=[openai_tool_call(extra={"reasoning_content": "call 级思考"})],
            message_extra={"reasoning_content": "message 级思考"},
        )

        result = await provider.chat(MESSAGES)

        assert result.finish_reason == FINISH_REASON_TOOL_CALLS
        assert result.reasoning_content == "message 级思考"  # 响应级
        assert result.tool_calls[0].reasoning_content == "call 级思考"  # 调用级

    @pytest.mark.asyncio
    async def test_no_reasoning_at_response_level_when_not_returned_by_model(self, provider, client):
        client.chat.completions.create.return_value = build_completion(content="普通回复")

        result = await provider.chat(MESSAGES)

        assert result.reasoning_content is None

    @pytest.mark.asyncio
    async def test_no_reasoning_content_anywhere(self, provider, client):
        client.chat.completions.create.return_value = build_completion(
            finish_reason="tool_calls", tool_calls=[openai_tool_call()]
        )

        result = await provider.chat(MESSAGES)

        assert result.tool_calls[0].reasoning_content is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw_arguments",
        [
            "",  # 空串
            "not-json",  # 非法 JSON
            '{"a": 1',  # 截断的 JSON
            "[1, 2]",  # 合法 JSON 但不是对象
            '"just-a-string"',  # 合法 JSON 但不是对象
        ],
    )
    async def test_unparsable_arguments_degrade_to_empty_dict(
        self, provider, client, raw_arguments, caplog
    ):
        client.chat.completions.create.return_value = build_completion(
            finish_reason="tool_calls", tool_calls=[openai_tool_call(arguments=raw_arguments)]
        )

        with caplog.at_level(logging.WARNING, logger=MODULE):
            result = await provider.chat(MESSAGES)

        assert result.tool_calls[0].arguments == {}  # 不炸整条响应
        assert all(c.reasoning_content is None for c in result.tool_calls)

    @pytest.mark.asyncio
    async def test_nested_json_arguments_preserved(self, provider, client):
        client.chat.completions.create.return_value = build_completion(
            finish_reason="tool_calls",
            tool_calls=[
                openai_tool_call(
                    name="write_file",
                    arguments='{"file_path": "a.py", "content": "喵", "meta": {"n": [1, 2]}}',
                )
            ],
        )

        result = await provider.chat(MESSAGES)

        assert result.tool_calls[0].arguments == {
            "file_path": "a.py",
            "content": "喵",
            "meta": {"n": [1, 2]},
        }

    @pytest.mark.asyncio
    async def test_missing_id_or_name_degrades_to_empty_string(self, provider, client):
        # SDK 模型强制要求 id / function.name, 这里用 SimpleNamespace 模拟不规范网关的返回
        stray_call = SimpleNamespace(function=SimpleNamespace(name=None, arguments="{}"))
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    message=SimpleNamespace(content=None, tool_calls=[stray_call]),
                )
            ],
            usage=None,
        )

        result = await provider.chat(MESSAGES)

        assert result.tool_calls[0].id == ""
        assert result.tool_calls[0].name == ""
        assert result.tool_calls[0].arguments == {}


# ---------------------------------------------------------- usage 提取(第 5 条)


class TestUsageExtraction:
    @pytest.mark.asyncio
    async def test_usage_is_converted_to_plain_dict(self, provider, client):
        client.chat.completions.create.return_value = build_completion(
            content="hi", usage=DEFAULT_USAGE
        )

        result = await provider.chat(MESSAGES)

        assert result.usage == DEFAULT_USAGE
        assert isinstance(result.usage, dict)

    @pytest.mark.asyncio
    async def test_usage_details_absent_keys_are_not_filled_with_none(self, provider, client):
        client.chat.completions.create.return_value = build_completion(
            content="hi", usage=DEFAULT_USAGE
        )

        result = await provider.chat(MESSAGES)

        # 没打开的 *_details 不应变成 null 噪声
        assert all(value is not None for value in result.usage.values())

    @pytest.mark.asyncio
    async def test_extra_usage_fields_from_gateway_are_kept(self, provider, client):
        gateway_usage = {**DEFAULT_USAGE, "reasoning_tokens": 7}
        client.chat.completions.create.return_value = build_completion(
            content="hi", usage=gateway_usage
        )

        result = await provider.chat(MESSAGES)

        assert result.usage["reasoning_tokens"] == 7

    @pytest.mark.asyncio
    async def test_missing_usage_gives_empty_dict(self, provider, client):
        client.chat.completions.create.return_value = build_completion(content="hi", usage=None)

        result = await provider.chat(MESSAGES)

        assert result.usage == {}


# ------------------------------------------------------------- 异常兜底(第 6 条)


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_api_exception_returns_error_response(self, provider, client, caplog):
        client.chat.completions.create.side_effect = openai.APIConnectionError(
            request=MagicMock(name="httpx.Request")
        )

        with caplog.at_level(logging.ERROR, logger=MODULE):
            result = await provider.chat(MESSAGES)

        assert isinstance(result, LLMResponse)
        assert result.finish_reason == FINISH_REASON_ERROR
        assert result.content == "[LLM调用失败] APIConnectionError: Connection error."
        assert result.tool_calls == []
        assert result.usage == {}
        assert result.reasoning_content is None
        assert result.has_tool_calls is False
        assert any(r.levelno == logging.ERROR for r in caplog.records)  # 有错误日志可排查

    @pytest.mark.asyncio
    async def test_generic_exception_is_wrapped_not_raised(self, provider, client):
        client.chat.completions.create.side_effect = RuntimeError("网关 502")

        result = await provider.chat(MESSAGES)

        assert result.finish_reason == FINISH_REASON_ERROR
        assert result.content == "[LLM调用失败] RuntimeError: 网关 502"

    @pytest.mark.asyncio
    async def test_cancellation_is_not_swallowed(self, provider, client):
        # CancelledError 继承 BaseException, 必须继续上抛, 否则 asyncio 任务无法被取消
        client.chat.completions.create.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await provider.chat(MESSAGES)

    @pytest.mark.asyncio
    async def test_empty_choices_returns_error_instead_of_index_error(self, provider, client):
        client.chat.completions.create.return_value = ChatCompletion.model_validate(
            {
                "id": "x",
                "object": "chat.completion",
                "created": 1,
                "model": "qwen3",
                "choices": [],
            }
        )

        result = await provider.chat(MESSAGES)

        assert result.finish_reason == FINISH_REASON_ERROR
        assert "choices 为空" in result.content

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "completion",
        [
            SimpleNamespace(choices=None, usage=None),  # 网关没给 choices
            SimpleNamespace(choices=[SimpleNamespace(finish_reason=None, message=None)]),  # 缺 message
        ],
    )
    async def test_malformed_gateway_response_returns_error(self, provider, client, completion):
        client.chat.completions.create.return_value = completion

        result = await provider.chat(MESSAGES)

        assert result.finish_reason == FINISH_REASON_ERROR
        assert result.content.startswith("[LLM调用失败] RuntimeError")

    @pytest.mark.asyncio
    async def test_missing_finish_reason_defaults_to_stop(self, provider, client):
        # SDK 的 Choice 不允许 finish_reason=None, 用 SimpleNamespace 模拟部分网关的缺失行为
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason=None, message=SimpleNamespace(content="hi", tool_calls=None))],
            usage=None,
        )

        result = await provider.chat(MESSAGES)

        assert result.content == "hi"
        assert result.finish_reason == FINISH_REASON_STOP


# ----------------------------------------------------------- 端到端: 打通工具链


class TestEndToEndWithToolRegistry:
    @pytest.mark.asyncio
    async def test_llm_tool_call_executes_real_tool(self, provider, client, tmp_path):
        (tmp_path / "note.txt").write_text("文件真实内容", encoding="utf-8")
        registry = ToolRegistry()
        registry.register(ReadFileTool(str(tmp_path)))

        client.chat.completions.create.return_value = build_completion(
            content=None,
            finish_reason="tool_calls",
            usage=DEFAULT_USAGE,
            tool_calls=[openai_tool_call(arguments='{"file_path": "note.txt"}')],
        )

        response = await provider.chat(MESSAGES, tools=registry.get_definitions())

        # 请求侧: 工具定义确实来自注册表, 并开了 auto
        kwargs = call_kwargs(client)
        assert kwargs["tools"] == registry.get_definitions()
        assert kwargs["tool_choice"] == "auto"

        # 响应侧: tool_call 可直接路由回真实工具执行
        assert response.finish_reason == FINISH_REASON_TOOL_CALLS  # 复数, 与 OpenAI 返回一致
        assert response.has_tool_calls is True
        call = response.tool_calls[0]
        assert call.name == "read_file"
        assert await registry.execute(call.name, call.arguments) == "文件真实内容"

    @pytest.mark.asyncio
    async def test_two_round_trip_conversation_shape(self, provider, client, tmp_path):
        (tmp_path / "a.py").write_text("print('hi')", encoding="utf-8")
        registry = ToolRegistry()
        registry.register(ReadFileTool(str(tmp_path)))

        tool_round = build_completion(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[openai_tool_call()],
        )
        final_round = build_completion(content="文件里是 print('hi')", finish_reason="stop")
        client.chat.completions.create.side_effect = [tool_round, final_round]

        messages = list(MESSAGES)
        first = await provider.chat(messages, tools=registry.get_definitions())
        assert first.has_tool_calls is True

        call = first.tool_calls[0]
        tool_result = await registry.execute(call.name, call.arguments)
        messages.append({"role": "tool", "tool_call_id": call.id, "content": tool_result})

        second = await provider.chat(messages, tools=registry.get_definitions())

        assert second.content == "文件里是 print('hi')"
        assert second.has_tool_calls is False
        assert client.chat.completions.create.await_count == 2
        # 第二轮请求带上了工具执行结果
        assert call_kwargs(client)["messages"][-1]["content"] == "print('hi')"
