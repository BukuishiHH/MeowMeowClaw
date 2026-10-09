"""meowmeowclaw/agent/compression.py 的单元测试(P2: L0 短路 + L2 硬裁).

测试策略:
- 用 ``CharCounter``(1 字符 = 1 token)替代真实分词器, 让预算断言完全确定、离线可跑;
- 覆盖: turn 切分/保护集/配对完整性/预算边界/keep_recent 降级/无可裁内容/工具定义计入;
- 关键不变量: 压缩只影响返回的请求视图, 入参事实源必须逐字节不变.

运行: pytest tests/agent/test_compression.py -v
"""

import json
from typing import Any, Optional, Sequence

import pytest

from meowmeowclaw.agent.compression import (
    HISTORY_OMITTED_TEMPLATE,
    CompressionError,
    ContextCompressor,
    split_turns,
)


# --------------------------------------------------------------------- 测试替身


class CharCounter:
    """确定性计数替身: content/tool_calls 按字符数计, 不含安全系数."""

    name = "char"

    def count_text(self, text: str) -> int:
        return len(text or "")

    def count_message(self, message: dict[str, Any]) -> int:
        total = len(message.get("content") or "")
        tool_calls = message.get("tool_calls")
        if tool_calls:
            total += len(json.dumps(tool_calls, ensure_ascii=False))
        return total

    def count_messages(self, messages: Sequence[dict[str, Any]]) -> int:
        return sum(self.count_message(message) for message in messages)

    def count_tools(self, tool_defs: Optional[Sequence[dict[str, Any]]]) -> int:
        if not tool_defs:
            return 0
        return len(json.dumps(list(tool_defs), ensure_ascii=False))

    def estimate_request(
        self,
        messages: Sequence[dict[str, Any]],
        tool_defs: Optional[Sequence[dict[str, Any]]] = None,
        *,
        safety_factor: float = 1.0,
    ) -> int:
        return self.count_messages(messages) + self.count_tools(tool_defs)


