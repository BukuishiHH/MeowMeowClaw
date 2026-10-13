"""远端只读工具(GM4): ``git_fetch``(§4.1 B)。

- 仅 ``--allow-remote=true`` 时注册(``app.select_tools``), handler 内再二次校验;
- fetch 只更新 remote-tracking refs, **不合并/rebase**(D7/T9);
- 会写 refs, 因此在仓库锁内执行(§5.5);
- 返回 before/after 状态摘要 + 远端 ref 变化(created/updated/deleted);
- fetch 进度在 stderr 且可能有副作用, 运行时不因输出超限被杀(仅截断标记);
  输出与错误消息统一经 ``sanitize_text`` 脱敏(§6.4)。
"""

from __future__ import annotations

from typing import Any, Optional

from ..budget import fit_text
from ..errors import GitMcpError
from ..parsers.refs import sanitize_text
from ..runner import CommandTimeoutError, GitCommandError
from . import common
from .base import LEVEL_REMOTE, ToolContext, ToolOutcome, ToolSpec

FETCH_OUTPUT_BYTES = 4096
_FIELD_SEP = "\x1f"


async def git_fetch(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """拉取远端对象(不合并/rebase)."""
    ctx.policy.ensure_remote_enabled(action="git_fetch")
    entry = common.resolve_entry(ctx, args.get("repo"))
    remote = args.get("remote")
    if remote is not None:
        remote = common.require_ref(remote, name="remote")
    prune = bool(args.get("prune", False))

    async with ctx.locks.acquire(entry):
        before = await common.read_status(ctx, entry)
        refs_before = await _remote_refs(ctx, entry, remote)
        argv = ["fetch"]
        if prune:
            argv.append("--prune")
        if remote:
            argv.append(remote)
        # kill_on_limit=False: fetch 中途被杀会留下半更新状态, 这里读完全部输出只做截断
        result = await common.run_git(ctx, entry, argv, kill_on_limit=False)
        await _ensure_success(ctx, entry, result)
        after = await common.read_status(ctx, entry)
        refs_after = await _remote_refs(ctx, entry, remote)

    changes = _diff_refs(refs_before, refs_after)
    output = fit_text(sanitize_text(result.stderr_text()), max_bytes=FETCH_OUTPUT_BYTES)
    data: dict[str, Any] = {
        "action": "fetch",
        "remote": remote,
        "prune": prune,
        "before": before.to_state_dict(),
        "after": after.to_state_dict(),
        "refs": changes,
        "refs_changed": len(changes),
        "output": output.text,
        "output_truncated": output.truncated,
    }
    meta = common.meta_for(entry, result)
    if result.truncated or output.truncated:
        meta["truncated"] = True
    return ToolOutcome(data=data, meta=meta)


async def _ensure_success(ctx: ToolContext, entry: Any, result: Any) -> None:
    """fetch 的错误消息需要脱敏(remote URL 可能含 token)."""
    if result.timed_out:
        raise CommandTimeoutError(result, timeout_seconds=ctx.config.timeout_seconds, meta={"repo": entry.alias})
    if result.exit_code != 0:
        lines = result.stderr_text().strip().splitlines()
        detail = sanitize_text(lines[0][:400]) if lines else ""
        raise GitCommandError(
            result,
            message=f"git fetch 失败(exit={result.exit_code}): {detail}".rstrip(": "),
            meta={"repo": entry.alias},
        )


async def _remote_refs(ctx: ToolContext, entry: Any, remote: Optional[str]) -> dict[str, str]:
    """读取 remote-tracking refs(name → oid), 用于对比 fetch 前后的变化."""
    prefix = f"refs/remotes/{remote}/" if remote else "refs/remotes/"
    result = await common.run_git(ctx, entry, ["for-each-ref", f"--format=%(refname:short){_FIELD_SEP}%(objectname)", prefix])
    common.ensure_success(ctx, result, entry=entry, what="git for-each-ref refs/remotes")
    refs: dict[str, str] = {}
    for line in result.stdout_text().splitlines():
        if not line:
            continue
        name, sep, oid = line.partition(_FIELD_SEP)
        if not sep:
            raise GitMcpError(f"无法解析远端 ref 行: {line[:120]!r}")
        refs[name] = oid
    return refs


def _diff_refs(before: dict[str, str], after: dict[str, str]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for name in sorted(set(before) | set(after)):
        old_oid, new_oid = before.get(name), after.get(name)
        if old_oid == new_oid:
            continue
        if old_oid is None:
            kind = "created"
        elif new_oid is None:
            kind = "deleted"
        else:
            kind = "updated"
        changes.append({"name": name, "change": kind, "old_oid": old_oid, "new_oid": new_oid})
    return changes


def build_remote_tools() -> tuple[ToolSpec, ...]:
    """GM4 远端只读工具表(仅 --allow-remote=true 时注册)."""
    return (
        ToolSpec(
            name="git_fetch",
            title="Git fetch",
            description=(
                "从远端拉取对象并更新 remote-tracking refs(不合并/rebase); remote 省略时用默认远端, "
                "prune=true 时清理已删除的远端分支; 返回 ref 变化与 before/after 状态. 需 --allow-remote=true."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo": common.repo_param(),
                    "remote": {"type": "string", "minLength": 1, "description": "远端名(如 origin; 省略用默认远端)"},
                    "prune": {"type": "boolean", "default": False, "description": "true 时删除远端已不存在的跟踪分支"},
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=git_fetch,
            level=LEVEL_REMOTE,
        ),
    )
