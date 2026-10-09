"""meowmeowclaw/agent/audit.py 的单元测试(P4: HISTORY.md).

覆盖: Markdown 格式、UTC 时间戳、摘要/原文落盘、原文截断、大小轮转、并发追加不交错、
fail-soft(父目录不可创建不抛异常)、缺字段容错.

运行: pytest tests/agent/test_audit.py -v
"""

import asyncio
import logging
import re
from typing import Any

import pytest

from meowmeowclaw.agent.audit import HistoryAuditLog
from meowmeowclaw.memory.models import ms_to_iso

EVENT_MS = 1_760_000_000_000  # 2025-10-09T08:53:20.000Z


def make_event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "timestamp_ms": EVENT_MS,
        "session": "cli:session:abc",
        "storage_id": "storage123",
        "event": "summary",
        "result": "ok",
        "reason": "",
        "counter": "heuristic-cjk",
        "budget": 48000,
        "estimated_before": 61203,
        "estimated_after": 21780,
        "dropped_turns": 12,
        "summary": "用户偏好用中文; 未完成任务: 实现 P5",
        "summary_tokens": 812,
        "summary_model": "deepseek-flash",
        "elapsed_ms": 1800,
        "cached": False,
        "original_messages": [
            {"role": "user", "content": "帮我改个 bug"},
            {"role": "assistant", "content": "已定位到 loop.py"},
        ],
    }
    event.update(overrides)
    return event


@pytest.fixture
def audit(tmp_path) -> HistoryAuditLog:
    return HistoryAuditLog(tmp_path / "memory" / "HISTORY.md")


# --------------------------------------------------------------- 格式与内容


class TestFormatEvent:
    @pytest.mark.asyncio
    async def test_section_contains_expected_fields(self, audit):
        await audit.record(make_event())

        text = audit.path.read_text(encoding="utf-8")

        assert f"## {ms_to_iso(EVENT_MS)} | session=cli:session:abc | storage=storage123 | event=summary | result=ok" in text
        assert "- 触发: estimated_input_tokens=61203 > budget=48000 (counter=heuristic-cjk)" in text
        assert "- 替换: 最早 12 个 turn / 2 条消息" in text
        assert "- 摘要: model=deepseek-flash, summary_tokens≈812, elapsed=1.8s, cached=False" in text
        assert "- 压缩后: estimated_input_tokens=21780" in text
        assert "### 摘要" in text
        assert "用户偏好用中文; 未完成任务: 实现 P5" in text
        assert "sessions/storage123.jsonl" in text
        assert '"role": "user"' in text
        assert '"content": "帮我改个 bug"' in text
        assert text.endswith("\n")

    @pytest.mark.asyncio
    async def test_fallback_event_without_summary(self, audit):
        await audit.record(
            make_event(
                event="fallback_trim",
                result="fallback",
                reason="timeout",
                summary=None,
                summary_tokens=None,
                elapsed_ms=15000,
            )
        )

        text = audit.path.read_text(encoding="utf-8")

        assert "event=fallback_trim | result=fallback" in text
        assert "- 原因: timeout" in text
        assert "（无：本次为硬裁降级）" in text
        assert "elapsed=15.0s" in text

    @pytest.mark.asyncio
    async def test_tool_elision_bullet_is_rendered(self, audit):
        await audit.record(
            make_event(
                event="tool_elision",
                result="ok",
                summary=None,
                tool_elisions=2,
                original_messages=[{"role": "tool", "tool_call_id": "c1", "content": "原始结果"}],
            )
        )

        text = audit.path.read_text(encoding="utf-8")
        assert "- 当前轮工具结果占位: 2 条" in text

    @pytest.mark.asyncio
    async def test_missing_fields_are_tolerated(self, tmp_path):
        audit = HistoryAuditLog(tmp_path / "HISTORY.md")

        await audit.record({})  # 不应抛异常

        text = audit.path.read_text(encoding="utf-8")
        assert "event=- | result=-" in text
        assert "```json" in text

    @pytest.mark.asyncio
    async def test_original_is_truncated_by_chars(self, tmp_path):
        audit = HistoryAuditLog(tmp_path / "HISTORY.md", original_chars=200)
        messages = [
            {"role": "user", "content": "HEAD" + "A" * 500 + "MIDDLE" + "B" * 500 + "TAIL"}
        ]

        await audit.record(make_event(original_messages=messages))

        text = audit.path.read_text(encoding="utf-8")
        assert "审计原文截断" in text
        assert "MIDDLE" not in text
        assert "HEAD" in text
        assert "TAIL" in text


# --------------------------------------------------------------- 轮转与并发


class TestRotationAndConcurrency:
    @pytest.mark.asyncio
    async def test_rotation_keeps_single_generation(self, tmp_path):
        audit = HistoryAuditLog(tmp_path / "HISTORY.md", max_bytes=50)

        await audit.record(make_event(summary="第一条"))
        await audit.record(make_event(summary="第二条"))
        await audit.record(make_event(summary="第三条"))

        assert audit.path.is_file()
        assert audit.rotated_path.is_file()
        current = audit.path.read_text(encoding="utf-8")
        rotated = audit.rotated_path.read_text(encoding="utf-8")
        assert "第三条" in current
        assert "第二条" in rotated
        assert "第一条" not in rotated  # 单代覆盖: 更早的已滚出
        assert "第一条" not in current

    @pytest.mark.asyncio
    async def test_concurrent_appends_do_not_interleave(self, tmp_path):
        audit = HistoryAuditLog(tmp_path / "HISTORY.md")

        await asyncio.gather(
            *(audit.record(make_event(summary=f"并发摘要{i}")) for i in range(8))
        )

        text = audit.path.read_text(encoding="utf-8")
        assert len(re.findall(r"(?m)^## ", text)) == 8
        assert text.count("### 摘要") == 8
        assert text.count("### 原文") == 8
        for index in range(8):
            assert f"并发摘要{index}" in text

    @pytest.mark.asyncio
    async def test_write_failure_is_fail_soft(self, tmp_path, caplog):
        blocker = tmp_path / "blocker"
        blocker.write_text("我是文件, 不是目录", encoding="utf-8")
        audit = HistoryAuditLog(blocker / "HISTORY.md")

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.agent.audit"):
            await audit.record(make_event())  # 不能抛异常

        assert any("HISTORY.md 写入失败" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_creates_parent_directory(self, tmp_path):
        audit = HistoryAuditLog(tmp_path / "deep" / "nested" / "HISTORY.md")

        await audit.record(make_event())

        assert audit.path.is_file()

    def test_invalid_constructor_args(self, tmp_path):
        for kwargs in (
            {"max_bytes": 0},
            {"max_bytes": "x"},
            {"original_chars": -1},
            {"original_chars": True},
            {"lock_timeout": 0},
            {"lock_timeout": "x"},
        ):
            with pytest.raises(ValueError):
                HistoryAuditLog(tmp_path / "HISTORY.md", **kwargs)  # type: ignore[arg-type]

    def test_repr_contains_path(self, tmp_path):
        audit = HistoryAuditLog(tmp_path / "HISTORY.md")

        assert "HISTORY.md" in repr(audit)
        assert "max_bytes" in repr(audit)
