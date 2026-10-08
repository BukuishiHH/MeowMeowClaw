"""meowmeowclaw/providers/base.py 的 Mock 单元测试.

被测对象:
- ``ToolCallRequest`` / ``LLMResponse`` 两个 dataclass 的数据契约;
- ``LLMProvider`` 抽象基类(异步 ``chat`` 接口).

测试策略:
- Mock 为主: 用 ``MagicMock(spec=LLMProvider)`` 与 ``create_autospec(LLMProvider, instance=True)``
  伪造 provider. 由于 ``chat`` 是 ``async def``, spec/autospec 会自动把它变成 ``AsyncMock``,
  因此可以验证"只依赖 LLMProvider 抽象的调用方可以无缝换成 Mock",
  并借 autospec 校验实参名必须符合抽象签名(参数改名即报错);
- 真实实现为辅: 同一个消费者函数分别用 Mock 与最小真实子类 ``FakeProvider`` 驱动,
  断言两者对外行为一致, 防止 Mock 失真;
- 数据契约: 默认值、可变默认值隔离、``__post_init__`` 归一化、``has_tool_calls`` 动态计算;
- 跨模块契约: ``ToolCallRequest.arguments`` 必须能 ``**`` 解包进 ``BaseTool.execute``.

说明: ``LLMResponse.tool_calls`` 显式传 None 时 ``has_tool_calls`` 会抛 TypeError, 见 TestKnownGaps.

运行: pytest tests/test_provider_base.py -v
"""

from dataclasses import asdict, fields, is_dataclass
from inspect import Parameter, iscoroutinefunction, signature
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from meowmeowclaw.tools import BaseTool
from meowmeowclaw.providers.base import (
    FINISH_REASON_CONTENT_FILTER,
    FINISH_REASON_ERROR,
    FINISH_REASON_LENGTH,
    FINISH_REASON_STOP,
    FINISH_REASON_TOOL_CALLS,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
)

# --------------------------------------------------------------- 测试替身与消费者


class FakeProvider(LLMProvider):
    """最小真实实现: 记录调用参数并返回预设响应, 用于给 Mock 做对照."""

    def __init__(self, response: Optional[LLMResponse] = None) -> None:
        self.response = response if response is not None else LLMResponse(content="fake")
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools, "model": model})
        return self.response


class ChatOnlyProvider(LLMProvider):
    """只实现 chat 的具体子类, 用于验证 ABC 约束."""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
    ) -> LLMResponse:
        return LLMResponse(content="ok")


async def call_provider(
    provider: LLMProvider,
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]] = None,
    model: Optional[str] = None,
) -> LLMResponse:
    """模拟未来 agent/loop.py 的调用方式: 只依赖抽象, 不关心具体实现."""
    return await provider.chat(messages, tools=tools, model=model)


def make_mock_provider(response: Optional[LLMResponse] = None) -> MagicMock:
    """构造符合 LLMProvider 接口的 Mock provider(chat 自动是 AsyncMock)."""
    provider = MagicMock(spec=LLMProvider)
    provider.chat.return_value = response if response is not None else LLMResponse(content="hi")
    return provider


@pytest.fixture
def messages() -> list[dict[str, Any]]:
    return [{"role": "user", "content": "你好"}]


# ------------------------------------------------------- Mock 契约自检(替身是否保真)


class TestMockProviderContract:
    """确认测试替身与真实 LLMProvider 契约一致, 避免 Mock 测试失真."""

    def test_magicmock_spec_is_llmprovider_instance(self):
        provider = MagicMock(spec=LLMProvider)

        assert isinstance(provider, LLMProvider)
        assert isinstance(provider.chat, AsyncMock)  # async def 自动映射成 AsyncMock

    def test_create_autospec_is_llmprovider_instance(self):
        provider = create_autospec(LLMProvider, instance=True)

        assert isinstance(provider, LLMProvider)
        assert isinstance(provider.chat, AsyncMock)

    def test_spec_mock_rejects_unknown_attributes(self):
        # 调用方一旦调用抽象接口之外的方法, Mock 立刻报错(暴露越界依赖)
        provider = MagicMock(spec=LLMProvider)

        with pytest.raises(AttributeError):
            provider.fetch_models  # noqa: B018 故意访问不存在的属性


