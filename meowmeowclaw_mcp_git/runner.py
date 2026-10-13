"""Git 执行层(``docs/GIT_MCP_DESIGN.md`` §5.1/§5.2/§5.4)。

- 只走参数列表, **禁止 shell**; 命令固定为
  ``git --no-pager -C <repo_path> <subcommand> [args...]``;
- 统一环境清洗: 防分页/防弹编辑器/防凭证卡死/固定 ``LC_ALL=C``;
- 硬超时: 超时后杀进程组(POSIX ``killpg`` / Windows ``taskkill`` 兜底);
- 输出上限: 超出 ``max_output_bytes`` 截断并标记 ``truncated``;
- 非零退出**默认不抛异常**, 由工具层决定映射为哪个错误码(``check=True`` 可切换)。

本模块不 import MCP SDK, 可独立单测。
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from .errors import ERROR_COMMAND_FAILED, ERROR_COMMAND_TIMEOUT, GitMcpError, InvalidArgumentError

GIT_BINARY = "git"
_TEXT_ENCODING = "utf-8"
_STDERR_SUMMARY_CHARS = 400


def build_git_env(environ: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """构造 git 子进程环境(§5.2): 只覆盖必要变量, 其余(含 ``HOME``/credential helper)继承 Host."""
    env = dict(os.environ if environ is None else environ)
    env.update(
        {
            # §5.2: 防分页 / 防编辑器 / 防凭证弹窗 / 固定 locale
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_EDITOR": "true",
            "EDITOR": "true",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    return env


@dataclass(frozen=True)
class GitResult:
    """一次 git 调用的完整结果(字节级 stdout/stderr, 由调用方决定解码)."""

    command: tuple[str, ...]
    cwd: str
    exit_code: int
    stdout: bytes = b""
    stderr: bytes = b""
    elapsed_ms: int = 0
    truncated: bool = False
    timed_out: bool = False
    stdout_total_bytes: int = 0
    stderr_total_bytes: int = 0

    @property
    def ok(self) -> bool:
        """exit=0 且未超时."""
        return self.exit_code == 0 and not self.timed_out

    def stdout_text(self) -> str:
        """按 UTF-8 解码(非法字节替换, 避免 MCP 文本流崩溃)."""
        return self.stdout.decode(_TEXT_ENCODING, errors="replace")

    def stderr_text(self) -> str:
        return self.stderr.decode(_TEXT_ENCODING, errors="replace")

    def meta(self) -> dict[str, Any]:
        """§4.2 meta 片段(不含 repo, 由工具层补充)."""
        return {
            "cwd": self.cwd,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms,
        }


class GitCommandError(GitMcpError):
    """git 非零退出(``command_failed``), 保留退出码/命令/stderr 摘要."""

    code = ERROR_COMMAND_FAILED

    def __init__(
        self,
        result: GitResult,
        *,
        message: Optional[str] = None,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.result = result
        merged = result.meta()
        merged.update(meta or {})
        summary = result.stderr_text().strip().splitlines()
        detail = f": {summary[0][:_STDERR_SUMMARY_CHARS]}" if summary else ""
        super().__init__(
            message or f"git 命令失败(exit={result.exit_code}){detail}",
            meta=merged,
        )


class CommandTimeoutError(GitMcpError):
    """git 超过硬超时被终止(``command_timeout``)."""

    code = ERROR_COMMAND_TIMEOUT

    def __init__(
        self,
        result: GitResult,
        *,
        timeout_seconds: float,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.result = result
        merged = result.meta()
        merged.update(meta or {})
        super().__init__(
            f"git 命令超时(>{timeout_seconds:g}s, 已终止进程组): {' '.join(result.command)}",
            meta=merged,
        )


class GitRunner:
    """安全的 git CLI 调用器(单实例可复用; 线程内同步执行)."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_output_bytes: int = 131_072,
        git_binary: str = GIT_BINARY,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise InvalidArgumentError("timeout_seconds 必须 > 0")
        if max_output_bytes <= 0:
            raise InvalidArgumentError("max_output_bytes 必须 > 0")
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_bytes = int(max_output_bytes)
        self.git_binary = git_binary
        self._base_env = build_git_env(environ)

    @classmethod
    def from_config(cls, config: Any) -> "GitRunner":
        """从 :class:`~meowmeowclaw_mcp_git.config.ServerConfig` 构造."""
        return cls(timeout_seconds=config.timeout_seconds, max_output_bytes=config.max_output_bytes)

    def build_command(self, args: Sequence[str], cwd: os.PathLike[str] | str) -> list[str]:
        """拼装最终命令行: ``git --no-pager -C <cwd> <args...>``(参数列表, 无 shell)."""
        if not args:
            raise InvalidArgumentError("git 参数不能为空")
        return [
            self.git_binary,
            "--no-pager",
            "-C",
            os.fspath(cwd),
            *[os.fspath(arg) for arg in args],
        ]

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: os.PathLike[str] | str,
        timeout: Optional[float] = None,
        max_output_bytes: Optional[int] = None,
        check: bool = False,
    ) -> GitResult:
        """执行一次 git 命令并返回 :class:`GitResult`."""
        effective_timeout = self.timeout_seconds if timeout is None else float(timeout)
        if effective_timeout <= 0:
            raise InvalidArgumentError("timeout 必须 > 0")
        cap = self.max_output_bytes if max_output_bytes is None else int(max_output_bytes)
        if cap <= 0:
            raise InvalidArgumentError("max_output_bytes 必须 > 0")

        command = self.build_command(args, cwd)
        started = time.monotonic()
        process = subprocess.Popen(  # noqa: S603 - 参数列表 + 无 shell(§5.1)
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(self._base_env),
            start_new_session=os.name == "posix",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,  # type: ignore[attr-defined]
        )
        timed_out = False
        try:
            raw_stdout, raw_stderr = process.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_group(process)
            raw_stdout, raw_stderr = process.communicate()
        elapsed_ms = int((time.monotonic() - started) * 1000)

        stdout_total, stderr_total = len(raw_stdout), len(raw_stderr)
        truncated = stdout_total > cap or stderr_total > cap
        result = GitResult(
            command=tuple(command),
            cwd=os.fspath(cwd),
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=raw_stdout[:cap],
            stderr=raw_stderr[:cap],
            elapsed_ms=elapsed_ms,
            truncated=truncated,
            timed_out=timed_out,
            stdout_total_bytes=stdout_total,
            stderr_total_bytes=stderr_total,
        )
        if check:
            if result.timed_out:
                raise CommandTimeoutError(result, timeout_seconds=effective_timeout)
            if result.exit_code != 0:
                raise GitCommandError(result)
        return result

    def run_text(
        self,
        args: Sequence[str],
        *,
        cwd: os.PathLike[str] | str,
        check: bool = True,
        **kwargs: Any,
    ) -> str:
        """便捷方法: 执行并返回 stdout 文本(默认非零即抛)."""
        return self.run(args, cwd=cwd, check=check, **kwargs).stdout_text()


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """超时清理: 优先杀进程组, 避免 git 的 hooks/子进程残留(§5.4)."""
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    elif os.name == "nt":  # pragma: no cover - Windows
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=5,
                check=False,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.kill()
    except OSError:
        pass
