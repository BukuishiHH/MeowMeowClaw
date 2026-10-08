"""Agent 主循环: 驱动 "模型 <-> 工具" 的多轮往返, 直到模型给出最终回答.

一轮 run() 的生命周期::

    messages = [system] + 会话历史 + [本次用户消息]
    for 最多 max_iterations 次:
        模型回复 -> 有工具调用: 回填 assistant(tool_calls) + 逐条执行工具 -> 继续
                 -> 无工具调用: 保存整轮历史并返回文本
    超限 -> 返回超时提示

用法::

    loop = AgentLoop(provider=OpenAICompatProvider(...), tools=registry, context=builder)
    print(await loop.run("帮我看下 README"))
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from meowmeowclaw.agent.context import ContextBuilder
from meowmeowclaw.llm.base import (
    FINISH_REASON_ERROR,
    FINISH_REASON_STOP,
    LLMProvider,
    LLMResponse,
)
from meowmeowclaw.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 未显式注入时的兜底迭代上限(生产路径由装配层传入 config.max_iterations)
DEFAULT_MAX_ITERATIONS = 32

# _check_tool_loop 的两种裁决前缀: 调用方据此区分"熔断"(结束本轮)与"警告"(跳过本次执行)
CIRCUIT_BREAK_PREFIX = "[工具熔断]"
LOOP_WARNING_PREFIX = "[工具循环警告]"
# 被跳过的工具调用回填给模型的标记, 提醒它换思路而不是重试同样的调用
SYSTEM_ERROR_PREFIX = "[SYSTEM_ERROR]"

# 同一 (工具名 + 入参) 在滑动窗口内的重复次数阈值与窗口大小
LOOP_WARNING_THRESHOLD = 10        # 达到该次数: 跳过本次执行并回填 SYSTEM_ERROR
LOOP_CIRCUIT_BREAK_THRESHOLD = 20  # 达到该次数: 直接熔断, 结束本轮
TOOL_CALL_WINDOW_SIZE = 30

# run_turn 结果里用于区分"未完成"的结束原因(与 Provider 的 FINISH_REASON_* 区分)
FINISH_REASON_MAX_ITERATIONS = "max_iterations"
FINISH_REASON_CIRCUIT_BREAK = "circuit_break"


@dataclass(frozen=True)
class AgentTurn:
    """一轮 ``AgentLoop.run_turn`` 的完整结果.

    - ``messages``: 本轮新增消息(OpenAI 格式, 不含 system 与既有历史);
      完整跑完时可直接交给 ConversationService 持久化;
    - ``completed``: True 表示模型给出了最终回答; False 表示出错/熔断/超限, 不应写入历史.
    """

    answer: str
    messages: list[dict[str, Any]]
    finish_reason: str
    iterations: int
    completed: bool


class AgentLoop:
    """
    工具调用型 Agent 主循环

    Args:
        provider: LLM Provider(llm.base.LLMProvider 的实现)
        tools: 工具注册表
        context: System Prompt / messages 构建器
        model: 按次覆盖 provider 的默认模型; None 表示用 provider 自己的默认模型
        max_iterations: 单轮对话内 "模型<->工具" 往返次数上限, 防止死循环;
            生产路径由装配层传入 ``.env`` 的 max_iterations; None 表示使用模块兜底值
            DEFAULT_MAX_ITERATIONS(32), 便于直接构造与单测

    注意:
        - ``run(user_message)`` 保留旧行为: 使用并更新实例内 ``_session_history``,
          只有完整跑完的一轮才写入, 错误/熔断/超时过程不写;
        - ``run_turn(user_message, history=...)`` 使用调用方提供的历史快照,
          不修改实例历史, 返回完整轮次结果供上层持久化;
        - reasoning_content 只保留在 LLMResponse 上, 不回填进 messages
          (DeepSeek 等要求多轮时不能回传 reasoning_content);
        - 同一实例不建议并发调用 run(), 内部状态(_session_history 等)未加锁.
    """

    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry,
        context: ContextBuilder,
        model: Optional[str] = None,
        max_iterations: Optional[int] = None,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.context = context
        self.model = model
        # 未显式注入时使用模块兜底值; 生产路径由 bootstrap.build_application 传入配置值
        self.max_iterations = (
            DEFAULT_MAX_ITERATIONS if max_iterations is None else max_iterations
        )
        # 工具调用签名滑动窗口, 用于防爆(同一调用反复重试)
        self._tool_call_history: list[str] = []
        # 跨轮次的会话历史(不含 system, system 每轮由 ContextBuilder 重建)
        self._session_history: list[dict[str, Any]] = []

    def __repr__(self) -> str:
        return (
            f"<AgentLoop model={self.model!r} max_iterations={self.max_iterations} "
            f"tools={self.tools.list_tools()}>"
        )

    # ------------------------------------------------------------------ 主循环

    async def run(self, user_message: str) -> str:
        """
        跑完一轮对话, 返回模型的最终回答文本(兼容旧调用方式)

        新代码建议使用 :meth:`run_turn` 获取完整轮次消息与完成状态.
        """
        turn = await self.run_turn(user_message)
        return turn.answer

    async def run_turn(
        self,
        user_message: str,
        *,
        history: Optional[list[dict[str, Any]]] = None,
    ) -> AgentTurn:
        """
        跑完一轮对话, 返回 :class:`AgentTurn`

        :param user_message: 本次用户输入
        :param history: 外部历史快照(OpenAI messages, 不含 system);
            None 表示使用并更新实例内 ``_session_history``(兼容旧用法);
            传入列表时不修改入参, 也不写入实例历史(由调用方负责持久化)
        :return: 完整轮次结果; 出错/熔断/超限时 ``completed=False``
        """
        use_internal_history = history is None
        base_history = (
            self._session_history
            if use_internal_history
            else [dict(message) for message in history]
        )
        messages = self.context.build_messages(
            history=base_history, current_message=user_message
        )
        # 本轮新增消息的起点: 跳过 [system] 与既有会话历史
        new_start = 1 + len(base_history)
        iterations = 0

        for iterations in range(1, self.max_iterations + 1):
            response = await self.provider.chat(
                messages, tools=self.tools.get_definitions(), model=self.model
            )

            # Provider 层已把异常包装成 finish_reason="error" 的响应
            if response.finish_reason == FINISH_REASON_ERROR:
                logger.warning("模型调用失败, 中止本轮: %s", response.content)
                return AgentTurn(
                    answer=response.content or "模型调用失败",
                    messages=messages[new_start:],
                    finish_reason=FINISH_REASON_ERROR,
                    iterations=iterations,
                    completed=False,
                )

            if response.has_tool_calls:
                messages.append(self._build_assistant_message(response))

                for call in response.tool_calls:
                    args_json = self._serialize_arguments(call.arguments)
                    verdict = self._check_tool_loop(call.name, args_json)

                    if verdict is not None and verdict.startswith(CIRCUIT_BREAK_PREFIX):
                        logger.warning("工具调用熔断, 中止本轮: %s", verdict)
                        return AgentTurn(
                            answer=verdict,
                            messages=messages[new_start:],
                            finish_reason=FINISH_REASON_CIRCUIT_BREAK,
                            iterations=iterations,
                            completed=False,
                        )

                    if verdict is not None:  # 警告: 跳过本次执行, 回填 SYSTEM_ERROR
                        logger.warning("工具调用告警, 跳过本次执行: %s", verdict)
                        messages.append(self._build_skipped_tool_message(call.id, verdict))
                        continue

                    result = await self.tools.execute(call.name, call.arguments)
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": result}
                    )
                continue

            # 模型没有调用工具: 本轮结束
            messages.append({"role": "assistant", "content": response.content or ""})
            new_messages = messages[new_start:]
            if use_internal_history:
                self._save_to_history(new_messages)
            if not response.content:
                logger.warning("模型未返回文本内容, 本轮回答为空")
            return AgentTurn(
                answer=response.content or "",
                messages=new_messages,
                finish_reason=FINISH_REASON_STOP,
                iterations=iterations,
                completed=True,
            )

        timeout_message = (
            f"[错误] 已达到最大迭代次数 {self.max_iterations} 次仍未得到最终回答, 已中止本轮任务. "
            f"建议拆分问题, 或调大 max_iterations."
        )
        logger.warning(timeout_message)
        return AgentTurn(
            answer=timeout_message,
            messages=messages[new_start:],
            finish_reason=FINISH_REASON_MAX_ITERATIONS,
            iterations=iterations,
            completed=False,
        )

    # ------------------------------------------------------------------ 防爆检测

    def _check_tool_loop(self, tool_name: str, tool_args_json: str) -> Optional[str]:
        """
        工具调用防爆检测: 同一 (工具名 + 入参) 反复出现时逐步降级

        :param tool_name: 工具名
        :param tool_args_json: 工具入参的 JSON 字符串(签名的一部分)
        :return: None 表示放行;
                 以 CIRCUIT_BREAK_PREFIX 开头表示熔断(调用方应直接结束本轮);
                 以 LOOP_WARNING_PREFIX 开头表示警告(调用方应跳过本次执行并回填 SYSTEM_ERROR)
        """
        signature = f"{tool_name}:{tool_args_json}"
        repeat_count = self._tool_call_history.count(signature)

        # 与原始规格的差异: 本实现把签名记入窗口**先于**返回值判定.
        # 否则命中"警告"后不再入窗, 计数会永远停在 10, 20 次的"熔断"阈值永远不可达.
        self._tool_call_history.append(signature)
        if len(self._tool_call_history) > TOOL_CALL_WINDOW_SIZE:
            self._tool_call_history.pop(0)

        if repeat_count >= LOOP_CIRCUIT_BREAK_THRESHOLD:
            return (
                f"{CIRCUIT_BREAK_PREFIX} 工具 [{tool_name}] 以相同入参重复调用 {repeat_count} 次, "
                f"疑似陷入死循环, 已中止本轮任务."
            )
        if repeat_count >= LOOP_WARNING_THRESHOLD:
            return (
                f"{LOOP_WARNING_PREFIX} 工具 [{tool_name}] 以相同入参已重复调用 {repeat_count} 次."
            )
        return None

    # ------------------------------------------------------------------ 消息构造

    @staticmethod
    def _serialize_arguments(arguments: dict[str, Any]) -> str:
        """工具入参 dict → OpenAI tool_call.function.arguments 要求的 JSON 字符串."""
        return json.dumps(arguments, ensure_ascii=False)

    def _build_assistant_message(self, response: LLMResponse) -> dict[str, Any]:
        """
        构造带 tool_calls 的 assistant 消息(OpenAI 格式)

        注意:
            - function.arguments 必须是 JSON **字符串**, 而 ToolCallRequest.arguments 是 dict;
            - content 为 None 时回填空串, 兼容对 null 敏感的网关;
            - 不回填 reasoning_content, 避免多轮时被上游拒绝.
        """
        return {
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": self._serialize_arguments(call.arguments),
                    },
                }
                for call in response.tool_calls
            ],
        }

    @staticmethod
    def _build_skipped_tool_message(tool_call_id: str, warning: str) -> dict[str, Any]:
        """被防爆跳过的工具调用也要回填 tool 消息, 否则下一轮请求缺少配对响应会被上游拒绝."""
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": (
                f"{SYSTEM_ERROR_PREFIX} {warning} "
                f"本次调用已被跳过, 请更换思路或改用其它工具, 不要重复同样的调用."
            ),
        }

    # ------------------------------------------------------------------ 历史管理

    def _save_to_history(self, messages_snapshot: list[dict[str, Any]]) -> None:
        """
        保存本轮新增消息到跨轮次会话历史

        :param messages_snapshot: **本轮新增**的消息(不含 system 与既有历史),
                                  调用方通过 messages[new_start:] 切片得到
        """
        self._session_history.extend(messages_snapshot)

    def clear_history(self) -> None:
        """清空工具调用滑窗与跨轮次会话历史."""
        self._tool_call_history.clear()
        self._session_history.clear()