# --------------------------------------------------- chat 调用契约(Mock 驱动断言)


class TestChatCallContract:
    @pytest.mark.asyncio
    async def test_await_returns_configured_response(self, messages):
        expected = LLMResponse(content="配置好的回复", finish_reason="stop")
        provider = make_mock_provider(expected)

        result = await provider.chat(messages, tools=None, model="qwen3")

        assert result is expected
        provider.chat.assert_awaited_once_with(messages, tools=None, model="qwen3")

    @pytest.mark.asyncio
    async def test_tools_and_model_are_optional(self, messages):
        provider = make_mock_provider()

        await provider.chat(messages)

        provider.chat.assert_awaited_once_with(messages)

    @pytest.mark.asyncio
    async def test_autospec_enforces_signature(self, messages):
        # 实参名必须与抽象签名一致: 传错关键字直接 TypeError, 且不记入调用次数
        provider = create_autospec(LLMProvider, instance=True)

        with pytest.raises(TypeError):
            await provider.chat(messages, unknown_kwarg=1)

        assert provider.chat.await_count == 0
        provider.chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exception_side_effect_propagates_to_caller(self, messages):
        provider = MagicMock(spec=LLMProvider)
        provider.chat.side_effect = TimeoutError("上游超时")

        with pytest.raises(TimeoutError, match="上游超时"):
            await provider.chat(messages)

        provider.chat.assert_awaited_once_with(messages)

    @pytest.mark.asyncio
    async def test_call_args_list_records_every_round(self, messages):
        provider = make_mock_provider()

        await provider.chat(messages, model="m1")
        await provider.chat(messages, model="m2")

        assert provider.chat.await_count == 2
        assert [c.kwargs["model"] for c in provider.chat.await_args_list] == ["m1", "m2"]


# --------------------------------------------- 同一消费者 + Mock/真实实现对照


class TestConsumerAgainstMockAndReal:
    """同一消费者分别用 Mock 与真实子类驱动, 断言对外可观测行为一致."""

    @pytest.mark.asyncio
    async def test_mock_and_real_provider_are_interchangeable(self, messages):
        expected = LLMResponse(content="hi")
        mock_provider = make_mock_provider(expected)
        real_provider = FakeProvider(expected)

        mock_result = await call_provider(mock_provider, messages, tools=None, model="qwen3")
        real_result = await call_provider(real_provider, messages, tools=None, model="qwen3")

        assert mock_result is expected
        assert real_result is expected
        mock_provider.chat.assert_awaited_once_with(messages, tools=None, model="qwen3")
        assert real_provider.calls == [{"messages": messages, "tools": None, "model": "qwen3"}]

    @pytest.mark.asyncio
    async def test_consumer_forwards_tool_definitions_untouched(self, messages):
        tool_defs = [{"type": "function", "function": {"name": "read_file"}}]
        mock_provider = make_mock_provider()
        real_provider = FakeProvider()

        await call_provider(mock_provider, messages, tools=tool_defs, model="m")
        await call_provider(real_provider, messages, tools=tool_defs, model="m")

        assert mock_provider.chat.await_args.kwargs["tools"] is tool_defs
        assert real_provider.calls[0]["tools"] is tool_defs  # 定义原样透传, 不被拷贝/改写

    @pytest.mark.asyncio
    async def test_consumer_propagates_provider_failure_identically(self, messages):
        mock_provider = MagicMock(spec=LLMProvider)
        mock_provider.chat.side_effect = ConnectionError("连接失败")

        class BoomProvider(LLMProvider):
            async def chat(self, messages, tools=None, model=None) -> LLMResponse:
                raise ConnectionError("连接失败")

        for provider in (mock_provider, BoomProvider()):
            with pytest.raises(ConnectionError, match="连接失败"):
                await call_provider(provider, messages)


