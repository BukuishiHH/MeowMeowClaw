"""meowmeowclaw/agent/loop.py 的 Mock 单元测试.

测试策略:
- Mock 为主:
  * ``ScriptedProvider``(真实 LLMProvider 子类)按脚本吐响应, 并对每次收到的 messages 做深拷贝快照,
    从而可以断言"模型在第二轮看到了什么";
  * ``ToolRegistry`` / ``ContextBuilder`` 用 ``MagicMock(spec=...)`` 替身, 隔离工具与提示词实现;
  * 直接单元测试 ``_check_tool_loop`` 的阈值与滑动窗口, 并回归"熔断阈值必须真的可达";
- 真实实现为辅: 端到端用例用真实 ContextBuilder + 真实 ToolRegistry + 真实 ReadFileTool,
  只把模型替换成脚本, 验证 "模型要工具 -> 真读盘 -> 回填 -> 最终回答" 整条链路.

运行: pytest tests/test_loop.py -v
"""

import copy
import json
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

import meowmeowclaw.agent.loop as loop_module
from meowmeowclaw.agent.context import ContextBuilder
from meowmeowclaw.agent.loop import (
    CIRCUIT_BREAK_PREFIX,
    DEFAULT_MAX_ITERATIONS,
    LOOP_CIRCUIT_BREAK_THRESHOLD,
    LOOP_WARNING_PREFIX,
    LOOP_WARNING_THRESHOLD,
    SYSTEM_ERROR_PREFIX,
    TOOL_CALL_WINDOW_SIZE,
    AgentLoop,
)
from meowmeowclaw.tools.filesystem import ReadFileTool
from meowmeowclaw.tools.registry import ToolRegistry
from meowmeowclaw.paths import IDENTITY_FILE
from meowmeowclaw.providers.base import (
    FINISH_REASON_ERROR,
    FINISH_REASON_STOP,
    FINISH_REASON_TOOL_CALLS,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
)

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


# --------------------------------------------------------------------- 测试替身


class ScriptedProvider(LLMProvider):
    """按脚本依次返回响应; 快照每次收到的 messages, 便于断言模型"看到了什么"."""

    def __init__(self, *responses: LLMResponse, default: Optional[LLMResponse] = None) -> None:
        self._responses = list(responses)
        self._default = default
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
    ) -> LLMResponse:
        self.calls.append(
            {"messages": copy.deepcopy(messages), "tools": tools, "model": model}
        )
        if self._responses:
            return self._responses.pop(0)
        if self._default is not None:
            return self._default
        raise AssertionError("脚本已用尽, 但主循环仍在调用 provider")


def text_response(content: str) -> LLMResponse:
    return LLMResponse(content=content, finish_reason=FINISH_REASON_STOP)


def tool_response(*calls: ToolCallRequest, content: Optional[str] = None) -> LLMResponse:
    return LLMResponse(
        content=content, tool_calls=list(calls), finish_reason=FINISH_REASON_TOOL_CALLS
    )


def make_call(
    call_id: str = "call_1",
    name: str = "read_file",
    arguments: Optional[dict[str, Any]] = None,
    reasoning_content: Optional[str] = None,
) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name=name,
        arguments={"file_path": "a.py"} if arguments is None else arguments,
        reasoning_content=reasoning_content,
    )


def make_registry(result: Any = None) -> MagicMock:
    registry = MagicMock(spec=ToolRegistry)
    registry.execute = AsyncMock(
        side_effect=result if result is not None else (lambda name, args: f"工具结果:{name}")
    )
    registry.get_definitions.return_value = TOOL_DEFS
    registry.list_tools.return_value = ["read_file"]
    return registry


def make_context_messages(history=None, current_message="") -> list[dict[str, Any]]:
    """忠实模仿真实 ContextBuilder 的 messages 拼装规则(含"空消息不追加")."""
    return (
        [{"role": "system", "content": "SYS"}]
        + (list(history) if history else [])
        + ([{"role": "user", "content": current_message}] if current_message else [])
    )


def make_context() -> MagicMock:
    context = MagicMock(spec=ContextBuilder)
    context.build_messages.side_effect = make_context_messages
    return context


def make_loop(provider, registry=None, context=None, **kwargs) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        tools=registry if registry is not None else make_registry(),
        context=context if context is not None else make_context(),
        **kwargs,
    )


def all_keys(messages: list[dict[str, Any]]) -> set[str]:
    return {key for message in messages for key in message}


def find_message(messages: list[dict[str, Any]], role: str) -> dict[str, Any]:
    return next(m for m in messages if m["role"] == role)


