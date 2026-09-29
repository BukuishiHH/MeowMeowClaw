from typing import Any, Optional
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolCallRequest:
    """
    统一工具调用请求结构, 用于封装LLM输出的tool_call信息
    reasoning_content: 部分推理模型附带的思考过程
    """
    id: str
    name: str                                   # 工具名称
    arguments: dict[str, Any]                   # 工具入参, 传给 execute(**kwargs) 的参数字典
    reasoning_content: Optional[str] = None     # 推理模型输出的内部思考文本

@dataclass
class LLMResponse:
    """
    LLM统一返回结构体, 兼容普通文本回复 + 工具调用返回
    """
    content: Optional[str]
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"                 # 结束原因, stop正常结束; tool_call 代表模型选择调用工具
    usage: dict[str, Any] = None                # token 用量信息

    def __post_init__(self):
        # 默认usage为空dict
        if self.usage is None:
            self.usage = {}

    @property
    def has_tool_calls(self) -> bool:
        """是否存在工具调用"""
        return len(self.tool_calls) > 0

class LLMProvider(ABC):
    """
    LLM提供者抽象基类
    所有大模型后端都继承此类, 实现chat异步方法
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None
    ) -> LLMResponse:
        """
        :param messages: 对话消息数组, OpenAI消息格式
        :param tools: function definition数组, 来自BaseTool.to_function_definition()
        :param model: 指定模型名称
        :return: LLMResponse 统一封装结果
        """
        ...