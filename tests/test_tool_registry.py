"""ToolRegistry 的 Mock 单元测试.

测试策略:
- 用 ``unittest.mock.MagicMock(spec=BaseTool)`` 伪造工具实例(隔离真实工具实现),
  ``spec=BaseTool`` 会让 async 的 ``execute`` 自动变成 ``AsyncMock``, 从而验证:
  注册、查重、定义查询、按名路由执行、kwargs 解包、异常包装、repr 等行为;
- 另用最小真实子类(EchoTool / StrictTool)做集成校验, 防止 Mock 与真实契约脱节.

运行: pytest tests/test_tool_registry.py -v
"""

import subprocess
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from meowmeowclaw.paths import PROJECT_ROOT
from meowmeowclaw.tools import BaseTool
from meowmeowclaw.tools.registry import ToolRegistry

# ---------------------------------------------------------------- 测试替身(hooks)


def make_tool_mock(
    name: str,
    *,
    result: str = "ok",
    error: BaseException | None = None,
    definition: dict[str, Any] | None = None,
) -> MagicMock:
    """构造一个符合 BaseTool 接口的 Mock 工具.

    :param name: 工具名(registry 的注册键)
    :param result: execute 的返回字符串
    :param error: 若给出, execute 被调用时抛出该异常(优先于 result)
    :param definition: 自定义 to_function_definition 返回值
    """
    tool = MagicMock(spec=BaseTool)
    tool.name = name
    tool.to_function_definition.return_value = definition or {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} 的描述",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            "strict": False,
        },
    }
    tool.execute = AsyncMock(return_value=result, side_effect=error)
    return tool


class EchoTool(BaseTool):
    """最小真实工具: 回显 text 参数."""

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "回显工具"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        return f"echo:{kwargs.get('text')}"


class StrictTool(BaseTool):
    """execute 只接受 msg 形参, 用于验证参数不匹配时 registry 的异常包装."""

    @property
    def name(self) -> str:
        return "strict"

    @property
    def description(self) -> str:
        return "严格形参工具"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
            "additionalProperties": False,
        }

    async def execute(self, msg: str) -> str:  # type: ignore[override]
        return f"strict:{msg}"


# ---------------------------------------------------------------- Mock 契约自检


class TestMockToolContract:
    def test_mock_tool_matches_base_tool_interface(self):
        """确认测试替身与真实 BaseTool 契约一致, 避免 Mock 测试失真."""
        tool = make_tool_mock("demo")
        assert isinstance(tool, BaseTool)
        assert tool.name == "demo"
        assert isinstance(tool.execute, AsyncMock)


# ---------------------------------------------------------------- register 注册


class TestRegister:
    def test_register_single_tool(self):
        registry = ToolRegistry()
        tool = make_tool_mock("weather")

        registry.register(tool)

        assert registry.list_tools() == ["weather"]
        assert registry._tools["weather"] is tool  # noqa: SLF001 白盒校验注册的是同一实例

    def test_register_multiple_tools_preserves_insertion_order(self):
        registry = ToolRegistry()
        registry.register(make_tool_mock("b"))
        registry.register(make_tool_mock("a"))
        registry.register(make_tool_mock("c"))

        assert registry.list_tools() == ["b", "a", "c"]

    def test_register_duplicate_name_raises_value_error(self):
        registry = ToolRegistry()
        first = make_tool_mock("dup")
        registry.register(first)

        with pytest.raises(ValueError, match=r"\[dup\]"):
            registry.register(make_tool_mock("dup"))

        # 重复注册不应覆盖原工具
        assert registry._tools["dup"] is first  # noqa: SLF001
        assert registry.list_tools() == ["dup"]

    def test_register_same_instance_twice_raises(self):
        registry = ToolRegistry()
        tool = make_tool_mock("same")
        registry.register(tool)

        with pytest.raises(ValueError):
            registry.register(tool)


# ------------------------------------------------------- get_definitions 定义查询


class TestGetDefinitions:
    def test_empty_registry_returns_empty_list(self):
        assert ToolRegistry().get_definitions() == []

    def test_returns_all_definitions_in_registration_order(self):
        registry = ToolRegistry()
        d1 = {"type": "function", "function": {"name": "t1", "strict": False}}
        d2 = {"type": "function", "function": {"name": "t2", "strict": True}}
        t1 = make_tool_mock("t1", definition=d1)
        t2 = make_tool_mock("t2", definition=d2)
        registry.register(t1)
        registry.register(t2)

        assert registry.get_definitions() == [d1, d2]
        t1.to_function_definition.assert_called_once_with()
        t2.to_function_definition.assert_called_once_with()

    def test_returns_new_list_each_call(self):
        registry = ToolRegistry()
        registry.register(make_tool_mock("t1"))

        assert registry.get_definitions() == registry.get_definitions()
        assert registry.get_definitions() is not registry.get_definitions()

    def test_real_tool_definition_is_forwarded(self):
        registry = ToolRegistry()
        registry.register(EchoTool())

        definition = registry.get_definitions()[0]

        assert definition["type"] == "function"
        assert definition["function"]["name"] == "echo"
        assert definition["function"]["strict"] is False