def user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def assistant(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


def build_conversation(turns: int, *, size: int = 100) -> list[dict[str, Any]]:
    """system + N 个 (user, assistant) 完整 turn + 当前 user, 每轮正文 size 字符."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": "SYS"}]
    for index in range(1, turns + 1):
        messages.append(user(f"q{index}" + "u" * size))
        messages.append(assistant(f"a{index}" + "a" * size))
    messages.append(user("current"))
    return messages


def make_compressor(
    *, budget: int, keep_recent_turns: int = 2, enabled: bool = True
) -> ContextCompressor:
    return ContextCompressor(
        counter=CharCounter(),
        token_budget=budget,
        keep_recent_turns=keep_recent_turns,
        enabled=enabled,
    )


# ------------------------------------------------------------------ turn 切分


class TestSplitTurns:
    def test_groups_by_user_boundary(self):
        messages = [
            user("q1"),
            assistant("a1"),
            user("q2"),
            assistant("a2"),
        ]

        turns = split_turns(messages)

        assert [len(turn) for turn in turns] == [2, 2]
        assert turns[0][0]["content"] == "q1"
        assert turns[1][0]["content"] == "q2"

    def test_tool_pair_stays_in_same_turn(self):
        messages = [
            user("q1"),
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "结果"},
            assistant("最终答案"),
        ]

        turns = split_turns(messages)

        assert len(turns) == 1
        assert [message["role"] for message in turns[0]] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]

    def test_consecutive_user_messages_are_separate_turns(self):
        turns = split_turns([user("q1"), user("q2")])
        assert len(turns) == 2
        assert all(len(turn) == 1 for turn in turns)

    def test_leading_non_user_fragment_is_skipped(self, caplog):
        messages = [
            assistant("孤儿回答"),
            {"role": "tool", "tool_call_id": "orphan", "content": "孤儿结果"},
            user("q1"),
            assistant("a1"),
        ]

        turns = split_turns(messages)

        assert len(turns) == 1
        assert turns[0][0]["content"] == "q1"
        assert any("前导非 user" in record.message for record in caplog.records)

    def test_empty(self):
        assert split_turns([]) == []


# ------------------------------------------------------------------ L0 短路


class TestUnderBudgetNoop:
    @pytest.mark.asyncio
    async def test_returns_same_object_and_outcome(self):
        messages = build_conversation(turns=3)
        compressor = make_compressor(budget=10_000)

        result = await compressor.prepare_request(messages)

        assert result is messages
        outcome = compressor.last_outcome
        assert outcome is not None
        assert outcome.changed is False
        assert outcome.still_over_budget is False
        assert outcome.dropped_turns == 0
        assert outcome.messages is messages

    @pytest.mark.asyncio
    async def test_disabled_is_noop_even_over_budget(self):
        messages = build_conversation(turns=3)
        compressor = make_compressor(budget=10, enabled=False)

        result = await compressor.prepare_request(messages)

        assert result is messages
        assert compressor.last_outcome is not None
        assert compressor.last_outcome.changed is False
        assert compressor.last_outcome.still_over_budget is False

    @pytest.mark.asyncio
    async def test_empty_messages(self):
        compressor = make_compressor(budget=10)

        result = await compressor.prepare_request([])

        assert result == []
        assert compressor.last_outcome is not None
        assert compressor.last_outcome.estimated_tokens == 0


# ------------------------------------------------------------------ L2 硬裁


class TestHardTrim:
    @pytest.mark.asyncio
    async def test_drops_oldest_turns_and_inserts_placeholder(self):
        # 3 个完整旧 turn + 当前 user; 预算 450 只放得下最近 1 个旧 turn
        messages = build_conversation(turns=3)
        compressor = make_compressor(budget=300, keep_recent_turns=2)

        result = await compressor.prepare_request(messages)

        assert result is not messages
        assert result[0] == {"role": "system", "content": "SYS"}
        placeholder = result[1]
        assert placeholder["role"] == "system"
        assert placeholder["content"] == HISTORY_OMITTED_TEMPLATE.format(count=2)
        # 最近一轮完整旧 turn 与当前提问保留
        contents = [message.get("content") for message in result]
        assert "q3" + "u" * 100 in contents
        assert "a3" + "a" * 100 in contents
        assert "q1" + "u" * 100 not in contents
        assert contents[-1] == "current"
        # outcome
        outcome = compressor.last_outcome
        assert outcome is not None
        assert outcome.changed is True
        assert outcome.dropped_turns == 2
        assert outcome.still_over_budget is False
        assert outcome.estimated_tokens <= 450

    @pytest.mark.asyncio
    async def test_input_messages_are_not_mutated(self):
        messages = build_conversation(turns=3)
        snapshot = json.dumps(messages, ensure_ascii=False)
        compressor = make_compressor(budget=450)

        await compressor.prepare_request(messages)

        assert json.dumps(messages, ensure_ascii=False) == snapshot

    @pytest.mark.asyncio
    async def test_falls_back_to_keep_one_when_needed(self):
        # keep_recent=3 的候选仍超预算, 自动降到 keep=1
        messages = build_conversation(turns=3)
        compressor = make_compressor(budget=450, keep_recent_turns=3)

        result = await compressor.prepare_request(messages)

        outcome = compressor.last_outcome
        assert outcome is not None
        assert outcome.changed is True
        assert outcome.dropped_turns == 2
        assert outcome.still_over_budget is False
        assert sum(1 for message in result if message["role"] == "user") == 2  # q3 + current

    @pytest.mark.asyncio
    async def test_still_over_budget_reports_and_keeps_best_effort(self):
        messages = build_conversation(turns=3)
        compressor = make_compressor(budget=50, keep_recent_turns=2)

        result = await compressor.prepare_request(messages)

        outcome = compressor.last_outcome
        assert outcome is not None
        assert outcome.changed is True
        assert outcome.still_over_budget is True
        assert outcome.dropped_turns == 2
        assert result[1]["content"].startswith("[历史省略]")

    @pytest.mark.asyncio
    async def test_no_compressible_turn_keeps_original(self, caplog):
        messages = [{"role": "system", "content": "SYS"}, user("q1"), assistant("a1"), user("cur")]
        compressor = make_compressor(budget=1, keep_recent_turns=2)

        result = await compressor.prepare_request(messages)

        assert result is messages
        outcome = compressor.last_outcome
        assert outcome is not None
        assert outcome.changed is False
        assert outcome.still_over_budget is True
        assert any("无可压缩" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_no_user_message_keeps_original(self):
        messages = [{"role": "system", "content": "SYS"}, assistant("孤儿")]
        compressor = make_compressor(budget=1)

        result = await compressor.prepare_request(messages)

        assert result is messages
        assert compressor.last_outcome is not None
        assert compressor.last_outcome.still_over_budget is True

    @pytest.mark.asyncio
    async def test_tool_pairing_survives_trim(self):
        messages = [
            {"role": "system", "content": "SYS"},
            # 将被裁掉的旧 turn(含工具配对)
            user("q1" + "u" * 100),
            {
                "role": "assistant",
                "content": "",
                "content_unused": None,
                "tool_calls": [
                    {"id": "old", "type": "function", "function": {"name": "read_file"}}
                ],
            },
            {"role": "tool", "tool_call_id": "old", "content": "旧结果" + "x" * 100},
            assistant("a1" + "a" * 100),
            # 保留的最近 turn(含工具配对)
            user("q2" + "u" * 100),
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "new", "type": "function", "function": {"name": "read_file"}}
                ],
            },
            {"role": "tool", "tool_call_id": "new", "content": "新结果" + "x" * 100},
            assistant("a2" + "a" * 100),
            user("current"),
        ]
        compressor = make_compressor(budget=450, keep_recent_turns=1)

        result = await compressor.prepare_request(messages)

        assert not any(message.get("tool_call_id") == "old" for message in result)
        kept_call_ids = [
            call["id"]
            for message in result
            if message.get("role") == "assistant"
            for call in (message.get("tool_calls") or [])
        ]
        kept_tool_ids = [
            message.get("tool_call_id") for message in result if message.get("role") == "tool"
        ]
        assert kept_call_ids == ["new"]
        assert kept_tool_ids == ["new"]
        # 占位消息之后的第一条必须是 user, 不能是悬空 tool
        assert result[2]["role"] == "user"

    @pytest.mark.asyncio
    async def test_tool_definitions_count_toward_budget(self):
        messages = build_conversation(turns=2)
        tools = [{"type": "function", "function": {"description": "x" * 500}}]
        compressor = make_compressor(budget=600, keep_recent_turns=1)

        result = await compressor.prepare_request(messages, tools)

        assert result is not messages
        assert result[1]["content"].startswith("[历史省略]")
        assert compressor.last_outcome is not None
        assert compressor.last_outcome.dropped_turns == 1


# ------------------------------------------------------------------ 构造校验


class TestConstruction:
    @pytest.mark.parametrize("budget", [0, -1, "100", None, True])
    def test_invalid_budget(self, budget):
        with pytest.raises(CompressionError):
            ContextCompressor(counter=CharCounter(), token_budget=budget)  # type: ignore[arg-type]

    @pytest.mark.parametrize("keep", [0, -1, "2", None, True])
    def test_invalid_keep_recent(self, keep):
        with pytest.raises(CompressionError):
            ContextCompressor(
                counter=CharCounter(),
                token_budget=100,
                keep_recent_turns=keep,  # type: ignore[arg-type]
            )

    def test_repr(self):
        text = repr(make_compressor(budget=123))
        assert "budget=123" in text
        assert "keep_recent_turns=2" in text
