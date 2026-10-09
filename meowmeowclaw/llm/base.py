from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

# finish_reason 取值约定: 前四个与 OpenAI Chat Completions 协议保持字面一致,
# Provider 层因此可以原样透传上游返回值, 上层统一引用下列常量而不是硬编码字符串
FINISH_REASON_STOP = "stop"                    # 正常结束
FINISH_REASON_LENGTH = "length"                # 触达 max_tokens 被截断
FINISH_REASON_TOOL_CALLS = "tool_calls"        # 模型选择调用工具
FINISH_REASON_CONTENT_FILTER = "content_filter"
FINISH_REASON_ERROR = "error"                  # 项目自定义: Provider 调用失败(非服务端返回)


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
    reasoning_content: 模型整体思考文本; 纯文本推理回复(无工具调用)也会保留, 不再只挂在 tool_call 上
    """
    content: Optional[str]
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = FINISH_REASON_STOP     # 取值见 FINISH_REASON_* 常量, tool_calls 代表模型选择调用工具
    usage: dict[str, Any] = None                # token 用量信息
    reasoning_content: Optional[str] = None     # 推理模型输出的思考过程(部分模型会与 content 同时返回)

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
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """
        :param messages: 对话消息数组, OpenAI消息格式
        :param tools: function definition数组, 来自BaseTool.to_function_definition()
        :param model: 指定模型名称
        :param max_tokens: 可选输出上限(如上下文摘要调用); None 表示不传该参数
        :return: LLMResponse 统一封装结果
        """
        ...