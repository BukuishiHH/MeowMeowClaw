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

import asyncio
import json
import logging
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from meowmeowclaw.agent.compression import ContextCompressor
from meowmeowclaw.agent.context import ContextBuilder
from meowmeowclaw.llm.base import (
    FINISH_REASON_ERROR,
    FINISH_REASON_STOP,
    LLMProvider,
    LLMResponse,
    StreamFinal,
    StreamTextDelta,
)
from meowmeowclaw.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 流式增量回调: 渲染层实现(CLI 打印 / QQ 分段); 失败只告警, 不影响本轮结果
DeltaCallback = Callable[[str], Awaitable[None]]

# 工具轮"前导文本"抑制宽限(见 docs/STREAMING_DESIGN.md §4.3):
# 缓冲达到任一阈值且未发现 tool_calls 才放行, 避免"我先查一下…"之类旁白变成气泡
DEFAULT_PREAMBLE_GRACE_MS = 1500
DEFAULT_PREAMBLE_GRACE_CHARS = 40

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
# L4: 压缩(含 L3 工具占位)后仍超预算, 不调用 Provider, 直接结束本轮
FINISH_REASON_CONTEXT_OVERFLOW = "context_overflow"
# L4 固定用户提示(设计文档确认口径, 不在回复里追加其它压缩提示)
CONTEXT_OVERFLOW_MESSAGE = "上下文超出预算，请拆分问题或使用 /clear"


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


@dataclass
class AgentTurnContext:
    """当前 ``AgentLoop.run_turn`` 的上下文(SA2).

    - ``turn_id``: 本次 turn 的随机 ID; root 主代理的 SpawnSubagentTool 用它做预算归属与审计;
    - ``budget``: 该 turn 的子代理预算(延迟创建, 由 SpawnSubagentTool 首次 spawn 时写入);
      预算对象挂在每个 task 自己的上下文对象上, 并发会话之间天然隔离;
    - 子代理 ``run_turn`` 会设置自己的上下文, 并在结束时用 token 恢复父上下文。
    """

    turn_id: str
    budget: Any = None

    def ensure_budget(self, factory: Callable[[], Any]) -> Any:
        """延迟创建并返回本 turn 的子代理预算; 同 turn 内多次 spawn 复用同一对象."""
        if self.budget is None:
            self.budget = factory()
        return self.budget


CURRENT_AGENT_TURN: ContextVar[Optional[AgentTurnContext]] = ContextVar(
    "meowmeowclaw_current_agent_turn", default=None
)


