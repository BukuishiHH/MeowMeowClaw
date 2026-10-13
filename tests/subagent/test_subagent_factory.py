"""SubagentFactory + SpawnSubagentTool 单元/集成测试(SA2).

对照 docs/SUBAGENT_DESIGN.md §3.1/§3.2/§5/§11.3:
- depth 上限: depth2 子代理工具集中没有 spawn_subagent, 执行层也拒绝;
- 上下文隔离: 子代理 messages 只有独立 system prompt + task, 不含父历史;
- SA2 只读: write_file/exec 被过滤;
- 超时/预算/fail-soft;
- 根 AgentLoop -> SpawnSubagentTool -> factory -> 子 AgentLoop 端到端;
- 嵌套 spawn 不触发调度器自我死锁。
"""

import asyncio
import json
from typing import Any

import pytest

from meowmeowclaw.agent.loop import AgentLoop
from meowmeowclaw.llm.base import (
    FINISH_REASON_STOP,
    FINISH_REASON_TOOL_CALLS,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
)
from meowmeowclaw.subagent import (
    ERROR_CHILDREN_EXCEEDED,
    ERROR_DEPTH_EXCEEDED,
    ERROR_ENABLED,
    ERROR_TIMEOUT,
    STATUS_ERROR,
    STATUS_LIMIT,
    STATUS_OK,
    STATUS_TIMEOUT,
    SpawnContext,
    SubagentBudget,
    SubagentFactory,
    SubagentRequest,
)
from meowmeowclaw.tools.base import BaseTool
from meowmeowclaw.tools.filesystem import ReadFileTool, WriteFileTool
from meowmeowclaw.tools.mutation import ReadVersionTracker, WorkspaceMutationGuard
from meowmeowclaw.tools.registry import ToolRegistry