# --------------------------------------------------------------- ToolCallRequest


class TestToolCallRequest:
    def test_is_dataclass_with_expected_field_order(self):
        assert is_dataclass(ToolCallRequest)
        assert [f.name for f in fields(ToolCallRequest)] == [
            "id",
            "name",
            "arguments",
            "reasoning_content",
        ]

    def test_construction_and_defaults(self):
        call = ToolCallRequest(id="call_1", name="read_file", arguments={"file_path": "a.py"})

        assert call.id == "call_1"
        assert call.name == "read_file"
        assert call.arguments == {"file_path": "a.py"}
        assert call.reasoning_content is None  # 非推理模型默认无思考文本

    def test_reasoning_content_can_carry_reasoning_text(self):
        call = ToolCallRequest(
            id="call_2",
            name="write_file",
            arguments={"file_path": "a.py", "content": "x"},
            reasoning_content="先分析再写入",
        )

        assert call.reasoning_content == "先分析再写入"

    @pytest.mark.parametrize("missing", ["id", "name", "arguments"])
    def test_required_fields_cannot_be_omitted(self, missing):
        payload = {"id": "c", "name": "n", "arguments": {}}
        payload.pop(missing)

        with pytest.raises(TypeError):
            ToolCallRequest(**payload)

    def test_equality_follows_field_values(self):
        kwargs = {"id": "c", "name": "read_file", "arguments": {"file_path": "a.py"}}

        assert ToolCallRequest(**kwargs) == ToolCallRequest(**kwargs)
        assert ToolCallRequest(**kwargs) != ToolCallRequest(**{**kwargs, "id": "other"})

    def test_asdict_serializes_for_message_payload(self):
        call = ToolCallRequest(id="c", name="read_file", arguments={"file_path": "a.py"})

        assert asdict(call) == {
            "id": "c",
            "name": "read_file",
            "arguments": {"file_path": "a.py"},
            "reasoning_content": None,
        }

    @pytest.mark.asyncio
    async def test_arguments_unpack_into_tool_execute(self):
        # 跨模块契约: arguments 必须能 ** 解包进 BaseTool.execute(**kwargs)
        async def fake_execute(**kwargs: Any) -> str:
            return f"读取:{kwargs['file_path']}"

        tool = MagicMock(spec=BaseTool)
        tool.execute = AsyncMock(side_effect=fake_execute)
        call = ToolCallRequest(id="c", name="read_file", arguments={"file_path": "src/a.py"})

        result = await tool.execute(**call.arguments)

        assert result == "读取:src/a.py"
        tool.execute.assert_awaited_once_with(file_path="src/a.py")


# ----------------------------------------------------------------- LLMResponse


