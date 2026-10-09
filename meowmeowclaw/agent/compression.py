"""上下文 Token 压缩编排: turn 切分、预算估算、请求视图、滚动摘要与确定性降级.

设计见 ``docs/CONTEXT_COMPRESSION_DESIGN.md`` §5. 本模块只负责把事实源 ``messages``
投影成可安全发给 Provider 的 **请求视图**(request view):

- 不改入参、不落盘、不影响 ``AgentTurn.messages`` / ``_session_history``;
- L0: 未超预算时零改写、零额外 LLM 调用;
- L1: 超预算时把"最旧的可压缩完整 turn"交给摘要模型, 成功则用一条
  ``[历史摘要]`` system 消息替换, 并做会话内滚动缓存(前缀不变则复用/合并);
- L2: 摘要不可用/失败/无收益/压缩后仍超预算时, 硬裁同批最旧完整 turn 并插入
  ``[历史省略]`` 占位; keep_recent_turns 配置值放不下时自动降到保留 1 个;
- 绝不切开 assistant.tool_calls ↔ tool 配对.
- L3(当前轮工具占位)/L4(context_overflow) 与 HISTORY.md 审计实现分别在 P5/P4.

保护集(永不压缩): system prompt、最近 ``keep_recent_turns`` 个完整历史 turn、
最后一个 ``role="user"`` 起的当前轮(含工具循环中间的 assistant/tool 消息).
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

from meowmeowclaw.llm.base import FINISH_REASON_ERROR, LLMProvider
from meowmeowclaw.llm.tokenizer import TokenCounter

logger = logging.getLogger(__name__)

# L2 硬裁占位提示: 让模型知道早期上下文被省略, 避免继续编造不存在的细节
HISTORY_OMITTED_TEMPLATE = "[历史省略] 因上下文预算不足，最早的 {count} 轮对话已省略。"
# L1 摘要消息前缀(设计文档 C6)
SUMMARY_HEADER = "[历史摘要]"

# 摘要调用默认值(与 config.py / 设计文档 §8 保持一致)
DEFAULT_SUMMARY_MAX_TOKENS = 768
DEFAULT_SUMMARY_TIMEOUT = 15.0
# 摘要输入序列化后的字符上限(超限则收紧一次; 仍超则放弃摘要走 L2)
SUMMARY_INPUT_CHARS = 24_000
# 摘要收益阈值: 摘要 token 必须 < 被替换内容 token 的 80% 才值得采用
SUMMARY_GAIN_RATIO = 0.8

SUMMARY_SYSTEM_PROMPT = (
    "你是对话上下文压缩器。请把给定的历史对话压缩为一段简洁的中文摘要。\n"
    "必须保留:\n"
    "- 用户偏好、关键事实与约束;\n"
    "- 已确认的结论与决策;\n"
    "- 未完成任务及当前进展;\n"
    "- 关键文件路径、命令、工具执行结果要点;\n"
    "- 重要时间线。\n"
    "禁止编造原文没有的信息; 不要输出寒暄; 直接输出摘要正文。"
)

# 摘要输入各角色的单条上限(字符); 严格模式用于第一次序列化仍超限时的收紧
_SUMMARY_LIMITS = {"user": 2000, "assistant": 2000, "tool": 600, "tool_call": 300}
_SUMMARY_LIMITS_TIGHT = {"user": 500, "assistant": 500, "tool": 200, "tool_call": 100}
# 合并摘要时旧摘要最多保留的字符数
_PRIOR_SUMMARY_CHARS = 4000


class CompressionError(ValueError):
    """压缩配置非法(预算/保留轮数/摘要参数不合法)."""


@runtime_checkable
class AuditSink(Protocol):
    """压缩审计事件接收端(P4 由 HISTORY.md 实现; 失败必须 fail-soft)."""

    async def record(self, event: dict[str, Any]) -> None:
        """记录一条压缩/降级事件."""
        ...


@dataclass(frozen=True)
class CompressionOutcome:
    """一次 ``prepare_request`` 的结果快照(日志 / 测试 / P5 错误路径使用)."""

    messages: list[dict[str, Any]]
    changed: bool
    estimated_tokens: int
    budget: int
    dropped_turns: int = 0
    still_over_budget: bool = False
    summary_applied: bool = False


@dataclass(frozen=True)
class _SummaryResult:
    """一次摘要尝试的结果(内部使用)."""

    ok: bool
    text: str = ""
    tokens: int = 0
    elapsed_ms: int = 0
    reason: str = ""
    cached: bool = False


def split_turns(messages: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """
    把扁平历史按 ``role == "user"`` 切分为完整 turn 列表.

    - 每个 turn 以 user 开头, 依次包含其后的 assistant / tool 消息;
    - 前导非 user 片段(畸形历史)直接跳过并 warning, 避免产生悬空 tool 消息;
    - 只做分组引用, 不修改、不拷贝消息内容.
    """
    turns: list[list[dict[str, Any]]] = []
    skipped = 0
    for message in messages:
        if message.get("role") == "user":
            turns.append([message])
        elif turns:
            turns[-1].append(message)
        else:
            skipped += 1
    if skipped:
        logger.warning("split_turns 跳过 %d 条前导非 user 消息(畸形历史)", skipped)
    return turns


def _find_last_user_index(messages: Sequence[dict[str, Any]]) -> Optional[int]:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    return None


def _flatten(turns: Sequence[Sequence[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [message for turn in turns for message in turn]


def _truncate_text(text: str, limit: int) -> str:
    """head/tail 截断: 保留前 50% + 后 50%, 中间插入省略标记."""
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    if limit <= 3:
        return text[:limit]
    return text[:half] + "\n...(省略)..." + text[-half:]


class ContextCompressor:
    """
    上下文压缩器(每个会话一个实例, 调用方需保证同会话内串行).

    Args:
        counter: token 计数器(P1 ``TokenCounter``)
        token_budget: 输入 token 预算(计数已含 ``SAFETY_FACTOR``, 此处直接比较)
        keep_recent_turns: 压缩时至少保留的最近完整 turn 数; 配置值 >1 且仍超预算时,
            自动降级到保留 1 个(设计文档 C5)
        enabled: 总开关; False 时 ``prepare_request`` 原样返回
        provider: LLM Provider; None 时禁用 L1 摘要, 直接走 L2 硬裁
        model: 主模型名(摘要模型留空时复用)
        summary_model: 摘要模型; 空串表示用主 model
        summary_max_tokens: 摘要输出上限
        summary_timeout: 摘要调用超时(秒)
        session: 会话标识 canonical(写入审计事件, 便于与 JSONL 对齐)
        storage_id: 会话存储 ID(与 sessions/<storage_id>.jsonl 对齐)
        audit_log: 审计事件接收端(P4 注入 HISTORY.md); None 表示不记录

    注意:
        - ``last_outcome`` 保存最近一次结果, 供 AgentLoop / P5 错误路径读取;
        - 摘要缓存是"会话内滚动缓存", 进程重启即失效, 不落盘(设计文档 §5.5).
    """

    def __init__(
        self,
        *,
        counter: TokenCounter,
        token_budget: int,
        keep_recent_turns: int = 2,
        enabled: bool = True,
        provider: Optional[LLMProvider] = None,
        model: Optional[str] = None,
        summary_model: str = "",
        summary_max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS,
        summary_timeout: float = DEFAULT_SUMMARY_TIMEOUT,
        session: str = "",
        storage_id: str = "",
        audit_log: Optional[AuditSink] = None,
    ) -> None:
        if not isinstance(token_budget, int) or isinstance(token_budget, bool):
            raise CompressionError(f"token_budget 必须是正整数: {token_budget!r}")
        if token_budget <= 0:
            raise CompressionError(f"token_budget 必须是正整数: {token_budget!r}")
        if not isinstance(keep_recent_turns, int) or isinstance(keep_recent_turns, bool):
            raise CompressionError(f"keep_recent_turns 必须是正整数: {keep_recent_turns!r}")
        if keep_recent_turns < 1:
            raise CompressionError(f"keep_recent_turns 必须 >= 1: {keep_recent_turns!r}")
        if not isinstance(summary_max_tokens, int) or isinstance(summary_max_tokens, bool):
            raise CompressionError(f"summary_max_tokens 必须是正整数: {summary_max_tokens!r}")
        if summary_max_tokens <= 0:
            raise CompressionError(f"summary_max_tokens 必须是正整数: {summary_max_tokens!r}")
        if not isinstance(summary_timeout, (int, float)) or isinstance(summary_timeout, bool):
            raise CompressionError(f"summary_timeout 必须是正数: {summary_timeout!r}")
        if summary_timeout <= 0:
            raise CompressionError(f"summary_timeout 必须是正数: {summary_timeout!r}")

        self.counter = counter
        self.token_budget = token_budget
        self.keep_recent_turns = keep_recent_turns
        self.enabled = bool(enabled)
        self.provider = provider
        self.model = model
        self.summary_model = summary_model or ""
        self.summary_max_tokens = summary_max_tokens
        self.summary_timeout = float(summary_timeout)
        self.session = session
        self.storage_id = storage_id
        self.audit_log = audit_log
        self.last_outcome: Optional[CompressionOutcome] = None

        # 会话内滚动摘要缓存: 已覆盖消息数 + 前缀哈希
        self._cached_summary: Optional[str] = None
        self._cached_count = 0
        self._cached_hash = ""

    def __repr__(self) -> str:
        return (
            f"<ContextCompressor budget={self.token_budget} "
            f"keep_recent_turns={self.keep_recent_turns} "
            f"counter={self.counter.name!r} "
            f"summary={'on' if self.provider is not None else 'off'} "
            f"enabled={self.enabled}>"
        )

    # ------------------------------------------------------------------ 对外

    async def prepare_request(
        self,
        messages: list[dict[str, Any]],
        tool_defs: Optional[Sequence[dict[str, Any]]] = None,
    ) -> list[dict[str, Any]]:
        """
        生成发往 Provider 的请求视图.

        :param messages: 事实源消息(system + 历史 + 当前轮), 不会被修改
        :param tool_defs: 工具定义, 参与 token 预算
        :return: 未超预算时返回原列表对象; 压缩后返回新列表
        """
        outcome = await self._prepare(messages, tool_defs)
        self.last_outcome = outcome
        return outcome.messages

    # ------------------------------------------------------------------ 主流程

    async def _prepare(
        self,
        messages: list[dict[str, Any]],
        tool_defs: Optional[Sequence[dict[str, Any]]],
    ) -> CompressionOutcome:
        if not messages:
            return CompressionOutcome(
                messages=messages,
                changed=False,
                estimated_tokens=0,
                budget=self.token_budget,
            )

        estimated = self.counter.estimate_request(messages, tool_defs)
        if not self.enabled or estimated <= self.token_budget:
            return CompressionOutcome(
                messages=messages,
                changed=False,
                estimated_tokens=estimated,
                budget=self.token_budget,
            )

        # 保护 system prompt: 约定只有 messages[0] 可能是 system(ContextBuilder 产物)
        has_system = messages[0].get("role") == "system"
        fixed = messages[:1] if has_system else []
        rest = messages[1:] if has_system else list(messages)

        last_user = _find_last_user_index(rest)
        if last_user is None:
            return self._over_budget(messages, estimated, dropped_turns=0)

        protected_current = rest[last_user:]
        turns = split_turns(rest[:last_user])
        if not turns:
            return self._over_budget(messages, estimated, dropped_turns=0)

        best: Optional[CompressionOutcome] = None
        best_dropped: list[dict[str, Any]] = []
        best_keep = 0
        summary_attempted = False
        summary_reason = "summary_disabled" if self.provider is None else ""
        summary_text = ""
        summary_tokens: Optional[int] = None
        summary_elapsed: Optional[int] = None
        summary_cached = False

        for keep in self._keep_attempts():
            if len(turns) <= keep:
                continue
            dropped = turns[:-keep]
            kept = turns[-keep:]
            dropped_messages = _flatten(dropped)

            # L1: 每次压缩事件最多一次摘要调用
            if not summary_attempted:
                summary_attempted = True
                result = await self._summarize(dropped_messages)
                if result.ok:
                    summary_text = result.text
                    summary_tokens = result.tokens
                    summary_elapsed = result.elapsed_ms
                    summary_cached = result.cached
                    candidate = self._build_summary_candidate(
                        fixed, result.text, len(dropped), kept, protected_current
                    )
                    candidate_estimate = self.counter.estimate_request(candidate, tool_defs)
                    if candidate_estimate <= self.token_budget:
                        self._store_cache(result.text, dropped_messages)
                        await self._emit_audit(
                            event="summary",
                            result="ok",
                            reason="cache_hit" if result.cached else "",
                            estimated_before=estimated,
                            estimated_after=candidate_estimate,
                            dropped_turns=len(dropped),
                            original_messages=dropped_messages,
                            summary=result.text,
                            summary_tokens=result.tokens,
                            elapsed_ms=result.elapsed_ms,
                        )
                        logger.info(
                            "历史摘要压缩完成: estimated=%d -> %d, budget=%d, "
                            "dropped_turns=%d, summary_tokens=%d, cached=%s",
                            estimated,
                            candidate_estimate,
                            self.token_budget,
                            len(dropped),
                            result.tokens,
                            result.cached,
                        )
                        return CompressionOutcome(
                            messages=candidate,
                            changed=True,
                            estimated_tokens=candidate_estimate,
                            budget=self.token_budget,
                            dropped_turns=len(dropped),
                            still_over_budget=False,
                            summary_applied=True,
                        )
                    summary_reason = "still_over"
                    logger.info(
                        "摘要已生成但压缩后仍超预算, 降级硬裁: estimated=%d > budget=%d",
                        candidate_estimate,
                        self.token_budget,
                    )
                else:
                    summary_reason = result.reason
                    summary_elapsed = result.elapsed_ms
                    logger.warning("摘要失败(%s), 降级硬裁最旧历史", result.reason)

            # L2: 硬裁同批最旧 turn + 固定占位
            candidate = self._build_candidate(fixed, dropped, kept, protected_current)
            candidate_estimate = self.counter.estimate_request(candidate, tool_defs)
            best = CompressionOutcome(
                messages=candidate,
                changed=True,
                estimated_tokens=candidate_estimate,
                budget=self.token_budget,
                dropped_turns=len(dropped),
                still_over_budget=candidate_estimate > self.token_budget,
            )
            best_dropped = dropped_messages
            best_keep = keep
            if not best.still_over_budget:
                break

        if best is None:
            return self._over_budget(messages, estimated, dropped_turns=0)

        if best.still_over_budget:
            logger.warning(
                "历史硬裁后仍超预算: estimated=%d > budget=%d (dropped_turns=%d, keep=%d)",
                best.estimated_tokens,
                self.token_budget,
                best.dropped_turns,
                best_keep,
            )
        else:
            logger.info(
                "历史硬裁完成: estimated=%d -> %d, budget=%d, dropped_turns=%d, keep=%d",
                estimated,
                best.estimated_tokens,
                self.token_budget,
                best.dropped_turns,
                best_keep,
            )
        await self._emit_audit(
            event="fallback_trim",
            result="failed" if best.still_over_budget else "fallback",
            reason=summary_reason,
            estimated_before=estimated,
            estimated_after=best.estimated_tokens,
            dropped_turns=best.dropped_turns,
            original_messages=best_dropped,
            summary=summary_text or None,
            summary_tokens=summary_tokens,
            elapsed_ms=summary_elapsed,
            cached=summary_cached,
        )
        return best

    def _keep_attempts(self) -> tuple[int, ...]:
        """保留策略: 先按配置值, 若仍超预算再降到 1(设计文档 C5)."""
        if self.keep_recent_turns <= 1:
            return (1,)
        return (self.keep_recent_turns, 1)

    @staticmethod
    def _build_candidate(
        fixed: list[dict[str, Any]],
        dropped: list[list[dict[str, Any]]],
        kept: list[list[dict[str, Any]]],
        protected_current: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        placeholder = {
            "role": "system",
            "content": HISTORY_OMITTED_TEMPLATE.format(count=len(dropped)),
        }
        return [*fixed, placeholder, *_flatten(kept), *protected_current]

    @staticmethod
    def _build_summary_candidate(
        fixed: list[dict[str, Any]],
        summary: str,
        dropped_turns: int,
        kept: list[list[dict[str, Any]]],
        protected_current: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        summary_message = {
            "role": "system",
            "content": (
                f"{SUMMARY_HEADER} {summary}\n"
                f"（此摘要覆盖最早 {dropped_turns} 轮对话）"
            ),
        }
        return [*fixed, summary_message, *_flatten(kept), *protected_current]

    def _over_budget(
        self,
        messages: list[dict[str, Any]],
        estimated: int,
        *,
        dropped_turns: int,
    ) -> CompressionOutcome:
        logger.warning(
            "上下文超预算且无可压缩的完整旧 turn: estimated=%d > budget=%d, "
            "保持原样交给后续 L3/L4 处理",
            estimated,
            self.token_budget,
        )
        return CompressionOutcome(
            messages=messages,
            changed=False,
            estimated_tokens=estimated,
            budget=self.token_budget,
            dropped_turns=dropped_turns,
            still_over_budget=True,
        )

    # ------------------------------------------------------------------ 摘要

    def _cache_hit(self, dropped_messages: Sequence[dict[str, Any]]) -> bool:
        return (
            self._cached_summary is not None
            and self._cached_count == len(dropped_messages)
            and self._cached_hash == self._hash_messages(dropped_messages)
        )

    def _merge_prefix_hit(self, dropped_messages: Sequence[dict[str, Any]]) -> bool:
        return (
            self._cached_summary is not None
            and 0 < self._cached_count < len(dropped_messages)
            and self._cached_hash == self._hash_messages(dropped_messages[: self._cached_count])
        )

    def _store_cache(self, summary: str, dropped_messages: Sequence[dict[str, Any]]) -> None:
        self._cached_summary = summary
        self._cached_count = len(dropped_messages)
        self._cached_hash = self._hash_messages(dropped_messages)

    def _invalidate_cache(self) -> None:
        self._cached_summary = None
        self._cached_count = 0
        self._cached_hash = ""

    @staticmethod
    def _hash_messages(messages: Sequence[dict[str, Any]]) -> str:
        hasher = hashlib.sha256()
        for message in messages:
            hasher.update(
                json.dumps(message, ensure_ascii=False, sort_keys=True, default=str).encode(
                    "utf-8"
                )
            )
            hasher.update(b"\x00")
        return hasher.hexdigest()

    async def _summarize(self, dropped_messages: Sequence[dict[str, Any]]) -> _SummaryResult:
        """执行一次摘要(含缓存命中/合并); 任何失败都返回 ``ok=False`` 而不抛异常."""
        if self.provider is None:
            return _SummaryResult(ok=False, reason="summary_disabled")

        started = time.monotonic()
        prior: Optional[str] = None
        new_messages: Sequence[dict[str, Any]] = dropped_messages
        cached = False

        if self._cache_hit(dropped_messages):
            return _SummaryResult(
                ok=True,
                text=self._cached_summary or "",
                tokens=self.counter.count_text(self._cached_summary or ""),
                elapsed_ms=0,
                reason="cache_hit",
                cached=True,
            )
        if self._merge_prefix_hit(dropped_messages):
            prior = self._cached_summary
            new_messages = dropped_messages[self._cached_count :]
        else:
            self._invalidate_cache()

        payload = self._build_summary_input(prior, new_messages)
        if payload is None:
            return _SummaryResult(
                ok=False, reason="input_too_long", elapsed_ms=self._elapsed_ms(started)
            )

        summary_messages = [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": payload},
        ]
        try:
            response = await asyncio.wait_for(
                self.provider.chat(
                    summary_messages,
                    tools=None,
                    model=self.summary_model or self.model,
                    max_tokens=self.summary_max_tokens,
                ),
                timeout=self.summary_timeout,
            )
        except asyncio.TimeoutError:
            return _SummaryResult(
                ok=False, reason="timeout", elapsed_ms=self._elapsed_ms(started)
            )
        except Exception as exc:  # noqa: BLE001 摘要失败绝不能影响主请求
            logger.warning("摘要调用异常(%s: %s), 降级硬裁", type(exc).__name__, exc)
            return _SummaryResult(
                ok=False, reason="provider_error", elapsed_ms=self._elapsed_ms(started)
            )

        if response.finish_reason == FINISH_REASON_ERROR:
            return _SummaryResult(
                ok=False, reason="error_response", elapsed_ms=self._elapsed_ms(started)
            )

        text = (response.content or "").strip()
        if not text:
            return _SummaryResult(ok=False, reason="empty", elapsed_ms=self._elapsed_ms(started))

        text = self._truncate_summary(text)
        summary_tokens = self.counter.count_text(text)
        dropped_tokens = self.counter.count_messages(dropped_messages)
        if dropped_tokens > 0 and summary_tokens >= SUMMARY_GAIN_RATIO * dropped_tokens:
            logger.warning(
                "摘要收益不足(summary_tokens=%d >= %.0f%% * dropped_tokens=%d), 降级硬裁",
                summary_tokens,
                SUMMARY_GAIN_RATIO * 100,
                dropped_tokens,
            )
            return _SummaryResult(
                ok=False, reason="no_gain", elapsed_ms=self._elapsed_ms(started)
            )

        return _SummaryResult(
            ok=True,
            text=text,
            tokens=summary_tokens,
            elapsed_ms=self._elapsed_ms(started),
            cached=cached,
        )

    def _truncate_summary(self, text: str) -> str:
        """兜底保证摘要不超过 ``summary_max_tokens`` (max_tokens 之外的双保险)."""
        if self.counter.count_text(text) <= self.summary_max_tokens:
            return text
        work = text
        for _ in range(4):
            tokens = self.counter.count_text(work)
            if tokens <= self.summary_max_tokens:
                break
            keep = max(1, int(len(work) * self.summary_max_tokens / max(tokens, 1)))
            if keep >= len(work):
                keep = len(work) - 1
            work = work[:keep].rstrip()
        if self.counter.count_text(work) > self.summary_max_tokens:
            work = work[: self.summary_max_tokens]
        if work and work != text:
            work = f"{work}\n...(摘要超长已截断)"
        return work

    def _build_summary_input(
        self,
        prior: Optional[str],
        messages: Sequence[dict[str, Any]],
    ) -> Optional[str]:
        """序列化摘要输入; 超过 ``SUMMARY_INPUT_CHARS`` 时收紧一次, 仍超返回 None."""
        prior_text = ""
        if prior:
            prior_text = f"[已有摘要]\n{_truncate_text(prior, _PRIOR_SUMMARY_CHARS)}\n\n[新增历史]\n"

        body = self._serialize_messages(messages, _SUMMARY_LIMITS)
        payload = prior_text + body
        if len(payload) <= SUMMARY_INPUT_CHARS:
            return payload

        body = self._serialize_messages(messages, _SUMMARY_LIMITS_TIGHT)
        payload = prior_text + body
        if len(payload) <= SUMMARY_INPUT_CHARS:
            return payload
        return None

    @staticmethod
    def _serialize_messages(
        messages: Sequence[dict[str, Any]],
        limits: dict[str, int],
    ) -> str:
        lines: list[str] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            text = content if isinstance(content, str) else (
                "" if content is None else str(content)
            )
            if role == "user":
                lines.append(f"用户: {_truncate_text(text, limits['user'])}")
            elif role == "assistant":
                if text:
                    lines.append(f"助手: {_truncate_text(text, limits['assistant'])}")
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    name = function.get("name") or "?"
                    arguments = function.get("arguments")
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, ensure_ascii=False, default=str)
                    lines.append(
                        "助手调用工具: "
                        f"{name}({_truncate_text(arguments or '', limits['tool_call'])})"
                    )
            elif role == "tool":
                lines.append(f"工具结果: {_truncate_text(text, limits['tool'])}")
        return "\n".join(line for line in lines if line)

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, int((time.monotonic() - started) * 1000))

    # ------------------------------------------------------------------ 审计

    async def _emit_audit(
        self,
        *,
        event: str,
        result: str,
        reason: str,
        estimated_before: int,
        estimated_after: int,
        dropped_turns: int,
        original_messages: Sequence[dict[str, Any]],
        summary: Optional[str] = None,
        summary_tokens: Optional[int] = None,
        elapsed_ms: Optional[int] = None,
        cached: bool = False,
    ) -> None:
        """把审计事件交给注入的 sink; sink 不存在或异常都 fail-soft."""
        if self.audit_log is None:
            return
        payload = {
            "timestamp_ms": int(time.time() * 1000),
            "session": self.session,
            "storage_id": self.storage_id,
            "event": event,
            "result": result,
            "reason": reason,
            "counter": self.counter.name,
            "budget": self.token_budget,
            "estimated_before": estimated_before,
            "estimated_after": estimated_after,
            "dropped_turns": dropped_turns,
            "summary": summary,
            "summary_tokens": summary_tokens,
            "summary_model": self.summary_model or self.model or "",
            "elapsed_ms": elapsed_ms,
            "cached": cached,
            "original_messages": list(original_messages),
        }
        try:
            await self.audit_log.record(payload)
        except Exception as exc:  # noqa: BLE001 审计失败不能影响压缩/回复
            logger.warning("审计日志记录失败(fail-soft): %r", exc)
