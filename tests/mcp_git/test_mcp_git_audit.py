"""GM6 工具调用审计单测(``meowmeowclaw_mcp_git/audit.py``, §8)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from meowmeowclaw_mcp_git.app import GitMcpApp
from meowmeowclaw_mcp_git.audit import AuditSink, digest_arguments
from meowmeowclaw_mcp_git.config import ServerConfig
from meowmeowclaw_mcp_git.errors import ConfigError

AUDIT_KEYS = {
    "ts",
    "tool",
    "repo",
    "args_digest",
    "status",
    "error_code",
    "exit_code",
    "elapsed_ms",
    "truncated",
}


def make_app(repo: Path, audit_path: Path, **overrides) -> GitMcpApp:
    return GitMcpApp(ServerConfig(roots=(str(repo),), audit_log=str(audit_path), **overrides))


def read_events(audit_path: Path) -> list[dict]:
    return [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line]


async def call(app: GitMcpApp, tool: str, **arguments) -> dict:
    return json.loads(await app.call_tool(tool, arguments))


class TestAuditSink:
    def test_creates_file_with_private_mode(self, tmp_path: Path):
        sink = AuditSink(tmp_path / "nested" / "audit.jsonl")

        assert sink.path.exists()
        if os.name != "nt":
            assert sink.path.stat().st_mode & 0o777 == 0o600

    def test_appends_jsonl_events(self, tmp_path: Path):
        sink = AuditSink(tmp_path / "audit.jsonl")

        sink.record({"tool": "git_status"})
        sink.record({"tool": "git_log"})

        assert [item["tool"] for item in read_events(sink.path)] == ["git_status", "git_log"]

    def test_bad_parent_path_raises_config_error(self, tmp_path: Path):
        blocker = tmp_path / "blocker"
        blocker.write_text("file\n", encoding="utf-8")

        with pytest.raises(ConfigError):
            AuditSink(blocker / "audit.jsonl")

    def test_empty_path_raises(self):
        with pytest.raises(ConfigError):
            AuditSink("")

    def test_digest_is_stable_and_value_insensitive_shape(self):
        first = digest_arguments({"b": 2, "a": 1})
        second = digest_arguments({"a": 1, "b": 2})

        assert first == second
        assert len(first) == 16
        assert digest_arguments({"a": 1}) != first


class TestAuditIntegration:
    @pytest.mark.asyncio
    async def test_disabled_by_default(self, repo: Path):
        app = GitMcpApp(ServerConfig(roots=(str(repo),)))

        assert app.audit is None
        payload = await call(app, "git_status")

        assert payload["ok"] is True

    @pytest.mark.asyncio
    async def test_success_event(self, repo: Path, tmp_path: Path):
        audit_path = tmp_path / "audit.jsonl"
        app = make_app(repo, audit_path)

        await call(app, "git_status", short=True)

        events = read_events(audit_path)
        assert len(events) == 1
        assert set(events[0]) == AUDIT_KEYS
        assert events[0]["tool"] == "git_status"
        assert events[0]["repo"] == repo.name
        assert events[0]["status"] == "ok"
        assert events[0]["error_code"] is None
        assert events[0]["exit_code"] == 0
        assert events[0]["truncated"] is False
        assert isinstance(events[0]["elapsed_ms"], int) and events[0]["elapsed_ms"] >= 0
        assert len(events[0]["args_digest"]) == 16

    @pytest.mark.asyncio
    async def test_error_event_carries_code(self, repo: Path, tmp_path: Path):
        audit_path = tmp_path / "audit.jsonl"
        app = make_app(repo, audit_path)

        await call(app, "git_status", repo="ghost")

        event = read_events(audit_path)[0]
        assert event["status"] == "error"
        assert event["error_code"] == "repo_not_found"
        assert event["repo"] == "ghost"
        assert event["exit_code"] is None

    @pytest.mark.asyncio
    async def test_validation_error_is_audited(self, repo: Path, tmp_path: Path):
        audit_path = tmp_path / "audit.jsonl"
        app = make_app(repo, audit_path)

        await call(app, "git_status", repo=123)

        event = read_events(audit_path)[0]
        assert event["status"] == "error"
        assert event["error_code"] == "invalid_argument"

    @pytest.mark.asyncio
    async def test_truncated_flag_propagates(self, history_repo: Path, tmp_path: Path):
        audit_path = tmp_path / "audit.jsonl"
        app = make_app(history_repo, audit_path)

        payload = await call(app, "git_grep", pattern="hello", max_lines=1)

        assert payload["data"]["truncated"] is True
        assert read_events(audit_path)[0]["truncated"] is True

    @pytest.mark.asyncio
    async def test_arguments_are_hashed_not_stored(self, write_repo: Path, tmp_path: Path, git):
        audit_path = tmp_path / "audit.jsonl"
        (write_repo / "app.py").write_text("changed\n", encoding="utf-8")
        git(write_repo, "add", "app.py")
        app = make_app(write_repo, audit_path, read_only=False)
        secret = "feat: super-secret-message"

        payload = await call(app, "git_commit", message=secret)

        assert payload["ok"] is True
        raw = audit_path.read_text(encoding="utf-8")
        assert secret not in raw
        assert "app.py" not in raw  # 参数值(路径)同样不落盘
        event = read_events(audit_path)[0]
        assert event["tool"] == "git_commit"
        assert event["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_prompts_and_resources_are_not_audited(self, history_repo: Path, tmp_path: Path):
        audit_path = tmp_path / "audit.jsonl"
        app = make_app(history_repo, audit_path)

        await app.build_prompt("review_staged_changes", {})
        await app.read_resource(f"git://{history_repo.name}/status")

        assert read_events(audit_path) == []

    @pytest.mark.asyncio
    async def test_write_failure_is_fail_soft(self, repo: Path, tmp_path: Path, monkeypatch):
        audit_path = tmp_path / "audit.jsonl"
        app = make_app(repo, audit_path)

        def broken(event):  # type: ignore[no-untyped-def]
            raise RuntimeError("disk full")

        monkeypatch.setattr(app.audit, "record", broken)

        payload = await call(app, "git_status")

        assert payload["ok"] is True  # 审计失败不影响工具返回

    @pytest.mark.asyncio
    async def test_default_repo_alias_when_omitted(self, repo: Path, tmp_path: Path):
        audit_path = tmp_path / "audit.jsonl"
        app = make_app(repo, audit_path)

        await call(app, "git_status")

        assert read_events(audit_path)[0]["repo"] == repo.name

    def test_startup_validation_rejects_unwritable_path(self, repo: Path, tmp_path: Path):
        blocker = tmp_path / "blocker"
        blocker.write_text("file\n", encoding="utf-8")

        with pytest.raises(ConfigError):
            make_app(repo, blocker / "nested" / "audit.jsonl")