class TestLLMResponse:
    def test_is_dataclass_with_expected_field_order(self):
        assert is_dataclass(LLMResponse)
        assert [f.name for f in fields(LLMResponse)] == [
            "content",
            "tool_calls",
            "finish_reason",
            "usage",
            "reasoning_content",  # 新增字段放在末尾, 不影响既有位置参数调用
        ]

    def test_content_is_required_but_may_be_none(self):
        with pytest.raises(TypeError):
            LLMResponse()

        # 纯工具调用场景: 模型可以不返回文本
        assert LLMResponse(content=None).content is None

    def test_defaults(self):
        response = LLMResponse(content="hi")

        assert response.tool_calls == []
        assert response.finish_reason == FINISH_REASON_STOP
        assert response.usage == {}
        assert response.reasoning_content is None

    def test_tool_calls_default_is_not_shared_between_instances(self):
        first, second = LLMResponse(content="a"), LLMResponse(content="b")

        first.tool_calls.append(ToolCallRequest(id="c", name="t", arguments={}))

        assert second.tool_calls == []  # 可变默认值未跨实例共享
        assert first.tool_calls is not second.tool_calls

    def test_usage_none_is_normalized_to_empty_dict(self):
        response = LLMResponse(content="hi", usage=None)

        assert response.usage == {}
        assert isinstance(response.usage, dict)

    def test_explicit_usage_is_kept_as_is(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 5}

        response = LLMResponse(content="hi", usage=usage)

        assert response.usage is usage  # 不做拷贝, 保留调用方对象
        assert response.usage["prompt_tokens"] == 10

    @pytest.mark.parametrize(
        ("tool_calls", "expected"),
        [
            ([], False),
            ([ToolCallRequest(id="c1", name="read_file", arguments={})], True),
            (
                [
                    ToolCallRequest(id="c1", name="read_file", arguments={}),
                    ToolCallRequest(id="c2", name="list_dir", arguments={}),
                ],
                True,
            ),
        ],
    )
    def test_has_tool_calls_reflects_tool_calls(self, tool_calls, expected):
        response = LLMResponse(content="hi", tool_calls=tool_calls)

        assert response.has_tool_calls is expected

    def test_has_tool_calls_is_computed_dynamically(self):
        # 属性每次读取都重新计算: 构造后再 append 也要能反映出来
        response = LLMResponse(content="hi")
        assert response.has_tool_calls is False

        response.tool_calls.append(ToolCallRequest(id="c", name="t", arguments={}))

        assert response.has_tool_calls is True

    def test_tool_call_finish_reason_round_trip(self):
        call = ToolCallRequest(
            id="call_1",
            name="read_file",
            arguments={"file_path": "a.py"},
            reasoning_content="思考中",
        )
        response = LLMResponse(content=None, tool_calls=[call], finish_reason="tool_call")

        assert response.finish_reason == "tool_call"
        assert response.has_tool_calls is True
        assert asdict(response)["tool_calls"] == [
            {
                "id": "call_1",
                "name": "read_file",
                "arguments": {"file_path": "a.py"},
                "reasoning_content": "思考中",
            }
        ]

    def test_reasoning_content_defaults_to_none(self):
        assert LLMResponse(content="hi").reasoning_content is None

    def test_pure_text_reasoning_reply_keeps_thinking_text(self):
        # 纯文本推理回复(无工具调用)的思考过程不再丢失
        response = LLMResponse(content="答案是 42", reasoning_content="先算 6*7")

        assert response.has_tool_calls is False
        assert response.reasoning_content == "先算 6*7"
        assert response.content == "答案是 42"

    def test_reasoning_content_coexists_with_tool_calls(self):
        call = ToolCallRequest(id="c1", name="read_file", arguments={}, reasoning_content="按 call 的思考")
        response = LLMResponse(
            content=None, tool_calls=[call], reasoning_content="整轮思考", finish_reason="tool_calls"
        )

        assert response.reasoning_content == "整轮思考"
        assert response.tool_calls[0].reasoning_content == "按 call 的思考"  # 两级思考互不覆盖

    def test_reasoning_content_participates_in_asdict_and_equality(self):
        assert asdict(LLMResponse(content="hi", reasoning_content="思考"))["reasoning_content"] == "思考"
        assert LLMResponse(content="hi", reasoning_content="思考") == LLMResponse(
            content="hi", reasoning_content="思考"
        )
        assert LLMResponse(content="hi", reasoning_content="A") != LLMResponse(
            content="hi", reasoning_content="B"
        )


# ------------------------------------------------------------ finish_reason 契约