def current_agent_turn() -> Optional[AgentTurnContext]:
    """返回当前 task 正在执行的 agent turn 上下文; 不在 run_turn 内时返回 None."""
    return CURRENT_AGENT_TURN.get()


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
        compressor: 可选的上下文压缩器; 每次 chat 前生成请求视图(超预算时硬裁/摘要),
            None(默认)时行为与不压缩完全一致

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
        compressor: Optional[ContextCompressor] = None,
        *,
        stream_preamble_grace_ms: int = DEFAULT_PREAMBLE_GRACE_MS,
        stream_preamble_grace_chars: int = DEFAULT_PREAMBLE_GRACE_CHARS,
        suppress_tool_preamble: bool = True,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.context = context
        self.model = model
        # 可选: 每次 provider.chat 前把事实源投影成压缩后的请求视图; None 表示不压缩
        self.compressor = compressor
        # 流式前导抑制参数(见 docs/STREAMING_DESIGN.md §4.3)
        self.stream_preamble_grace_ms = max(0, int(stream_preamble_grace_ms))
        self.stream_preamble_grace_chars = max(0, int(stream_preamble_grace_chars))
        self.suppress_tool_preamble = bool(suppress_tool_preamble)
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
            f"compressor={self.compressor is not None} tools={self.tools.list_tools()}>"
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
        on_delta: Optional[DeltaCallback] = None,
    ) -> AgentTurn:
        """跑完一轮对话, 并在本次 turn 期间设置 ``CURRENT_AGENT_TURN``.

        子代理 factory 通过该上下文拿到 root turn_id 做预算/审计归属;
        子代理自身的 ``run_turn`` 会设置自己的 turn 上下文, 结束时用 token 恢复父上下文。
        """
        token: Token = CURRENT_AGENT_TURN.set(AgentTurnContext(turn_id=uuid.uuid4().hex))
        try:
            return await self._run_turn_impl(
                user_message, history=history, on_delta=on_delta
            )
        finally:
            CURRENT_AGENT_TURN.reset(token)

    async def _run_turn_impl(
        self,
        user_message: str,
        *,
        history: Optional[list[dict[str, Any]]] = None,
        on_delta: Optional[DeltaCallback] = None,
    ) -> AgentTurn:
        """
        跑完一轮对话, 返回 :class:`AgentTurn`

        :param user_message: 本次用户输入
        :param history: 外部历史快照(OpenAI messages, 不含 system);
            None 表示使用并更新实例内 ``_session_history``(兼容旧用法);
            传入列表时不修改入参, 也不写入实例历史(由调用方负责持久化)
        :param on_delta: 可选的流式增量回调; None(默认)时**完全走非流式路径**,
            行为与现状逐字节一致; 传入时该轮改用 ``provider.chat_stream()``,
            并把"最终回答"的文本增量交给回调(工具轮旁白按 §4.3 策略抑制)
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
            tool_defs = self.tools.get_definitions()
            # C1 请求视图隔离: 压缩只影响发给 Provider 的副本,
            # 事实源 messages / new_start / AgentTurn.messages 保持完整
            request_messages = messages
            if self.compressor is not None:
                request_messages = await self.compressor.prepare_request(messages, tool_defs)
                outcome = self.compressor.last_outcome
                if outcome is not None and outcome.still_over_budget:
                    # L4: 已无可压缩内容(含当前轮工具占位), 不发请求, 明确告知用户
                    logger.warning(
                        "上下文压缩后仍超预算, 中止本轮: estimated=%d > budget=%d",
                        outcome.estimated_tokens,
                        outcome.budget,
                    )
                    return AgentTurn(
                        answer=CONTEXT_OVERFLOW_MESSAGE,
                        messages=messages[new_start:],
                        finish_reason=FINISH_REASON_CONTEXT_OVERFLOW,
                        iterations=iterations,
                        completed=False,
                    )
            response = (
                await self.provider.chat(request_messages, tools=tool_defs, model=self.model)
                if on_delta is None
                else await self._chat_streaming(request_messages, tool_defs, on_delta)
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

    # ------------------------------------------------------------------ 流式

    async def _chat_streaming(
        self,
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]],
        on_delta: DeltaCallback,
    ) -> LLMResponse:
        """走 Provider 流式接口, 只把"最终回答"的增量外发(工具轮旁白按策略抑制).

        见 docs/STREAMING_DESIGN.md §4.3:
        - 文本增量先进入缓冲, 达到宽限(字符数或时间)且未发现 tool_calls 才放行;
        - 一旦发现 tool_calls, 未放行的缓冲直接丢弃; 已放行的保留(QQ 无法编辑, 记为已知残余风险);
        - 短回答在宽限内就结束: 结束时一次性补发;
        - 回调异常不影响本轮结果(渲染层失败不允许炸穿 Agent).
        """
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        buffer: list[str] = []
        released = False
        tool_seen = False
        response: Optional[LLMResponse] = None

        try:
            stream = self.provider.chat_stream(messages, tools=tool_defs, model=self.model)
            async for event in stream:
                if isinstance(event, StreamTextDelta):
                    if tool_seen:
                        continue
                    if released:
                        await self._emit(on_delta, event.text)
                        continue
                    buffer.append(event.text)
                    pending = "".join(buffer)
                    grace_over = (
                        len(pending) >= self.stream_preamble_grace_chars
                        or (loop.time() - started_at) * 1000 >= self.stream_preamble_grace_ms
                    )
                    if not self.suppress_tool_preamble or grace_over:
                        released = True
                        buffer.clear()
                        await self._emit(on_delta, pending)
                    continue
                if isinstance(event, StreamFinal):
                    response = event.response
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 流式实现抛异常: 回退非流式, 保证本轮可用
            logger.warning("流式调用失败, 回退非流式: %r", exc)
            return await self.provider.chat(messages, tools=tool_defs, model=self.model)

        if response is None:  # pragma: no cover - Provider 契约保证最终必出 StreamFinal
            logger.warning("流式调用未返回最终结果, 回退非流式")
            return await self.provider.chat(messages, tools=tool_defs, model=self.model)

        if response.has_tool_calls:
            if released:
                logger.warning(
                    "流式前导文本已外发后才出现工具调用(QQ 侧无法编辑, 属已知残余风险): chars=%d",
                    len(response.content or ""),
                )
            else:
                buffer.clear()  # 未外发的旁白丢弃
        elif not released and buffer:
            # 宽限期内就结束的短回答: 此时补发, 保证内容不丢
            pending = "".join(buffer)
            buffer.clear()
            released = True
            await self._emit(on_delta, pending)

        return response

    @staticmethod
    async def _emit(on_delta: DeltaCallback, text: str) -> None:
        """外发一段增量; 渲染侧任何异常只告警(不允许影响本轮结果)."""
        if not text:
            return
        try:
            await on_delta(text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("流式增量外发失败(已忽略): chars=%d", len(text))

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
