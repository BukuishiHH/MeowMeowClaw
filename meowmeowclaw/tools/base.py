from abc import ABC, abstractmethod
from typing import Any


class BaseTool(ABC):
    """
    自定义智能体工具基类, 参考OpenClaw AgentTool + OpenAI function calling规范
    所有自定义工具必须继承此类, 并实现 name / description / parameters 抽象属性与execute异步方法
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """
        [抽象属性] 工具名称
        LLM调用工具时使用的标识符, 不能包含空格, 推荐下划线命名, 如get_weather
        """
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """
        [抽象属性] 工具功能描述
        给LLM阅读, 清晰说明什么时候调用这个工具、用途是什么
        """
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """
        [抽象属性] 参数JSON Schema(OpenAI标准)
        必须是字典, 结构示例:
        {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名"}
            },
            "required": ["city"],
            "additionalProperties": False
        }
        """
        ...

    @property
    def strict(self) -> bool:
        """
        [抽象属性] OpenAI strict模式开关
        开启后强制模型输出严格匹配JSON schema, 默认为False
        子类可重写此属性开启strict
        """
        return False

    @property
    def label(self) -> str:
        """
        [可选属性] 人类可读的工具展示名(对齐OpenClaw)
        用于日志、UI展示, 不传给LLM, 默认直接复用name
        """
        return self.name

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """
        [抽象异步方法] 工具执行逻辑
        :param kwargs: LLM解析出的工具参数字典
        :return: str, 工具执行结果, 会回传给LLM作为tool返回内容
        异常建议在内部捕获, 包装为字符串返回给大模型
        """
        ...

    def to_function_definition(self) -> dict[str, Any]:
        """
        组装为OpenAI Chat Completions API要求的tools定义dict
        可直接放入请求体 tools 数组
        """
        func_def = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
                # 显式输出strict字段, 默认False(非严格模式), 子类可重写strict开启
                "strict": self.strict
            }
        }
        return func_def

    def __repr__(self) -> str:
        """调试打印"""
        return f"<Tool name={self.name}, label={self.label}>"