# ------------------------------------------------- 迭代上限由装配层显式注入


class TestInjectedMaxIterations:
    @pytest.mark.asyncio
    async def test_injected_value_is_used(self):
        # 生产路径: bootstrap.build_application 把 config.max_iterations 传进来
        provider = ScriptedProvider(default=tool_response(make_call()))
        loop = make_loop(provider, max_iterations=7)

        assert loop.max_iterations == 7
        assert "最大迭代次数 7" in await loop.run("hi")
        assert len(provider.calls) == 7

    def test_default_fallback_when_not_injected(self):
        # 直接构造(如单测)未注入时使用模块兜底值, 不再读取任何全局配置
        loop = make_loop(ScriptedProvider(text_response("x")))

        assert loop.max_iterations == DEFAULT_MAX_ITERATIONS

    def test_each_instance_keeps_its_own_value(self):
        first = make_loop(ScriptedProvider(text_response("x")), max_iterations=5)
        second = make_loop(ScriptedProvider(text_response("x")), max_iterations=9)

        assert (first.max_iterations, second.max_iterations) == (5, 9)

    def test_repr_shows_effective_max_iterations(self):
        assert "32" in repr(make_loop(ScriptedProvider(text_response("x"))))


# ------------------------------------------------------------------ 正常文本回复


class TestTextReplies:
    @pytest.mark.asyncio
    async def test_returns_model_text(self):
        loop = make_loop(ScriptedProvider(text_response("你好呀")))

        assert await loop.run("hi") == "你好呀"

    @pytest.mark.asyncio
    async def test_provider_receives_tool_definitions_and_model(self):
        provider = ScriptedProvider(text_response("ok"))
        registry = make_registry()
        loop = make_loop(provider, registry, model="qwen3-max")

        await loop.run("hi")

        assert provider.calls[0]["tools"] == TOOL_DEFS
        assert provider.calls[0]["model"] == "qwen3-max"
        assert provider.calls[0]["messages"][0] == {"role": "system", "content": "SYS"}
        assert provider.calls[0]["messages"][-1] == {"role": "user", "content": "hi"}

    @pytest.mark.asyncio
    async def test_model_defaults_to_none(self):
        provider = ScriptedProvider(text_response("ok"))
        loop = make_loop(provider)

        await loop.run("hi")

        assert provider.calls[0]["model"] is None

    @pytest.mark.asyncio
    async def test_empty_content_returns_empty_string(self):
        loop = make_loop(ScriptedProvider(LLMResponse(content=None, finish_reason=FINISH_REASON_STOP)))

        assert await loop.run("hi") == ""


# ------------------------------------------------------------------ 工具调用往返


