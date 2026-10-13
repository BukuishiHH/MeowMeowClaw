"""子代理领域模型单元测试(SA1, ``meowmeowclaw/subagent/models.py``).

对照 docs/SUBAGENT_DESIGN.md §3.2/§4.5/§5/§8.1:
请求/上下文校验、结果状态映射与 JSON 截断、预算计数、统计快照。
"""

import json
from types import SimpleNamespace

import pytest

from meowmeowclaw.subagent import (
    ERROR_BUDGET_EXCEEDED,
    ERROR_CHILDREN_EXCEEDED,
    ERROR_CONTEXT_OVERFLOW,
    ERROR_INTERNAL,
    ERROR_MAX_ITERATIONS,
    ERROR_PROVIDER,
    STATUS_CANCELLED,
    STATUS_ERROR,
    STATUS_LIMIT,
    STATUS_OK,
    STATUS_TIMEOUT,
    SpawnContext,
    SubagentBudget,
    SubagentRequest,
    SubagentResult,
    SubagentStats,
)


class TestSubagentRequest:
    def test_task_is_stripped_and_digest_is_stable(self):
        first = SubagentRequest(task="  完成任务  ", role="  researcher ", context="  背景  ")
        second = SubagentRequest(task="完成任务", role="researcher", context="背景")

        assert first.task == "完成任务"
        assert first.role == "researcher"
        assert first.context == "背景"
        assert first.task_digest == second.task_digest
        assert len(first.task_digest) == 16

    @pytest.mark.parametrize("task", ["", "   ", None, 123])
    def test_invalid_task_rejected(self, task):
        with pytest.raises(ValueError):
            SubagentRequest(task=task)  # type: ignore[arg-type]

    @pytest.mark.parametrize("field", ["role", "context"])
    def test_non_string_optional_fields_rejected(self, field):
        with pytest.raises(ValueError):
            SubagentRequest(task="t", **{field: 123})  # type: ignore[arg-type]


class TestSubagentResult:
    def test_to_dict_omits_optional_error_code(self):
        result = SubagentResult(
            status=STATUS_OK,
            summary="结论",
            finish_reason="stop",
            iterations=2,
            elapsed_ms=12,
            subagent_id="sa-1",
            depth=1,
            model="m",
        )

        data = result.to_dict()

        assert data["status"] == STATUS_OK
        assert "error_code" not in data
        assert data["truncated"] is False

    def test_error_result_keeps_error_code(self):
        result = SubagentResult(
            status=STATUS_ERROR,
            summary="失败",
            finish_reason="error",
            iterations=1,
            elapsed_ms=3,
            subagent_id="sa-2",
            error_code=ERROR_PROVIDER,
        )

        assert result.to_dict()["error_code"] == ERROR_PROVIDER

    @pytest.mark.parametrize("status", ["unknown", "", None])
    def test_invalid_status_rejected(self, status):
        with pytest.raises(ValueError):
            SubagentResult(
                status=status,  # type: ignore[arg-type]
                summary="s",
                finish_reason="stop",
                iterations=0,
                elapsed_ms=0,
                subagent_id="sa-1",
            )

    def test_to_json_round_trip(self):
        result = SubagentResult(
            status=STATUS_OK,
            summary="好了",
            finish_reason="stop",
            iterations=1,
            elapsed_ms=10,
            subagent_id="sa-1",
            depth=2,
            model="m",
        )

        payload = json.loads(result.to_json())

        assert payload == result.to_dict()

    def test_to_json_truncates_summary_before_limit(self):
        result = SubagentResult(
            status=STATUS_OK,
            summary="甲" * 500,
            finish_reason="stop",
            iterations=1,
            elapsed_ms=10,
            subagent_id="sa-1",
        )

        text = result.to_json(max_chars=120)
        payload = json.loads(text)

        assert len(text) <= 120
        assert payload["truncated"] is True
        assert len(payload["summary"]) < 500
        assert payload["status"] == STATUS_OK

    def test_to_json_without_limit_keeps_full_summary(self):
        result = SubagentResult(
            status=STATUS_OK,
            summary="甲" * 500,
            finish_reason="stop",
            iterations=1,
            elapsed_ms=10,
            subagent_id="sa-1",
        )

        payload = json.loads(result.to_json())

        assert payload["summary"] == "甲" * 500
        assert payload["truncated"] is False

    def test_to_json_extremely_small_limit_still_valid_json(self):
        result = SubagentResult(
            status=STATUS_ERROR,
            summary="非常长的失败说明" * 50,
            finish_reason="error",
            iterations=1,
            elapsed_ms=10,
            subagent_id="sa-1",
            error_code=ERROR_INTERNAL,
        )

        text = result.to_json(max_chars=30)
        payload = json.loads(text)

        assert payload["status"] == STATUS_ERROR
        assert payload["truncated"] is True

    def test_from_turn_ok(self):
        turn = SimpleNamespace(completed=True, finish_reason="stop", answer="结果", iterations=3)

        result = SubagentResult.from_turn(
            turn, subagent_id="sa-1", depth=1, model="m", elapsed_ms=99
        )

        assert result.status == STATUS_OK
        assert result.summary == "结果"
        assert result.error_code is None

    @pytest.mark.parametrize(
        ("finish_reason", "completed", "expected_status", "expected_code"),
        [
            ("max_iterations", False, STATUS_LIMIT, ERROR_MAX_ITERATIONS),
            ("context_overflow", False, STATUS_ERROR, ERROR_CONTEXT_OVERFLOW),
            ("error", False, STATUS_ERROR, ERROR_PROVIDER),
            ("weird", False, STATUS_ERROR, ERROR_INTERNAL),
        ],
    )
    def test_from_turn_mapping(self, finish_reason, completed, expected_status, expected_code):
        turn = SimpleNamespace(
            completed=completed, finish_reason=finish_reason, answer="x", iterations=1
        )

        result = SubagentResult.from_turn(
            turn, subagent_id="sa-1", depth=0, model="m", elapsed_ms=1
        )

        assert result.status == expected_status
        assert result.error_code == expected_code


