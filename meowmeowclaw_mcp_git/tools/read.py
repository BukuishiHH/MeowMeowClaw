"""只读/分析类工具(§4.1 A 组).

GM1 只实现 ``git_status``; ``git_diff`` / ``git_log`` 等随 GM2 加入, 并复用
``_run_git`` 与 meta 组装方式。
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..errors import GitMcpError, OutputTruncatedError, ParseError
from ..parsers.status import parse_porcelain_v2
from ..registry import RepoEntry
from ..runner import CommandTimeoutError, GitCommandError, GitResult
from .base import LEVEL_READ, ToolContext, ToolOutcome, ToolSpec

REPO_PARAM: dict[str, Any] = {
    "type": "string",
    "description": "仓库 alias; 单仓库模式可省略",
    "minLength": 1,
}


async def _run_git(ctx: ToolContext, entry: RepoEntry, args: list[str]) -> GitResult:
    """在事件循环外执行 git(状态查询可能达秒级, 避免阻塞 MCP 心跳/取消)."""
    return await asyncio.to_thread(ctx.runner.run, args, cwd=entry.path)


def _resolve_entry(ctx: ToolContext, repo: Any) -> RepoEntry:
    try:
        return ctx.registry.resolve(repo)
    except GitMcpError as exc:
        if repo is not None:
            exc.meta.setdefault("repo", repo)
        raise


async def git_status(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """工作区/暂存区/分支状态(porcelain v2 结构化)."""
    entry = _resolve_entry(ctx, args.get("repo"))
    result = await _run_git(ctx, entry, ["status", "--porcelain=v2", "--branch", "-z"])
    meta: dict[str, Any] = {"repo": entry.alias, **result.meta()}
    if result.timed_out:
        raise CommandTimeoutError(result, timeout_seconds=ctx.config.timeout_seconds, meta={"repo": entry.alias})
    if result.exit_code != 0:
        raise GitCommandError(result, meta={"repo": entry.alias})
    if result.truncated:
        # 残缺的 staged/unstaged 列表比报错更危险(模型可能据此认为工作区干净)
        raise OutputTruncatedError(
            f"git status 输出超过 {ctx.config.max_output_bytes} 字节上限(已截断), "
            "请提高 --max-output-bytes 或先缩小工作区变更范围",
            meta={**meta, "truncated": True},
        )
    try:
        summary = parse_porcelain_v2(result.stdout)
    except ParseError as exc:
        raise ParseError(str(exc), meta={**meta, **exc.meta}) from exc
    data = summary.to_short_data() if args.get("short") else summary.to_data()
    return ToolOutcome(data=data, meta=meta)


def build_read_tools() -> tuple[ToolSpec, ...]:
    """GM1 只读工具表(GM2 在此追加 diff/log/show/...)."""
    return (
        ToolSpec(
            name="git_status",
            title="Git 状态",
            description=(
                "查看仓库当前分支、ahead/behind 与工作区状态: staged/unstaged/untracked/conflicts "
                "路径列表(porcelain v2 解析); short=true 时只返回各组计数以节省上下文. "
                "只读, 不修改仓库."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo": REPO_PARAM,
                    "short": {
                        "type": "boolean",
                        "default": False,
                        "description": "true 时省略逐文件列表, 只返回分支信息与各组计数",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=git_status,
            level=LEVEL_READ,
        ),
    )
