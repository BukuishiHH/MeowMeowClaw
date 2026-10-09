"""上下文 Token 压缩编排: turn 切分、预算估算、请求视图与确定性硬裁兜底.

设计见 ``docs/CONTEXT_COMPRESSION_DESIGN.md`` §5. 本模块只负责把事实源 ``messages``
投影成可安全发给 Provider 的 **请求视图**(request view):

- 不改入参、不落盘、不影响 ``AgentTurn.messages`` / ``_session_history``;
- L0: 未超预算时零改写、零额外 LLM 调用;
- L2(P2 范围): 超预算且摘要不可用/未接入时, 按"完整 turn"硬裁最旧部分,
  并插入 ``[历史省略]`` 占位消息; 绝不切开 assistant.tool_calls ↔ tool 配对;
- L1 摘要(P3)/审计日志(P4)/L3~L4(P5) 在后续阶段接入.

保护集(永不压缩): system prompt、最近 ``keep_recent_turns`` 个完整历史 turn、
最后一个 ``role="user"`` 起的当前轮(含工具循环中间的 assistant/tool 消息).
"""

import logging
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from meowmeowclaw.llm.tokenizer import TokenCounter

logger = logging.getLogger(__name__)

# L2 硬裁占位提示: 让模型知道早期上下文被省略, 避免继续编造不存在的细节
HISTORY_OMITTED_TEMPLATE = "[历史省略] 因上下文预算不足，最早的 {count} 轮对话已省略。"


class CompressionError(ValueError):
    """压缩配置非法(预算/保留轮数不合法)."""


@dataclass(frozen=True)
class CompressionOutcome:
    """一次 ``prepare_request`` 的结果快照(日志 / 测试 / P5 错误路径使用)."""

    messages: list[dict[str, Any]]
    changed: bool
    estimated_tokens: int
    budget: int
    dropped_turns: int = 0
    still_over_budget: bool = False
    summary_applied: bool = False  # P3 摘要接入后置 True


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


class ContextCompressor:
    """
    上下文压缩器(每个会话一个实例, 调用方需保证同会话内串行).

    Args:
        counter: token 计数器(P1 ``TokenCounter``)
        token_budget: 输入 token 预算(计数已含 ``SAFETY_FACTOR``, 此处直接比较)
        keep_recent_turns: 硬裁时至少保留的最近完整 turn 数; 配置值 >1 时若仍超预算,
            自动降级到保留 1 个(设计文档 C5)
        enabled: 总开关; False 时 ``prepare_request`` 原样返回

    注意:
        - ``prepare_request`` 是 async(P3 摘要调用需要), P2 内部无 await;
        - ``last_outcome`` 保存最近一次结果, 供 AgentLoop / P5 错误路径读取.
    """

    def __init__(
        self,
        *,
        counter: TokenCounter,
        token_budget: int,
        keep_recent_turns: int = 2,
        enabled: bool = True,
    ) -> None:
        if not isinstance(token_budget, int) or isinstance(token_budget, bool):
            raise CompressionError(f"token_budget 必须是正整数: {token_budget!r}")
        if token_budget <= 0:
            raise CompressionError(f"token_budget 必须是正整数: {token_budget!r}")
        if not isinstance(keep_recent_turns, int) or isinstance(keep_recent_turns, bool):
            raise CompressionError(f"keep_recent_turns 必须是正整数: {keep_recent_turns!r}")
        if keep_recent_turns < 1:
            raise CompressionError(f"keep_recent_turns 必须 >= 1: {keep_recent_turns!r}")

        self.counter = counter
        self.token_budget = token_budget
        self.keep_recent_turns = keep_recent_turns
        self.enabled = bool(enabled)
        self.last_outcome: Optional[CompressionOutcome] = None

    def __repr__(self) -> str:
        return (
            f"<ContextCompressor budget={self.token_budget} "
            f"keep_recent_turns={self.keep_recent_turns} "
            f"counter={self.counter.name!r} enabled={self.enabled}>"
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
        :return: 未超预算时返回原列表对象; 硬裁后返回新列表
        """
        outcome = self._prepare(messages, tool_defs)
        self.last_outcome = outcome
        return outcome.messages

    # ------------------------------------------------------------------ 内部

    def _prepare(
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
        for keep in self._keep_attempts():
            if len(turns) <= keep:
                continue
            dropped = turns[:-keep]
            kept = turns[-keep:]
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
            if not best.still_over_budget:
                break

        if best is None:
            return self._over_budget(messages, estimated, dropped_turns=0)

        if best.still_over_budget:
            logger.warning(
                "历史硬裁后仍超预算: estimated=%d > budget=%d (dropped_turns=%d, keep>=1)",
                best.estimated_tokens,
                self.token_budget,
                best.dropped_turns,
            )
        else:
            logger.info(
                "历史硬裁完成: estimated=%d -> %d, budget=%d, dropped_turns=%d",
                estimated,
                best.estimated_tokens,
                self.token_budget,
                best.dropped_turns,
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