class TestToolRoundTrip:
    @pytest.mark.asyncio
    async def test_assistant_and_tool_message_shape(self):
        provider = ScriptedProvider(
            tool_response(make_call()), text_response("最终回答")
        )
        registry = make_registry()
        loop = make_loop(provider, registry)

        assert await loop.run("读一下 a.py") == "最终回答"

        messages = provider.calls[1]["messages"]  # 第二次请求, 模型看到的完整上下文
        assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool"]

        assistant = messages[2]
        assert assistant["role"] == "assistant"
        assert assistant["content"] == ""            # content=None 回填空串
        assert len(assistant["tool_calls"]) == 1
        raw_call = assistant["tool_calls"][0]
        assert raw_call["id"] == "call_1"
        assert raw_call["type"] == "function"
        assert raw_call["function"]["name"] == "read_file"
        # arguments 必须是 JSON 字符串, 且能还原成 dict
        assert isinstance(raw_call["function"]["arguments"], str)
        assert json.loads(raw_call["function"]["arguments"]) == {"file_path": "a.py"}

        tool_message = messages[3]
        assert tool_message["role"] == "tool"
        assert tool_message["tool_call_id"] == "call_1"
        assert tool_message["content"] == "工具结果:read_file"

        registry.execute.assert_awaited_once_with("read_file", {"file_path": "a.py"})

    @pytest.mark.asyncio
    async def test_assistant_content_is_kept_when_present(self):
        provider = ScriptedProvider(
            tool_response(make_call(), content="我先读一下文件"), text_response("done")
        )
        loop = make_loop(provider)

        await loop.run("hi")

        assistant = find_message(provider.calls[1]["messages"], "assistant")
        assert assistant["content"] == "我先读一下文件"

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_in_one_response(self):
        provider = ScriptedProvider(
            tool_response(
                make_call("c1", "read_file", {"file_path": "a.py"}),
                make_call("c2", "list_dir", {"dir_path": "src"}),
            ),
            text_response("完成"),
        )
        registry = make_registry()
        loop = make_loop(provider, registry)

        assert await loop.run("帮我看看") == "完成"

        assistant = find_message(provider.calls[1]["messages"], "assistant")
        assert [c["id"] for c in assistant["tool_calls"]] == ["c1", "c2"]

        tool_messages = [m for m in provider.calls[1]["messages"] if m["role"] == "tool"]
        assert [(m["tool_call_id"], m["content"]) for m in tool_messages] == [
            ("c1", "工具结果:read_file"),
            ("c2", "工具结果:list_dir"),
        ]
        assert registry.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_chinese_arguments_are_not_escaped(self):
        provider = ScriptedProvider(
            tool_response(make_call(arguments={"file_path": "中文目录/说明.md"})),
            text_response("done"),
        )
        loop = make_loop(provider)

        await loop.run("hi")

        assistant = find_message(provider.calls[1]["messages"], "assistant")
        raw = assistant["tool_calls"][0]["function"]["arguments"]
        assert "中文目录" in raw          # ensure_ascii=False, 便于排查
        assert "\\u" not in raw

    @pytest.mark.asyncio
    async def test_reasoning_content_is_not_echoed_into_messages(self):
        provider = ScriptedProvider(
            tool_response(
                make_call(reasoning_content="调用级思考"), content="思考中..." 
            ),
            text_response("done"),
        )
        provider._responses[0] = LLMResponse(
            content="思考中...",
            tool_calls=[make_call(reasoning_content="调用级思考")],
            finish_reason=FINISH_REASON_TOOL_CALLS,
            reasoning_content="整轮思考",
        )
        loop = make_loop(provider)

        await loop.run("hi")

        # DeepSeek 等要求多轮时不得回传 reasoning_content
        assert "reasoning_content" not in all_keys(provider.calls[1]["messages"])
        assert "整轮思考" not in json.dumps(provider.calls[1]["messages"], ensure_ascii=False)


# ------------------------------------------------------------- 错误与迭代上限


class TestErrorAndTimeout:
    @pytest.mark.asyncio
    async def test_error_finish_reason_returns_immediately(self):
        error = LLMResponse(content="[LLM调用失败] APIConnectionError: boom", finish_reason=FINISH_REASON_ERROR)
        provider = ScriptedProvider(error, text_response("不该被调用"))
        registry = make_registry()
        loop = make_loop(provider, registry)

        result = await loop.run("hi")

        assert result == "[LLM调用失败] APIConnectionError: boom"
        assert len(provider.calls) == 1        # 不再继续请求
        registry.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_error_without_content_still_returns_text(self):
        provider = ScriptedProvider(LLMResponse(content=None, finish_reason=FINISH_REASON_ERROR))
        loop = make_loop(provider)

        assert await loop.run("hi") == "模型调用失败"

    @pytest.mark.asyncio
    async def test_max_iterations_returns_timeout_message(self):
        provider = ScriptedProvider(default=tool_response(make_call()))
        registry = make_registry()
        loop = make_loop(provider, registry, max_iterations=3)

        result = await loop.run("hi")

        assert "最大迭代次数 3" in result
        assert len(provider.calls) == 3        # 恰好用满 3 次
        assert registry.execute.await_count == 3

    @pytest.mark.asyncio
    async def test_zero_iterations_immediately_times_out(self):
        provider = ScriptedProvider(text_response("不会走到"))
        loop = make_loop(provider, max_iterations=0)

        assert "最大迭代次数 0" in await loop.run("hi")
        assert provider.calls == []


# ---------------------------------------------------------------- 防爆(滑动窗口)


