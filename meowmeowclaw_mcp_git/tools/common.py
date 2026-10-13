"""只读工具共享件: git 调用、错误映射、meta、路径参数与自由参数防护。

- 所有工具的参数都以列表传给 git, 但自由文本(ref/pattern)仍要防"看起来像选项"
  (例如 ``--output=/etc/x``), 因此 ref 类参数统一走 :func:`require_ref`, pattern
  以 ``-e<pattern>``/``-S<pattern>`` 粘连传递, tag pattern 拒绝 ``-`` 开头;
- ``ensure_success`` 是错误码的唯一收口: 超时 → ``command_timeout``、截断 →
  ``output_truncated``、非零退出 → ``command_failed``; 允许截断的文本类工具
  显式传 ``allow_truncated=True``。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Optional, Sequence

from ..budget import DEFAULT_TEXT_BYTES
from ..errors import GitMcpError, InvalidArgumentError, OutputTruncatedError
from ..registry import RepoEntry
from ..runner import CommandTimeoutError, GitCommandError, GitResult
from .base import ToolContext


def repo_param(description: str = "仓库 alias; 单仓库模式可省略") -> dict[str, Any]:
    return {"type": "string", "description": description, "minLength": 1}


def ref_param(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description, "minLength": 1}


def paths_param(description: str = "仓库内相对路径过滤(可多个)") -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string", "minLength": 1}, "description": description}


def max_bytes_param(description: str, default: int) -> dict[str, Any]:
    return {
        "type": "integer",
        "minimum": 1,
        "default": default,
        "description": f"{description}(同时受服务器 --max-output-bytes 约束)",
    }


def max_lines_param(description: str, default: int, *, maximum: int = 2000) -> dict[str, Any]:
    return {"type": "integer", "minimum": 1, "maximum": maximum, "default": default, "description": description}


async def run_git(
    ctx: ToolContext,
    entry: RepoEntry,
    args: Sequence[str],
    *,
    max_output_bytes: Optional[int] = None,
) -> GitResult:
    """在事件循环外执行 git(读命令可能达秒级, 不阻塞 MCP 心跳/取消)."""
    return await asyncio.to_thread(ctx.runner.run, args, cwd=entry.path, max_output_bytes=max_output_bytes)


def resolve_entry(ctx: ToolContext, repo: Any) -> RepoEntry:
    """解析 repo alias, 失败时把 alias 带进错误 meta."""
    try:
        return ctx.registry.resolve(repo)
    except GitMcpError as exc:
        if repo is not None:
            exc.meta.setdefault("repo", repo)
        raise


def meta_for(entry: RepoEntry, result: GitResult) -> dict[str, Any]:
    return {"repo": entry.alias, **result.meta()}


def ensure_success(
    ctx: ToolContext,
    result: GitResult,
    *,
    entry: RepoEntry,
    what: str = "git 命令",
    allow_truncated: bool = False,
) -> None:
    """把 git 结果映射为稳定错误码(§4.2)."""
    if result.timed_out:
        raise CommandTimeoutError(
            result,
            timeout_seconds=ctx.config.timeout_seconds,
            meta={"repo": entry.alias},
        )
    if result.truncated:
        if allow_truncated:
            return
        raise OutputTruncatedError(
            f"{what} 输出超过上限(已截断, 无法保证结果完整); 请缩小查询范围或提高 --max-output-bytes",
            meta={"repo": entry.alias, **result.meta()},
        )
    if result.exit_code != 0:
        raise GitCommandError(result, meta={"repo": entry.alias})


def rel_path(ctx: ToolContext, entry: RepoEntry, raw: str) -> str:
    """校验仓库内相对路径并转成 git 可用的 POSIX 相对路径."""
    try:
        resolved = ctx.registry.resolve_path(entry, raw)
    except GitMcpError as exc:
        exc.meta.setdefault("repo", entry.alias)
        raise
    relative = os.path.relpath(resolved, entry.path)
    return Path(relative).as_posix()


def rel_paths(ctx: ToolContext, entry: RepoEntry, raws: Optional[Sequence[str]]) -> list[str]:
    return [rel_path(ctx, entry, raw) for raw in (raws or ())]


def require_ref(value: Any, *, name: str = "ref") -> str:
    """校验 ref 类自由文本: 非空、不以 ``-`` 开头、无空白/NUL."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgumentError(f"{name} 不能为空")
    if value.startswith("-"):
        raise InvalidArgumentError(f"{name} 不能以 - 开头(防止注入 git 选项): {value!r}")
    if any(char.isspace() for char in value):
        raise InvalidArgumentError(f"{name} 不能包含空白字符: {value!r}")
    if "\x00" in value:
        raise InvalidArgumentError(f"{name} 不能包含 NUL")
    return value


def require_text(value: Any, *, name: str) -> str:
    """校验 pattern/author 等自由文本(空值直接拒绝)."""
    if not isinstance(value, str) or value == "":
        raise InvalidArgumentError(f"{name} 不能为空")
    if "\x00" in value:
        raise InvalidArgumentError(f"{name} 不能包含 NUL")
    return value


def effective_bytes(ctx: ToolContext, requested: Any, *, default: int = DEFAULT_TEXT_BYTES) -> int:
    """工具级字节预算: 取请求值(或默认)并与服务器硬上限取小."""
    value = int(requested) if requested is not None else int(default)
    return max(1, min(value, ctx.config.max_output_bytes))


def effective_lines(requested: Any, *, default: int, hard_max: int = 2000) -> int:
    value = int(requested) if requested is not None else int(default)
    return max(1, min(value, hard_max))