# ------------------------------------------------------------- execute 路由执行


class TestExecute:
    @pytest.mark.asyncio
    async def test_routes_to_matching_tool_with_unpacked_kwargs(self):
        registry = ToolRegistry()
        weather = make_tool_mock("weather", result="sunny")
        other = make_tool_mock("other", result="should-not-be-called")
        registry.register(weather)
        registry.register(other)

        result = await registry.execute("weather", {"city": "上海", "unit": "c"})

        assert result == "sunny"
        weather.execute.assert_awaited_once_with(city="上海", unit="c")
        other.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_arguments_calls_execute_without_kwargs(self):
        registry = ToolRegistry()
        tool = make_tool_mock("noarg", result="done")
        registry.register(tool)

        assert await registry.execute("noarg", {}) == "done"
        tool.execute.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_text_without_raising(self):
        registry = ToolRegistry()
        tool = make_tool_mock("known")
        registry.register(tool)

        result = await registry.execute("unknown", {"x": 1})

        assert result == "工具调用失败: 不存在名称为 [unknown] 的工具, 请检查工具名称."
        tool.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_tool_on_empty_registry(self):
        result = await ToolRegistry().execute("ghost", {})
        assert "[ghost]" in result

    @pytest.mark.asyncio
    async def test_tool_exception_is_wrapped_as_text(self):
        registry = ToolRegistry()
        registry.register(make_tool_mock("boom", error=RuntimeError("引擎故障")))

        result = await registry.execute("boom", {"x": 1})

        assert result == "执行工具 [boom] 发生异常: 引擎故障"

    @pytest.mark.asyncio
    async def test_bad_arguments_type_error_is_wrapped(self):
        """参数与 execute 形参不匹配时, TypeError 不应冒泡给 Agent."""
        registry = ToolRegistry()
        registry.register(StrictTool())

        result = await registry.execute("strict", {})  # 缺少必需形参 msg

        assert result.startswith("执行工具 [strict] 发生异常:")
        assert "msg" in result

    @pytest.mark.asyncio
    async def test_real_tool_end_to_end(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        registry.register(StrictTool())

        assert await registry.execute("echo", {"text": "hi"}) == "echo:hi"
        assert await registry.execute("strict", {"msg": "hello"}) == "strict:hello"

    @pytest.mark.asyncio
    async def test_unknown_tool_does_not_affect_registered_tools(self):
        registry = ToolRegistry()
        tool = make_tool_mock("real", result="real-result")
        registry.register(tool)

        assert "不存在" in await registry.execute("fake", {})
        assert await registry.execute("real", {}) == "real-result"


# ------------------------------------------------------------------ repr / 其他


class TestRepr:
    def test_repr_empty_registry(self):
        assert repr(ToolRegistry()) == "<ToolRegistry registered_tools=[]>"

    def test_repr_lists_registered_names(self):
        registry = ToolRegistry()
        registry.register(make_tool_mock("alpha"))
        registry.register(make_tool_mock("beta"))

        assert repr(registry) == "<ToolRegistry registered_tools=['alpha', 'beta']>"


# ------------------------------------------------- 包导入边界(无副作用)


class TestToolsPackageImportBoundary:
    """``import meowmeowclaw.tools`` 只应拉起框架, 不应拉起具体工具/配置/上层模块."""

    def test_import_tools_does_not_load_concrete_tools_or_config(self):
        code = (
            "import sys\n"
            "import meowmeowclaw.tools\n"
            "loaded = [name for name in (\n"
            "    'meowmeowclaw.config',\n"
            "    'meowmeowclaw.agent',\n"
            "    'meowmeowclaw.skills',\n"
            "    'meowmeowclaw.tools.filesystem',\n"
            "    'meowmeowclaw.tools.shell',\n"
            "    'meowmeowclaw.tools.web_search',\n"
            "    'meowmeowclaw.tools.web_fetch',\n"
            ") if name in sys.modules]\n"
            "print(','.join(loaded))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""

    def test_exports_only_framework_symbols(self):
        import meowmeowclaw.tools as tools

        assert set(tools.__all__) == {"BaseTool", "ToolRegistry"}
