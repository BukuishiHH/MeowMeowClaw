"""OpenAI 兼容协议的 LLM Provider 实现(基于 openai SDK 的 AsyncOpenAI).

凡是实现了 ``/v1/chat/completions`` 协议的服务都可以复用本模块:
OpenAI、DeepSeek、通义千问(DashScope 兼容模式)、vLLM、Ollama、One-API 等,
通常只需切换 ``base_url`` + ``model``.

用法::

    provider = OpenAICompatProvider(
        api_key="sk-xxx",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
    )
    resp = await provider.chat(messages, tools=registry.get_definitions())
    if resp.has_tool_calls:
        ...
    await provider.aclose()
"""

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Optional

from openai import AsyncOpenAI

from meowmeowclaw.llm.base import (
    FINISH_REASON_ERROR,
    FINISH_REASON_STOP,
    FINISH_REASON_TOOL_CALLS,
    LLMProvider,
    LLMResponse,
    StreamEvent,
    StreamFinal,
    StreamTextDelta,
    ToolCallRequest,
)

logger = logging.getLogger(__name__)


def _error_response(exc: BaseException) -> LLMResponse:
    """把异常包装成统一响应, 不让 Provider 层异常炸穿 Agent 主循环."""
    return LLMResponse(
        content=f"[LLM调用失败] {type(exc).__name__}: {exc}",
        finish_reason=FINISH_REASON_ERROR,
    )


