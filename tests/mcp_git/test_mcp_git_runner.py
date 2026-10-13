"""GM1 GitRunner 单测(``meowmeowclaw_mcp_git/runner.py``, 对照 §5.1/§5.2/§5.4)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from meowmeowclaw_mcp_git.errors import InvalidArgumentError, OutputTruncatedError
from meowmeowclaw_mcp_git.runner import (
    CommandTimeoutError,
    GitCommandError,
    GitRunner,
    build_git_env,
)


class TestBuildCommand:
    def test_shape_is_argv_list_without_shell(self, repo: Path):
        runner = GitRunner()
        command = runner.build_command(["status", "--porcelain=v2"], repo)

        assert command == ["git", "--no-pager", "-C", str(repo), "status", "--porcelain=v2"]

    def test_empty_args_rejected(self, repo: Path):
        with pytest.raises(InvalidArgumentError):
            GitRunner().build_command([], repo)


class TestRun:
    def test_success_in_repo(self, repo: Path):
        result = GitRunner().run(["rev-parse", "--is-inside-work-tree"], cwd=repo)

        assert result.ok is True
        assert result.exit_code == 0
        assert result.stdout_text().strip() == "true"
        assert result.stderr_text() == ""
        assert result.command[:2] == ("git", "--no-pager")
        assert result.cwd == str(repo)

    def test_failure_captures_stderr_and_exit_code(self, repo: Path):
        result = GitRunner().run(["cat-file", "-t", "does-not-exist"], cwd=repo)

        assert result.ok is False
        assert result.exit_code != 0
        assert "does-not-exist" in result.stderr_text()
        assert result.stdout_text() == ""
        assert result.meta()["exit_code"] == result.exit_code

    def test_check_true_raises_structured_error(self, repo: Path):
        with pytest.raises(GitCommandError) as excinfo:
            GitRunner().run(["rev-parse", "does-not-exist"], cwd=repo, check=True)

        assert excinfo.value.code == "command_failed"
        assert excinfo.value.meta["exit_code"] != 0
        assert "git" in str(excinfo.value)

    def test_run_text_returns_stdout(self, repo: Path):
        assert GitRunner().run_text(["symbolic-ref", "--short", "HEAD"], cwd=repo).strip() == "main"

    def test_git_missing_raises_oserror(self, repo: Path):
        runner = GitRunner(git_binary="definitely-not-a-git-binary")

        with pytest.raises(OSError):
            runner.run(["status"], cwd=repo)

    def test_invalid_timeout_and_limit(self, repo: Path):
        runner = GitRunner()

        with pytest.raises(InvalidArgumentError):
            runner.run(["status"], cwd=repo, timeout=0)
        with pytest.raises(InvalidArgumentError):
            runner.run(["status"], cwd=repo, max_output_bytes=0)
        with pytest.raises(InvalidArgumentError):
            GitRunner(timeout_seconds=0)
        with pytest.raises(InvalidArgumentError):
            GitRunner(max_output_bytes=-1)


class TestOutputBudget:
    def test_output_below_limit_not_truncated(self, repo: Path):
        result = GitRunner(max_output_bytes=4096).run(["rev-parse", "--show-toplevel"], cwd=repo)

        assert result.truncated is False
        assert result.stdout_total_bytes <= 4096

    def test_output_over_limit_is_truncated_and_marked(self, repo: Path):
        result = GitRunner(max_output_bytes=8).run(["rev-parse", "--show-toplevel"], cwd=repo)

        assert result.truncated is True
        assert len(result.stdout) == 8
        assert result.stdout_total_bytes > 8
        assert result.meta()["truncated"] is True

    def test_per_call_limit_overrides_default(self, repo: Path):
        result = GitRunner(max_output_bytes=4096).run(
            ["rev-parse", "--show-toplevel"], cwd=repo, max_output_bytes=4
        )

        assert result.truncated is True
        assert len(result.stdout) == 4


@pytest.mark.skipif(os.name == "nt", reason="POSIX 进程组语义")
class TestTimeout:
    def fake_git(self, tmp_path: Path, body: str) -> str:
        script = tmp_path / "fake-git"
        script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        script.chmod(0o755)
        return str(script)

    def test_timeout_kills_process_group(self, tmp_path: Path):
        runner = GitRunner(git_binary=self.fake_git(tmp_path, "sleep 30"), timeout_seconds=0.3)
        started = time.monotonic()

        result = runner.run(["status"], cwd=tmp_path)

        assert result.timed_out is True
        assert result.exit_code != 0
        assert time.monotonic() - started < 10  # 没有等到 sleep 30 结束

    def test_timeout_with_check_raises_command_timeout(self, tmp_path: Path):
        runner = GitRunner(git_binary=self.fake_git(tmp_path, "sleep 30"), timeout_seconds=0.3)

        with pytest.raises(CommandTimeoutError) as excinfo:
            runner.run(["status"], cwd=tmp_path, check=True, timeout=0.3)

        assert excinfo.value.code == "command_timeout"
        assert excinfo.value.meta["exit_code"] != 0


class TestEnvironment:
    def test_pager_editor_prompt_locale_are_forced(self):
        env = build_git_env({"HOME": "/home/tester", "PATH": "/usr/bin"})

        assert env["GIT_PAGER"] == "cat"
        assert env["PAGER"] == "cat"
        assert env["GIT_EDITOR"] == "true"
        assert env["EDITOR"] == "true"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["LC_ALL"] == "C"

    def test_host_env_is_inherited(self):
        env = build_git_env({"HOME": "/home/tester", "MY_TOKEN": "secret"})

        assert env["HOME"] == "/home/tester"
        assert env["MY_TOKEN"] == "secret"
        assert env is not None

    def test_child_process_sees_forced_env(self, repo: Path, monkeypatch):
        monkeypatch.setenv("MY_MARKER", "marker-value")
        script = repo / "env-git"
        script.write_text(
            "#!/bin/sh\nenv | grep -E '^(GIT_PAGER|LC_ALL|MY_MARKER)=' | sort\n",
            encoding="utf-8",
        )
        script.chmod(0o755)

        result = GitRunner(git_binary=str(script)).run(["status"], cwd=repo)

        assert "GIT_PAGER=cat" in result.stdout_text()
        assert "LC_ALL=C" in result.stdout_text()
        assert "MY_MARKER=marker-value" in result.stdout_text()

    def test_stdout_stderr_are_bytes(self, repo: Path):
        result = GitRunner().run(["rev-parse", "--show-toplevel"], cwd=repo)

        assert isinstance(result.stdout, bytes)
        assert isinstance(result.stderr, bytes)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell 脚本")
class TestStreamingTruncation:
    """GM2: 边读边截断 + 早停(大输出不读进内存, T7/§5.4)."""

    def fake_git(self, tmp_path: Path, body: str) -> str:
        script = tmp_path / "fake-git"
        script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        script.chmod(0o755)
        return str(script)

    def test_large_output_is_capped_and_stops_early(self, tmp_path: Path):
        big = self.fake_git(
            tmp_path,
            "i=0\nwhile [ $i -lt 500000 ]; do printf 'x'; i=$((i+1)); done",
        )
        runner = GitRunner(git_binary=big, max_output_bytes=1000)
        started = time.monotonic()

        result = runner.run(["log"], cwd=tmp_path)

        assert result.truncated is True
        assert len(result.stdout) == 1000  # 只保留前 1000 字节
        assert result.stdout_total_bytes > 1000  # 完整尺寸仍被记录
        assert result.timed_out is False
        assert time.monotonic() - started < 30  # 没有等 500KB 全部写完

    def test_check_true_raises_output_truncated(self, tmp_path: Path):
        big = self.fake_git(
            tmp_path,
            "i=0\nwhile [ $i -lt 500000 ]; do printf 'x'; i=$((i+1)); done",
        )
        runner = GitRunner(git_binary=big, max_output_bytes=500)

        with pytest.raises(OutputTruncatedError) as excinfo:
            runner.run(["log"], cwd=tmp_path, check=True)

        assert excinfo.value.code == "output_truncated"

    def test_small_output_is_not_truncated(self, tmp_path: Path):
        small = self.fake_git(tmp_path, "printf 'hello'")

        result = GitRunner(git_binary=small, max_output_bytes=1000).run(["log"], cwd=tmp_path)

        assert result.truncated is False
        assert result.stdout == b"hello"
        assert result.stdout_total_bytes == 5