class TestFinishReasonContract:
    """finish_reason 取值与 OpenAI 协议对齐, 上层统一引用常量而非硬编码字符串."""

    def test_constants_match_openai_wire_values(self):
        # 与 OpenAI Chat Completions 返回值逐字一致, Provider 才能原样透传
        assert FINISH_REASON_STOP == "stop"
        assert FINISH_REASON_LENGTH == "length"
        assert FINISH_REASON_TOOL_CALLS == "tool_calls"
        assert FINISH_REASON_CONTENT_FILTER == "content_filter"

    def test_tool_calls_constant_is_plural(self):
        # 回归防线: 曾把工具调用误记成 tool_call(单数), 与 OpenAI 实际返回值不符
        assert FINISH_REASON_TOOL_CALLS != "tool_call"

    def test_error_is_project_defined_not_a_wire_value(self):
        wire_values = {
            FINISH_REASON_STOP,
            FINISH_REASON_LENGTH,
            FINISH_REASON_TOOL_CALLS,
            FINISH_REASON_CONTENT_FILTER,
        }

        assert FINISH_REASON_ERROR == "error"
        assert FINISH_REASON_ERROR not in wire_values  # 项目自定义, 不会被服务端返回

    def test_default_finish_reason_is_stop_constant(self):
        assert LLMResponse(content="hi").finish_reason == FINISH_REASON_STOP
        assert LLMResponse.__dataclass_fields__["finish_reason"].default == FINISH_REASON_STOP

    def test_constants_are_exported_from_package(self):
        import meowmeowclaw.providers as providers

        assert providers.FINISH_REASON_TOOL_CALLS == "tool_calls"
        assert providers.FINISH_REASON_ERROR == "error"


# ---------------------------------------------------------------- 抽象基类约束


class TestLLMProviderAbstract:
    def test_is_abstract_with_single_abstract_method(self):
        assert issubclass(LLMProvider, object)
        assert LLMProvider.__abstractmethods__ == frozenset({"chat"})

    def test_cannot_instantiate_abstract_base(self):
        with pytest.raises(TypeError, match="abstract"):
            LLMProvider()  # type: ignore[abstract]

    def test_subclass_without_chat_cannot_be_instantiated(self):
        class IncompleteProvider(LLMProvider):
            pass

        with pytest.raises(TypeError, match="abstract"):
            IncompleteProvider()  # type: ignore[abstract]

    def test_chat_is_coroutine_function(self):
        assert iscoroutinefunction(LLMProvider.chat)
        assert iscoroutinefunction(FakeProvider.chat)

    def test_chat_signature_contract(self):
        params = signature(LLMProvider.chat).parameters

        assert list(params) == ["self", "messages", "tools", "model"]
        assert params["tools"].default is None
        assert params["model"].default is None
        assert params["messages"].default is Parameter.empty

    @pytest.mark.asyncio
    async def test_concrete_subclass_is_usable(self, messages):
        provider = ChatOnlyProvider()

        assert isinstance(provider, LLMProvider)
        assert await provider.chat(messages) == LLMResponse(content="ok")


# ------------------------------------------------------------- 已知缺口(xfail 跟踪)


class TestKnownGaps:
    """以 xfail 记录当前实现的已知缺口: 修复后自动转 XPASS, 便于回归跟踪."""

    @pytest.mark.xfail(
        reason="has_tool_calls 直接 len(self.tool_calls), tool_calls=None 时抛 TypeError "
        "(真实 provider 映射 API 响应时容易传 None)",
        strict=False,
    )
    def test_tool_calls_none_should_be_treated_as_no_tool_call(self):
        # 真实 provider 直接把 API 响应映射过来时很容易传 None, 期望返回 False 而不是抛错
        response = LLMResponse(content="hi", tool_calls=None)

        assert response.has_tool_calls is False

    @pytest.mark.xfail(
        reason="usage 默认值 None, 但类型标注是 dict[str, Any], 缺少 Optional",
        strict=False,
    )
    def test_usage_field_annotation_should_be_optional(self):
        # usage 默认值是 None, 类型标注却是 dict[str, Any](缺少 Optional)
        assert LLMResponse.__dataclass_fields__["usage"].type == Optional[dict[str, Any]]