class OpenAICompatProvider(LLMProvider):
    """
    基于 openai SDK AsyncOpenAI 的通用 Provider

    Args:
        api_key: 服务端密钥; 传 None 时回退环境变量 OPENAI_API_KEY
        base_url: 服务端地址; 传 None 时回退环境变量 OPENAI_BASE_URL 或官方地址
        model: 默认模型名; 可为 None, 此时必须在 chat(model=...) 中按次指定

    注意:
        - 无可用密钥时, AsyncOpenAI 在构造阶段直接抛 OpenAIError(快速失败, 不打哑炮);
        - api_key 不会出现在 __repr__ 与日志中;
        - 应用退出时调用 ``await provider.aclose()`` 释放连接池.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    def __repr__(self) -> str:
        masked_key = "***" if self.api_key else None
        return (
            f"<OpenAICompatProvider model={self.model!r} "
            f"base_url={self.base_url!r} api_key={masked_key}>"
        )

    async def aclose(self) -> None:
        """关闭底层 HTTP 连接池(应用退出时调用)."""
        await self.client.close()

    # ------------------------------------------------------------------ 对外主方法

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        target_model = model or self.model
        if not target_model:
            return _error_response(ValueError("未指定模型名, 请在构造参数或 chat(model=...) 中指定"))

        request_kwargs = self._build_request_kwargs(messages, tools, target_model, max_tokens)

        try:
            completion = await self.client.chat.completions.create(**request_kwargs)
        except Exception as exc:  # noqa: BLE001 只兜 Exception; CancelledError 等 BaseException 继续上抛
            logger.exception(
                "调用 OpenAI 兼容接口失败: model=%s, base_url=%s", target_model, self.base_url
            )
            return _error_response(exc)

        return self._parse_completion(completion)

    @staticmethod
    def _build_request_kwargs(
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]],
        model: str,
        max_tokens: Optional[int],
    ) -> dict[str, Any]:
        """构造 create() 的公共参数(非流式/流式共用).

        仅在确有工具定义时才带 tools / tool_choice: 空列表或 None 一律不传,
        避免部分网关(vLLM/One-API 等)直接返回 400.
        """
        request_kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if max_tokens is not None:
            # 摘要调用等场景需要限制输出长度; 不传则沿用服务端默认
            request_kwargs["max_tokens"] = int(max_tokens)
        if tools:
            request_kwargs["tools"] = tools
            request_kwargs["tool_choice"] = "auto"
        return request_kwargs

    # ------------------------------------------------------------------ 流式接口

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[StreamEvent]:
        """流式对话: 逐条产出文本增量, 结束时产出与非流式结构一致的 ``StreamFinal``.

        - ``tool_calls`` 以 index 分片到达: ``arguments`` 逐片**字符串拼接**, 结束后统一解析;
        - ``reasoning_content`` 只累积进最终 ``LLMResponse``, 不作为文本增量外发;
        - 空流(服务端不支持流式)回退一次非流式 ``chat()``;
        - 流中断保留已生成文本, 并追加 ``[流式输出中断: ...]``, ``finish_reason="error"``.
        """
        target_model = model or self.model
        if not target_model:
            yield StreamFinal(
                _error_response(ValueError("未指定模型名, 请在构造参数或 chat(model=...) 中指定"))
            )
            return

        request_kwargs = self._build_request_kwargs(messages, tools, target_model, max_tokens)
        request_kwargs["stream"] = True
        request_kwargs["stream_options"] = {"include_usage": True}

        try:
            stream = await self._open_stream(request_kwargs, target_model)
        except Exception as exc:  # noqa: BLE001 建流失败按统一错误响应返回
            logger.exception(
                "调用 OpenAI 兼容流式接口失败: model=%s, base_url=%s", target_model, self.base_url
            )
            yield StreamFinal(_error_response(exc))
            return

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_slots: dict[int, dict[str, str]] = {}
        finish_reason: Optional[str] = None
        usage: dict[str, Any] = {}
        chunk_count = 0

        try:
            async for chunk in stream:
                chunk_count += 1
                usage = self._extract_usage(getattr(chunk, "usage", None)) or usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue  # include_usage 的尾块: 只有 usage, 没有 choices
                choice = choices[0]
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                text = getattr(delta, "content", None)
                if text:
                    content_parts.append(text)
                    yield StreamTextDelta(text)
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    reasoning_parts.append(str(reasoning))
                for raw_call in getattr(delta, "tool_calls", None) or []:
                    self._accumulate_tool_call_delta(tool_slots, raw_call)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 流中断: 保留已生成文本
            logger.exception("流式响应读取失败: model=%s", target_model)
            partial = "".join(content_parts)
            answer = (
                f"{partial}\n\n[流式输出中断: {type(exc).__name__}: {exc}]"
                if partial
                else f"[LLM调用失败] {type(exc).__name__}: {exc}"
            )
            yield StreamFinal(
                LLMResponse(content=answer, finish_reason=FINISH_REASON_ERROR, usage=usage)
            )
            return

        if chunk_count == 0:
            # 服务端返回空流(不支持 streaming): 回退一次非流式调用
            logger.warning("流式接口返回空流, 回退非流式调用: model=%s", target_model)
            yield StreamFinal(
                await self.chat(messages, tools=tools, model=model, max_tokens=max_tokens)
            )
            return

        tool_calls = self._finalize_stream_tool_calls(tool_slots)
        yield StreamFinal(
            LLMResponse(
                content="".join(content_parts) or None,
                tool_calls=tool_calls,
                # 上游未给 finish_reason 时按"有工具调用/纯文本"兜底
                finish_reason=finish_reason
                or (FINISH_REASON_TOOL_CALLS if tool_calls else FINISH_REASON_STOP),
                usage=usage,
                reasoning_content="".join(reasoning_parts) or None,
            )
        )

    async def _open_stream(self, request_kwargs: dict[str, Any], target_model: str) -> Any:
        """建立流式连接; 部分网关不认 ``stream_options``, 400 时去掉重试一次(仍失败则抛出)."""
        try:
            return await self.client.chat.completions.create(**request_kwargs)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status_code", None) != 400 or "stream_options" not in request_kwargs:
                raise
            retry_kwargs = dict(request_kwargs)
            retry_kwargs.pop("stream_options", None)
            logger.warning("网关不认 stream_options, 去掉后重试: model=%s", target_model)
            return await self.client.chat.completions.create(**retry_kwargs)

    @staticmethod
    def _accumulate_tool_call_delta(
        slots: dict[int, dict[str, str]], raw_call: Any
    ) -> None:
        """把流式 tool_call 分片累加到 index 对应槽位(arguments 必须字符串拼接, 不能逐片解析)."""
        index = getattr(raw_call, "index", None)
        if index is None:
            index = 0 if not slots else max(slots) + 1
        slot = slots.setdefault(int(index), {"id": "", "name": "", "arguments": ""})
        call_id = getattr(raw_call, "id", None)
        if call_id:
            slot["id"] = str(call_id)
        function = getattr(raw_call, "function", None)
        if function is not None:
            name = getattr(function, "name", None)
            if name:
                slot["name"] = str(name)
            arguments = getattr(function, "arguments", None)
            if arguments:
                slot["arguments"] += str(arguments)

    def _finalize_stream_tool_calls(
        self, slots: dict[int, dict[str, str]]
    ) -> list[ToolCallRequest]:
        """流结束后把槽位转成 ToolCallRequest(arguments 统一 JSON 解析)."""
        converted: list[ToolCallRequest] = []
        for index in sorted(slots):
            slot = slots[index]
            if not slot["name"] and not slot["arguments"]:
                continue  # 只收到 id 的空槽位(部分网关会先发 id)直接忽略
            converted.append(
                ToolCallRequest(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=self._parse_arguments(slot["arguments"], slot["name"]),
                )
            )
        return converted

    # ------------------------------------------------------------------ 响应解析

    @staticmethod
    def _parse_completion(completion: Any) -> LLMResponse:
        """把 SDK 的 ChatCompletion 转成项目统一的 LLMResponse."""
        choices = getattr(completion, "choices", None) or []
        if not choices:
            # 网关异常时可能返回空 choices, 这里给出可读错误而不是 IndexError
            return _error_response(RuntimeError("模型返回结果为空(choices 为空)"))

        choice = choices[0]
        message = getattr(choice, "message", None)
        if message is None:
            return _error_response(RuntimeError("模型返回结果缺少 message 字段"))

        return LLMResponse(
            content=getattr(message, "content", None),
            tool_calls=OpenAICompatProvider._convert_tool_calls(
                getattr(message, "tool_calls", None), message
            ),
            # finish_reason 原样透传(取值与 FINISH_REASON_* 常量字面一致),
            # 上游未返回时按正常结束处理; "tool_calls" 表示模型选择调用工具
            finish_reason=getattr(choice, "finish_reason", None) or FINISH_REASON_STOP,
            usage=OpenAICompatProvider._extract_usage(getattr(completion, "usage", None)),
            # message 顶层思考文本: 纯文本推理回复(无 tool_calls)靠它保留下来
            reasoning_content=getattr(message, "reasoning_content", None),
        )

    @staticmethod
    def _convert_tool_calls(raw_tool_calls: Any, message: Any) -> list[ToolCallRequest]:
        """把 SDK 的 tool_calls 转成 ToolCallRequest 列表."""
        if not raw_tool_calls:
            return []

        # 部分推理模型(DeepSeek-R1 / 通义 QwQ 等)把思考过程挂在 message 顶层, 而不是 tool_call 上
        message_reasoning = getattr(message, "reasoning_content", None)

        converted: list[ToolCallRequest] = []
        for tool_call in raw_tool_calls:
            function = getattr(tool_call, "function", None)
            # getattr 容错: SDK 的 tool_call 结构本没有 reasoning_content 字段,
            # 但服务端多返回的字段会被 pydantic 的 extra="allow" 保留下来
            reasoning = getattr(tool_call, "reasoning_content", None)
            if reasoning is None:
                reasoning = message_reasoning

            converted.append(
                ToolCallRequest(
                    id=getattr(tool_call, "id", None) or "",
                    name=getattr(function, "name", None) or "",
                    arguments=OpenAICompatProvider._parse_arguments(
                        getattr(function, "arguments", None), getattr(function, "name", None)
                    ),
                    reasoning_content=reasoning,
                )
            )
        return converted

    @staticmethod
    def _parse_arguments(raw_arguments: Any, tool_name: Optional[str] = None) -> dict[str, Any]:
        """tool_call.function.arguments 是 JSON 字符串, 需解析成 dict 才能 ** 解包给 execute."""
        if isinstance(raw_arguments, dict):  # 少数网关直接返回对象
            return raw_arguments
        if not raw_arguments:
            return {}
        try:
            parsed = json.loads(raw_arguments)
        except (TypeError, ValueError):
            # 模型偶发输出非法 JSON: 降级为空参数, 让工具自己报缺参, 不要整条响应炸掉
            logger.warning(
                "工具 [%s] 的 arguments 不是合法 JSON, 已降级为空参数: %r",
                tool_name,
                raw_arguments,
            )
            return {}
        if not isinstance(parsed, dict):
            logger.warning(
                "工具 [%s] 的 arguments 不是 JSON 对象, 已降级为空参数: %r", tool_name, raw_arguments
            )
            return {}
        return parsed

    @staticmethod
    def _extract_usage(raw_usage: Any) -> dict[str, Any]:
        """提取 token 用量; 部分网关不返回 usage, 此时给空 dict."""
        if raw_usage is None:
            return {}
        if isinstance(raw_usage, dict):
            return raw_usage
        model_dump = getattr(raw_usage, "model_dump", None)
        if callable(model_dump):
            # exclude_none: 避免把 SDK 中未填充的 *_details 字段以 null 形式带进来
            return model_dump(exclude_none=True)
        try:
            return dict(raw_usage)
        except (TypeError, ValueError):
            logger.warning("无法解析 usage: %r", raw_usage)
            return {}