class TestSubagentBudget:
    @pytest.mark.asyncio
    async def test_children_limit_is_per_parent(self):
        budget = SubagentBudget(max_children_per_turn=2, max_total_per_turn=10)

        assert await budget.reserve_child("root") is None
        assert await budget.reserve_child("root") is None
        assert await budget.reserve_child("root") == ERROR_CHILDREN_EXCEEDED
        # 另一个父代理有自己的直接子代理名额
        assert await budget.reserve_child("child") is None
        assert budget.snapshot()["per_parent"] == {"root": 2, "child": 1}

    @pytest.mark.asyncio
    async def test_total_limit_wins(self):
        budget = SubagentBudget(max_children_per_turn=5, max_total_per_turn=2)

        assert await budget.reserve_child("a") is None
        assert await budget.reserve_child("b") is None
        assert await budget.reserve_child("c") == ERROR_BUDGET_EXCEEDED
        assert budget.total_spawned == 2

    @pytest.mark.parametrize(
        ("children", "total"), [(0, 1), (1, 0), (-1, 1), (1, -1)]
    )
    def test_invalid_limits_rejected(self, children, total):
        with pytest.raises(ValueError):
            SubagentBudget(max_children_per_turn=children, max_total_per_turn=total)

    @pytest.mark.asyncio
    async def test_empty_parent_id_rejected(self):
        budget = SubagentBudget()

        assert await budget.reserve_child("") == ERROR_INTERNAL
        assert budget.snapshot()["total_spawned"] == 0


class TestSpawnContext:
    def test_valid_context(self):
        budget = SubagentBudget()
        ctx = SpawnContext(
            root_id="root", parent_id="p-1", depth=1, budget=budget, tool_names=("read_file",)
        )

        assert ctx.depth == 1
        assert ctx.tool_names == ("read_file",)

    @pytest.mark.parametrize(
        ("root", "parent", "depth"),
        [("", "p", 0), ("r", "", 0), ("r", "p", -1)],
    )
    def test_invalid_context(self, root, parent, depth):
        with pytest.raises(ValueError):
            SpawnContext(
                root_id=root,
                parent_id=parent,
                depth=depth,
                budget=SubagentBudget(),
            )


class TestSubagentStats:
    def test_spawn_and_finish_counts(self):
        stats = SubagentStats(enabled=True)

        stats.record_spawn(depth=2)
        stats.record_spawn(depth=1)
        assert stats.active == 2
        assert stats.max_depth_seen == 2

        stats.record_finish(STATUS_OK)
        stats.record_finish(STATUS_TIMEOUT)
        stats.record_finish(STATUS_CANCELLED)
        stats.record_finish(STATUS_LIMIT)
        stats.record_finish(STATUS_ERROR)

        snapshot = stats.snapshot()
        assert snapshot["spawned_total"] == 2
        assert snapshot["active"] == 0
        assert snapshot["ok"] == 1
        assert snapshot["timeout"] == 1
        assert snapshot["cancelled"] == 1
        assert snapshot["limited"] == 1
        assert snapshot["error"] == 1

    def test_observability_fields_and_reset(self):
        stats = SubagentStats(enabled=True)
        stats.record_conflict(write=True)
        stats.record_conflict()
        stats.record_exec_denied()
        stats.record_finish(STATUS_OK, lock_wait_ms=12.5)
        stats.record_finish(STATUS_OK, lock_wait_ms=7.5)

        snapshot = stats.snapshot()
        assert snapshot["conflicts_detected"] == 2
        assert snapshot["write_conflicts"] == 1
        assert snapshot["exec_denied"] == 1
        assert snapshot["lock_wait_total_ms"] == 20.0
        assert snapshot["lock_wait_max_ms"] == 12.5

        stats.reset()
        snapshot = stats.snapshot()
        assert snapshot["enabled"] is True
        assert snapshot["spawned_total"] == 0
        assert snapshot["lock_wait_total_ms"] == 0.0