class TestLoopGuard:
    def test_first_ten_calls_pass(self):
        loop = make_loop(ScriptedProvider(text_response("x")))

        verdicts = [loop._check_tool_loop("read_file", "{}") for _ in range(LOOP_WARNING_THRESHOLD)]

        assert verdicts == [None] * LOOP_WARNING_THRESHOLD

    def test_warning_at_threshold(self):
        loop = make_loop(ScriptedProvider(text_response("x")))
        for _ in range(LOOP_WARNING_THRESHOLD):
            loop._check_tool_loop("read_file", "{}")

        verdict = loop._check_tool_loop("read_file", "{}")

        assert verdict is not None
        assert verdict.startswith(LOOP_WARNING_PREFIX)
        assert "read_file" in verdict

    def test_circuit_break_is_reachable(self):
        """回归防线: 若"警告"分支不再记入窗口, 计数会卡在 10, 熔断永远不会触发."""
        loop = make_loop(ScriptedProvider(text_response("x")))

        verdicts = [
            loop._check_tool_loop("read_file", "{}")
            for _ in range(LOOP_CIRCUIT_BREAK_THRESHOLD + 1)
        ]

        assert verdicts[LOOP_WARNING_THRESHOLD - 1] is None          # 第 10 次仍放行
        assert verdicts[LOOP_WARNING_THRESHOLD].startswith(LOOP_WARNING_PREFIX)
        assert verdicts[LOOP_CIRCUIT_BREAK_THRESHOLD - 1].startswith(LOOP_WARNING_PREFIX)
        assert verdicts[LOOP_CIRCUIT_BREAK_THRESHOLD].startswith(CIRCUIT_BREAK_PREFIX)

    def test_window_is_capped(self):
        loop = make_loop(ScriptedProvider(text_response("x")))

        for index in range(TOOL_CALL_WINDOW_SIZE * 2):
            loop._check_tool_loop("read_file", json.dumps({"i": index}))

        assert len(loop._tool_call_history) == TOOL_CALL_WINDOW_SIZE

    def test_signature_is_tool_name_plus_args(self):
        loop = make_loop(ScriptedProvider(text_response("x")))

        loop._check_tool_loop("read_file", '{"file_path": "a.py"}')

        assert loop._tool_call_history == ['read_file:{"file_path": "a.py"}']

    def test_different_args_or_tools_are_counted_separately(self):
        loop = make_loop(ScriptedProvider(text_response("x")))

        # 同一工具、每次入参都不同 -> 每个签名各算一次, 永远不会累积到阈值
        for index in range(LOOP_CIRCUIT_BREAK_THRESHOLD + 5):
            assert loop._check_tool_loop("read_file", json.dumps({"i": index})) is None

        # 另一个工具自己只调用了 9 次, 不该被 read_file 的历史牵连
        for _ in range(LOOP_WARNING_THRESHOLD - 1):
            assert loop._check_tool_loop("list_dir", "{}") is None

    @pytest.mark.asyncio
    async def test_warning_skips_execution_and_reports_system_error(self):
        # 前 10 次放行执行, 第 11 次告警跳过, 第 12 轮模型改用文本回答
        provider = ScriptedProvider(
            *([tool_response(make_call())] * (LOOP_WARNING_THRESHOLD + 1)),
            text_response("换个思路答完了"),
            default=None,
        )
        registry = make_registry()
        loop = make_loop(provider, registry, max_iterations=20)

        result = await loop.run("hi")

        assert result == "换个思路答完了"
        assert registry.execute.await_count == LOOP_WARNING_THRESHOLD  # 第 11 次被跳过

        # 模型第 12 轮看到的消息里, 最后一条就是被跳过那次的 SYSTEM_ERROR 回填
        last_round_messages = provider.calls[-1]["messages"]
        assert last_round_messages[-1]["role"] == "tool"
        skipped = [
            m for m in last_round_messages if m["content"].startswith(SYSTEM_ERROR_PREFIX)
        ]
        assert len(skipped) == 1
        assert skipped[0]["tool_call_id"] == "call_1"

    @pytest.mark.asyncio
    async def test_circuit_break_ends_the_round(self):
        provider = ScriptedProvider(default=tool_response(make_call()))
        registry = make_registry()
        loop = make_loop(provider, registry, max_iterations=25)

        result = await loop.run("hi")

        assert result.startswith(CIRCUIT_BREAK_PREFIX)
        assert "read_file" in result
        assert registry.execute.await_count == LOOP_WARNING_THRESHOLD  # 只有告警前的 10 次真的执行
        assert len(provider.calls) == LOOP_CIRCUIT_BREAK_THRESHOLD + 1


# ---------------------------------------------------------------------- 历史管理


