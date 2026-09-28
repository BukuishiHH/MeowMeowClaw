from typing import Dict, List
from backend.agent.tools import BaseTool

class ToolRegistry:
    """
    工具注册管理器
    负责工具注册、查询定义、路由执行工具调用, 适配OpenClaw风格Agent
    """
    def __init__(self):
        # 存储工具字典: key = 工具name, value=BaseTool实例
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """
        注册单个工具
        :param tool: 继承BaseTool的工具实例
        :raises ValueError: 当同名工具已注册时抛出
        """
        tool_name = tool.name
        if tool_name in self._tools:
            raise ValueError(f"工具名称 [{tool_name}]已经注册, 不能重复注册")
        self._tools[tool_name] = tool

    def get_definitions(self) -> List[dict]:
        """
        获取全部工具OpenAI function calling JSON定义列表
        可直接放到chat completion请求的tools参数
        """
        definitions = []
        for tool in self._tools.values():
            definitions.append(tool.to_function_definition())
        return definitions

    async def execute(self, name: str, arguments: dict) -> str:
        """
        根据工具名 + 参数字典, 路由并异步执行工具
        :param name: 工具名称(LLM返回的function name)
        :param arguments: LLM解析出来的工具参数字典
        :return: 工具执行结果字符串: 异常时返回可读错误文本(交给LLM)
        """
        # 查找工具
        if name not in self._tools:
            return f"工具调用失败: 不存在名称为 [{name}] 的工具, 请检查工具名称."
        tool = self._tools[name]
        try:
            # **解包arguments传给工具execute
            result = await tool.execute(**arguments)
            return result
        except Exception as e:
            # 捕获所有异常，包装成字符串返回，避免Agent中断
            return f"执行工具 [{name}] 发生异常: {str(e)}"

    def list_tools(self) -> List[str]:
        """返回所有已注册工具名称列表"""
        return list(self._tools.keys())

    def __repr__(self):
        return f"<ToolRegistry registered_tools={self.list_tools()}>"