class NamedTool(BaseTool):
    """只提供名字的最小工具(测试子代理工具子集/定义)."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} 测试工具"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


class CapturingProvider(LLMProvider):
    """记录 provider 收到的 messages/tools, 返回固定回答."""

    def __init__(self, answer: str = "子代理结果", *, delay: float = 0.0) -> None:
        self.answer = answer
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, tools=None, model=None, max_tokens=None) -> LLMResponse:
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "tools": list(tools or []),
                "model": model,
            }
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        return LLMResponse(content=self.answer, finish_reason=FINISH_REASON_STOP)


class SimpleContextBuilder:
    """根 AgentLoop 用的最小 context(只负责拼接 system/history/current)."""

    def build_messages(self, history=None, current_message=""):
        messages = [{"role": "system", "content": "ROOT SYSTEM"}]
        if history:
            messages.extend(history)
        if current_message:
            messages.append({"role": "user", "content": current_message})
        return messages


def base_registry_with(*names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(NamedTool(name))
    return registry


def make_factory(provider, registry=None, **overrides) -> SubagentFactory:
    params = {
        "provider": provider,
        "base_registry": registry if registry is not None else base_registry_with("read_file"),
        "workspace": "/tmp/subagent-ws",
        "read_only": True,
    }
    params.update(overrides)
    return SubagentFactory(**params)


def tool_names(payload: dict[str, Any]) -> list[str]:
    return [item["function"]["name"] for item in payload["tools"]]


class TestSpawnOkAndIsolation:
    @pytest.mark.asyncio
    async def test_spawn_ok_child_prompt_only_has_task_and_context(self):
        provider = CapturingProvider()
        registry = base_registry_with("read_file", "write_file", "exec")
        factory = make_factory(
            provider,
            registry,
            tool_names=("read_file", "write_file", "exec"),
        )
        ctx = SpawnContext(
            root_id="root", parent_id="root", depth=0, budget=SubagentBudget()
        )

        result = await factory.spawn(
            SubagentRequest(task="独立任务", role="researcher", context="必要背景"), ctx
        )

        assert result.status == STATUS_OK
        assert result.depth == 1
        assert result.summary == "子代理结果"

        call = provider.calls[0]
        assert len(call["messages"]) == 2
        system_prompt = call["messages"][0]["content"]
        assert "独立任务" in system_prompt
        assert "researcher" in system_prompt
        assert "必要背景" in system_prompt
        # SA2 只读: write_file/exec 被过滤; depth1 仍可再 spawn 一层
        assert "write_file" not in system_prompt
        assert "exec" not in system_prompt
        assert tool_names(call) == ["read_file", "spawn_subagent"]

    @pytest.mark.asyncio
    async def test_depth_two_child_tools_have_no_spawn(self):
        provider = CapturingProvider()
        factory = make_factory(provider, base_registry_with("read_file"))
        ctx = SpawnContext(
            root_id="root", parent_id="child-1", depth=1, budget=SubagentBudget()
        )

        result = await factory.spawn(SubagentRequest(task="depth2 任务"), ctx)

        assert result.status == STATUS_OK
        assert result.depth == 2
        assert tool_names(provider.calls[0]) == ["read_file"]

    @pytest.mark.asyncio
    async def test_sa3_write_enabled_but_exec_still_filtered(self):
        provider = CapturingProvider()
        factory = make_factory(
            provider,
            base_registry_with("read_file", "write_file", "exec"),
            tool_names=("read_file", "write_file", "exec"),
            read_only=False,
            allow_exec=False,
        )
        ctx = SpawnContext(root_id="root", parent_id="root", depth=0, budget=SubagentBudget())

        result = await factory.spawn(SubagentRequest(task="写入测试"), ctx)

        assert result.status == STATUS_OK
        names = tool_names(provider.calls[0])
        assert "read_file" in names
        assert "write_file" in names
        assert "exec" not in names

    def test_child_write_tool_gets_independent_tracker_and_shared_guard(self, tmp_path):
        workspace = tmp_path / "ws"
        guard = WorkspaceMutationGuard(tmp_path / "workspace.lock")
        base = ToolRegistry()
        base.register(ReadFileTool(workspace, workspace / "memory"))
        base.register(
            WriteFileTool(workspace, workspace / "memory", mutation_guard=guard)
        )
        factory = SubagentFactory(
            provider=CapturingProvider(),
            base_registry=base,
            workspace=workspace,
            read_only=False,
            tool_names=("read_file", "write_file"),
        )
        tracker = ReadVersionTracker()

        child = factory._build_child_registry(tracker)  # noqa: SLF001 - 验证克隆语义

        child_write = child.get("write_file")
        parent_write = base.get("write_file")
        assert child_write is not parent_write
        assert child_write._version_tracker is tracker  # noqa: SLF001
        assert child_write._mutation_guard is guard  # noqa: SLF001


class TestDepthBudgetFailSoft:
    @pytest.mark.asyncio
    async def test_root_tool_budget_is_scoped_to_current_turn_context(self):
        from meowmeowclaw.agent.loop import CURRENT_AGENT_TURN, AgentTurnContext

        provider = CapturingProvider()
        factory = make_factory(provider)
        tool = factory.make_root_tool()

        async def resolve(turn_id: str) -> SpawnContext:
            token = CURRENT_AGENT_TURN.set(AgentTurnContext(turn_id=turn_id))
            try:
                return tool._resolve_context()  # noqa: SLF001 - 验证并发会话预算隔离
            finally:
                CURRENT_AGENT_TURN.reset(token)

        first, second = await asyncio.gather(resolve("turn-a"), resolve("turn-b"))

        assert first.root_id == "turn-a"
        assert second.root_id == "turn-b"
        assert first.budget is not second.budget

    @pytest.mark.asyncio
    async def test_depth_exceeded_rejected_before_provider(self):
        provider = CapturingProvider()
        factory = make_factory(provider)
        ctx = SpawnContext(
            root_id="root", parent_id="child-2", depth=2, budget=SubagentBudget()
        )

        result = await factory.spawn(SubagentRequest(task="too deep"), ctx)

        assert result.status == STATUS_LIMIT
        assert result.error_code == ERROR_DEPTH_EXCEEDED
        assert provider.calls == []

    @pytest.mark.asyncio
    async def test_children_budget_exceeded(self):
        provider = CapturingProvider()
        factory = make_factory(
            provider, max_children_per_turn=1, max_total_per_turn=10
        )
        budget = SubagentBudget(max_children_per_turn=1, max_total_per_turn=10)
        ctx = SpawnContext(root_id="root", parent_id="root", depth=0, budget=budget)

        assert (await factory.spawn(SubagentRequest(task="a"), ctx)).status == STATUS_OK
        second = await factory.spawn(SubagentRequest(task="b"), ctx)

        assert second.status == STATUS_LIMIT
        assert second.error_code == ERROR_CHILDREN_EXCEEDED
        assert len(provider.calls) == 1

    @pytest.mark.asyncio
    async def test_total_budget_exceeded(self):
        provider = CapturingProvider()
        factory = make_factory(
            provider, max_children_per_turn=5, max_total_per_turn=1
        )
        budget = SubagentBudget(max_children_per_turn=5, max_total_per_turn=1)

        first = await factory.spawn(
            SubagentRequest(task="a"),
            SpawnContext(root_id="root", parent_id="p1", depth=0, budget=budget),
        )
        second = await factory.spawn(
            SubagentRequest(task="b"),
            SpawnContext(root_id="root", parent_id="p2", depth=0, budget=budget),
        )

        assert first.status == STATUS_OK
        assert second.status == STATUS_LIMIT
        assert second.error_code == "budget_exceeded"

    @pytest.mark.asyncio
    async def test_timeout_is_folded_to_result(self):
        provider = CapturingProvider(delay=0.2)
        factory = make_factory(provider, timeout_seconds=0.01)
        ctx = SpawnContext(root_id="root", parent_id="root", depth=0, budget=SubagentBudget())

        result = await factory.spawn(SubagentRequest(task="slow"), ctx)

        assert result.status == STATUS_TIMEOUT
        assert result.error_code == ERROR_TIMEOUT
        assert result.finish_reason == "timeout"

    @pytest.mark.asyncio
    async def test_disabled_factory_returns_error(self):
        provider = CapturingProvider()
        factory = make_factory(provider, enabled=False)
        ctx = SpawnContext(root_id="root", parent_id="root", depth=0, budget=SubagentBudget())

        result = await factory.spawn(SubagentRequest(task="x"), ctx)

        assert result.status == STATUS_ERROR
        assert result.error_code == ERROR_ENABLED
        assert provider.calls == []


class TestChildWriteExecution:
    @pytest.mark.asyncio
    async def test_child_write_file_executes_through_clone_and_tracker(self, tmp_path):
        class WriteProvider(CapturingProvider):
            async def chat(self, messages, tools=None, model=None, max_tokens=None):
                self.calls.append(
                    {
                        "messages": [dict(message) for message in messages],
                        "tools": list(tools or []),
                        "model": model,
                    }
                )
                if len(self.calls) == 1:
                    assert "write_file" in [item["function"]["name"] for item in tools or []]
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCallRequest(
                                id="call_write",
                                name="write_file",
                                arguments={"file_path": "a.txt", "content": "hello"},
                            )
                        ],
                        finish_reason=FINISH_REASON_TOOL_CALLS,
                    )
                return LLMResponse(content="写完了", finish_reason=FINISH_REASON_STOP)

        workspace = tmp_path / "ws"
        memory = workspace / "memory"
        guard = WorkspaceMutationGuard(tmp_path / "workspace.lock", timeout=2.0)
        base = ToolRegistry()
        base.register(ReadFileTool(workspace, memory))
        base.register(WriteFileTool(workspace, memory, mutation_guard=guard))
        factory = SubagentFactory(
            provider=WriteProvider(),
            base_registry=base,
            workspace=workspace,
            read_only=False,
            allow_exec=False,
            tool_names=("read_file", "write_file"),
        )
        ctx = SpawnContext(root_id="root", parent_id="root", depth=0, budget=SubagentBudget())

        result = await factory.spawn(SubagentRequest(task="写 a.txt"), ctx)

        assert result.status == STATUS_OK
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "hello"


class TestRootAgentLoopIntegration:
    @pytest.mark.asyncio
    async def test_root_loop_spawns_child_and_consumes_json_result(self):
        class RoutingProvider(CapturingProvider):
            def __init__(self) -> None:
                super().__init__()
                self.root_tool_result: str | None = None

            async def chat(self, messages, tools=None, model=None, max_tokens=None):
                self.calls.append(
                    {
                        "messages": [dict(message) for message in messages],
                        "tools": list(tools or []),
                        "model": model,
                    }
                )
                system = str(messages[0].get("content") or "")
                if "只负责完成父代理分配的任务" in system:
                    return LLMResponse(content="子代理结果", finish_reason=FINISH_REASON_STOP)
                if any(message.get("role") == "tool" for message in messages):
                    self.root_tool_result = next(
                        message["content"] for message in messages if message.get("role") == "tool"
                    )
                    return LLMResponse(content="父最终回答", finish_reason=FINISH_REASON_STOP)
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            id="call_1",
                            name="spawn_subagent",
                            arguments={"task": "子任务", "role": "researcher"},
                        )
                    ],
                    finish_reason=FINISH_REASON_TOOL_CALLS,
                )

        provider = RoutingProvider()
        factory = make_factory(provider, base_registry_with("read_file"))
        root_registry = ToolRegistry()
        root_registry.register(factory.make_root_tool())
        loop = AgentLoop(
            provider=provider,
            tools=root_registry,
            context=SimpleContextBuilder(),
            max_iterations=4,
        )

        turn = await asyncio.wait_for(loop.run_turn("父问题"), timeout=5)

        assert turn.completed is True
        assert turn.answer == "父最终回答"
        assert provider.root_tool_result is not None
        payload = json.loads(provider.root_tool_result)
        assert payload["status"] == STATUS_OK
        assert payload["summary"] == "子代理结果"
        assert payload["depth"] == 1

    @pytest.mark.asyncio
    async def test_nested_spawn_does_not_deadlock_and_depth_two_has_no_spawn(self):
        class NestedProvider(CapturingProvider):
            def __init__(self) -> None:
                super().__init__()
                self.depth2_tools: list[list[str]] = []

            async def chat(self, messages, tools=None, model=None, max_tokens=None):
                self.calls.append(
                    {
                        "messages": [dict(message) for message in messages],
                        "tools": list(tools or []),
                        "model": model,
                    }
                )
                system = str(messages[0].get("content") or "")
                names = [item["function"]["name"] for item in (tools or [])]
                has_tool_result = any(message.get("role") == "tool" for message in messages)
                if "深层任务" in system:
                    self.depth2_tools.append(names)
                    return LLMResponse(content="深层结果", finish_reason=FINISH_REASON_STOP)
                if "中间任务" in system:
                    if has_tool_result:
                        return LLMResponse(content="中间完成", finish_reason=FINISH_REASON_STOP)
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCallRequest(
                                id="call_inner",
                                name="spawn_subagent",
                                arguments={"task": "深层任务"},
                            )
                        ],
                        finish_reason=FINISH_REASON_TOOL_CALLS,
                    )
                if has_tool_result:
                    return LLMResponse(content="root完成", finish_reason=FINISH_REASON_STOP)
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            id="call_outer",
                            name="spawn_subagent",
                            arguments={"task": "中间任务"},
                        )
                    ],
                    finish_reason=FINISH_REASON_TOOL_CALLS,
                )

        provider = NestedProvider()
        factory = make_factory(provider, base_registry_with("read_file"))
        root_registry = ToolRegistry()
        root_registry.register(factory.make_root_tool())
        loop = AgentLoop(
            provider=provider,
            tools=root_registry,
            context=SimpleContextBuilder(),
            max_iterations=6,
        )

        turn = await asyncio.wait_for(loop.run_turn("root"), timeout=5)

        assert turn.completed is True
        assert turn.answer == "root完成"
        # depth2 子代理收到的工具定义里没有 spawn_subagent
        assert provider.depth2_tools
        assert all("spawn_subagent" not in names for names in provider.depth2_tools)