class TestSessionHistory:
    @pytest.mark.asyncio
    async def test_successful_round_is_saved_without_system(self):
        provider = ScriptedProvider(tool_response(make_call()), text_response("答完了"))
        loop = make_loop(provider)

        await loop.run("第一问")

        roles = [m["role"] for m in loop._session_history]
        assert roles == ["user", "assistant", "tool", "assistant"]
        assert loop._session_history[0] == {"role": "user", "content": "第一问"}
        assert loop._session_history[-1] == {"role": "assistant", "content": "答完了"}
        assert "system" not in roles

    @pytest.mark.asyncio
    async def test_second_round_receives_saved_history(self):
        provider = ScriptedProvider(text_response("第一次"), text_response("第二次"))
        seen: list[dict[str, Any]] = []

        def spy_build_messages(history=None, current_message=""):
            # 循环是按引用传入 _session_history 的, 这里当场快照
            seen.append({"history": list(history or []), "current_message": current_message})
            return make_context_messages(history, current_message)

        context = MagicMock(spec=ContextBuilder)
        context.build_messages.side_effect = spy_build_messages
        loop = make_loop(provider, context=context)

        await loop.run("第一问")
        first_round_history = list(loop._session_history)
        await loop.run("第二问")

        assert seen[0] == {"history": [], "current_message": "第一问"}
        assert seen[1] == {"history": first_round_history, "current_message": "第二问"}
        # 第二轮模型看到: system + 第一轮全部 + 第二问
        assert [m["role"] for m in provider.calls[1]["messages"]] == [
            "system",
            "user",
            "assistant",
            "user",
        ]

    @pytest.mark.asyncio
    async def test_error_does_not_pollute_history(self):
        loop = make_loop(
            ScriptedProvider(LLMResponse(content="boom", finish_reason=FINISH_REASON_ERROR))
        )

        await loop.run("hi")

        assert loop._session_history == []

    @pytest.mark.asyncio
    async def test_timeout_does_not_pollute_history(self):
        loop = make_loop(
            ScriptedProvider(default=tool_response(make_call())), max_iterations=2
        )

        await loop.run("hi")

        assert loop._session_history == []

    @pytest.mark.asyncio
    async def test_circuit_break_does_not_pollute_history(self):
        loop = make_loop(ScriptedProvider(default=tool_response(make_call())), max_iterations=25)

        await loop.run("hi")

        assert loop._session_history == []

    def test_save_to_history_appends_snapshot(self):
        loop = make_loop(ScriptedProvider(text_response("x")))

        loop._save_to_history([{"role": "user", "content": "a"}])
        loop._save_to_history([{"role": "assistant", "content": "b"}])

        assert loop._session_history == [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]

    def test_clear_history_empties_both_buffers(self):
        loop = make_loop(ScriptedProvider(text_response("x")))
        loop._check_tool_loop("read_file", "{}")
        loop._save_to_history([{"role": "user", "content": "a"}])

        loop.clear_history()

        assert loop._tool_call_history == []
        assert loop._session_history == []

    def test_repr(self):
        text = repr(make_loop(ScriptedProvider(text_response("x")), model="m", max_iterations=7))

        assert "AgentLoop" in text
        assert "'m'" in text
        assert "7" in text
        assert "read_file" in text


# -------------------------------------------------- 真实组件端到端(只替换模型)


class TestEndToEndWithRealComponents:
    @pytest.mark.asyncio
    async def test_model_tool_call_reads_real_file(self, tmp_path):
        (tmp_path / "note.txt").write_text("磁盘上的真实内容", encoding="utf-8")

        registry = ToolRegistry()
        registry.register(ReadFileTool(str(tmp_path)))
        context = ContextBuilder(tmp_path, IDENTITY_FILE)
        provider = ScriptedProvider(
            tool_response(make_call(name="read_file", arguments={"file_path": "note.txt"})),
            text_response("文件里写着: 磁盘上的真实内容"),
        )
        loop = AgentLoop(provider=provider, tools=registry, context=context)

        result = await loop.run("帮我读 note.txt")

        assert result == "文件里写着: 磁盘上的真实内容"

        # 真实 System Prompt 已注入
        assert provider.calls[0]["messages"][0]["role"] == "system"
        assert "## 工作区" in provider.calls[0]["messages"][0]["content"]

        # tool 消息里是真实读盘结果
        tool_message = find_message(provider.calls[1]["messages"], "tool")
        assert tool_message["content"] == "磁盘上的真实内容"

        # 历史完整保留了一整轮
        assert [m["role"] for m in loop._session_history] == ["user", "assistant", "tool", "assistant"]

    @pytest.mark.asyncio
    async def test_real_registry_reports_unknown_tool_back_to_model(self, tmp_path):
        registry = ToolRegistry()
        registry.register(ReadFileTool(str(tmp_path)))
        provider = ScriptedProvider(
            tool_response(make_call(name="不存在的工具")), text_response("那我换个办法")
        )
        loop = AgentLoop(provider=provider, tools=registry, context=ContextBuilder(tmp_path, IDENTITY_FILE))

        assert await loop.run("hi") == "那我换个办法"

        tool_message = find_message(provider.calls[1]["messages"], "tool")
        assert "不存在的工具" in tool_message["content"]  # registry 的错误文本被回填给模型
