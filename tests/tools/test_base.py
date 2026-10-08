import pytest
from meowmeowclaw.tools import BaseTool
from typing import Any

class MinimalTool(BaseTool):
    @property
    def name(self) -> str:
        return "minimal_tool"

    @property
    def description(self) -> str:
        return "最小测试工具"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        return f"exec result: {kwargs}"

def test_base_tool_concrete_subclass():
    tool = MinimalTool()
    assert tool.label == "minimal_tool"  # 默认label复用name
    func_def = tool.to_function_definition()
    assert func_def["function"]["name"] == "minimal_tool"
    assert func_def["function"]["strict"] is False

@pytest.mark.asyncio
async def test_minimal_tool_execute():
    tool = MinimalTool()
    ret = await tool.execute(msg="hello")
    assert ret == "exec result: {'msg': 'hello'}"

def test_abc_abstract_enforce():
    # 缺任意抽象成员, 实例化直接抛TypeError, 验证ABC约束生效
    class BadTool(BaseTool):
        # 故意不实现name、description等抽象属性
        pass

    with pytest.raises(TypeError):
        BadTool()