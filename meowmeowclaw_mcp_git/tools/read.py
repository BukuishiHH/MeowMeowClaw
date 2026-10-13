"""核心只读工具(§4.1 A 组): status / diff / log / show / blame / file_at_revision.

- 参数校验 + 路径防护在 :mod:`common`; 解析在 :mod:`meowmeowclaw_mcp_git.parsers`;
  文本裁剪在 :mod:`meowmeowclaw_mcp_git.budget`;
- 所有工具只读: 不修改 index/工作区/历史, 也不访问远端(§3.3 ``read`` 等级)。
"""

from __future__ import annotations

from typing import Any, Optional

from ..budget import DEFAULT_FILE_BYTES, DEFAULT_TEXT_BYTES, fit_items, fit_text
from ..errors import InvalidArgumentError, ParseError
from ..parsers.blame import parse_blame
from ..parsers.diff import parse_numstat
from ..parsers.log import LOG_FORMAT, parse_log, parse_show_meta
from ..parsers.status import parse_porcelain_v2
from . import common
from .base import LEVEL_READ, ToolContext, ToolOutcome, ToolSpec

DIFF_MODES = ("worktree", "staged", "commit", "range")


def _clip(value: Any, *, name: str) -> str:
    """file/pattern 等必填文本的兜底校验(直调 handler 时也安全)."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgumentError(f"{name} 不能为空")
    return value


async def git_status(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """工作区/暂存区/分支状态(porcelain v2 结构化)."""
    entry = common.resolve_entry(ctx, args.get("repo"))
    result = await common.run_git(ctx, entry, ["status", "--porcelain=v2", "--branch", "-z"])
    meta = common.meta_for(entry, result)
    common.ensure_success(ctx, result, entry=entry, what="git status")
    try:
        summary = parse_porcelain_v2(result.stdout)
    except ParseError as exc:
        raise ParseError(str(exc), meta={**meta, **exc.meta}) from exc
    data = summary.to_short_data() if args.get("short") else summary.to_data()
    return ToolOutcome(data=data, meta=meta)


async def git_diff(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """工作区/暂存区/提交/区间差异: numstat 结构化 + 受预算限制的 patch 片段."""
    entry = common.resolve_entry(ctx, args.get("repo"))
    mode = args.get("mode") or "worktree"
    base = args.get("base")
    head = args.get("head")
    stat_only = bool(args.get("stat_only", False))
    context_lines = int(args.get("context_lines", 3))
    max_bytes = common.effective_bytes(ctx, args.get("max_bytes"), default=DEFAULT_TEXT_BYTES)
    paths = common.rel_paths(ctx, entry, args.get("paths"))

    if mode in ("worktree", "staged") and (base or head):
        raise InvalidArgumentError(f"mode={mode} 不接受 base/head")
    if mode == "commit":
        if not base:
            raise InvalidArgumentError("mode=commit 需要 base")
        base = common.require_ref(base, name="base")
    elif mode == "range":
        if not base or not head:
            raise InvalidArgumentError("mode=range 需要 base 与 head")
        base = common.require_ref(base, name="base")
        head = common.require_ref(head, name="head")

    targets: list[str] = []
    if mode == "staged":
        targets = ["--cached"]
    elif mode == "commit":
        targets = [base]
    elif mode == "range":
        targets = [base, head]

    stat_args = ["diff", "--no-color", "--numstat", "-z", *targets]
    if paths:
        stat_args += ["--", *paths]
    stat_result = await common.run_git(ctx, entry, stat_args)
    common.ensure_success(ctx, stat_result, entry=entry, what="git diff --numstat")
    files = parse_numstat(stat_result.stdout)

    data: dict[str, Any] = {
        "mode": mode,
        "base": base,
        "head": head,
        "files": [item.to_dict() for item in files],
        "file_count": len(files),
        "stat_only": stat_only,
        "truncated": False,
    }
    meta = common.meta_for(entry, stat_result)
    if not stat_only:
        patch_args = ["diff", "--no-color", "--patch", f"-U{context_lines}", *targets]
        if paths:
            patch_args += ["--", *paths]
        patch_result = await common.run_git(ctx, entry, patch_args, max_output_bytes=max_bytes + 1)
        common.ensure_success(ctx, patch_result, entry=entry, what="git diff --patch", allow_truncated=True)
        fitted = fit_text(patch_result.stdout_text(), max_bytes=max_bytes)
        data["patch"] = fitted.text
        data["patch_truncated"] = bool(patch_result.truncated or fitted.truncated)
        data["max_bytes"] = max_bytes
        data["truncated"] = data["patch_truncated"]
        if data["truncated"]:
            meta["truncated"] = True
    return ToolOutcome(data=data, meta=meta)


async def git_log(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """提交历史(默认 20 条; ``has_more`` 表示还有更多)."""
    entry = common.resolve_entry(ctx, args.get("repo"))
    max_count = int(args.get("max_count", 20))
    argv = ["log", f"--max-count={max_count + 1}", f"--pretty=format:{LOG_FORMAT}"]
    author = args.get("author")
    since = args.get("since")
    until = args.get("until")
    grep = args.get("grep")
    if author is not None:
        argv.append(f"--author={common.require_text(author, name='author')}")
    if since is not None:
        argv.append(f"--since={common.require_text(since, name='since')}")
    if until is not None:
        argv.append(f"--until={common.require_text(until, name='until')}")
    if grep is not None:
        argv.append(f"--grep={common.require_text(grep, name='grep')}")
    path = args.get("path")
    if path is not None:
        argv += ["--", common.rel_path(ctx, entry, _clip(path, name="path"))]

    result = await common.run_git(ctx, entry, argv)
    common.ensure_success(ctx, result, entry=entry, what="git log", allow_truncated=True)
    commits = parse_log(result.stdout)
    has_more = bool(result.truncated or len(commits) > max_count)
    commits = commits[:max_count]
    data: dict[str, Any] = {
        "commits": [item.to_dict() for item in commits],
        "count": len(commits),
        "has_more": has_more,
    }
    meta = common.meta_for(entry, result)
    if has_more:
        meta["has_more"] = True
    return ToolOutcome(data=data, meta=meta)


async def git_show(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """单个 commit/tag 的元信息 + numstat + patch(按需)."""
    entry = common.resolve_entry(ctx, args.get("repo"))
    ref = common.require_ref(args.get("ref"), name="ref")
    stat_only = bool(args.get("stat_only", False))
    max_bytes = common.effective_bytes(ctx, args.get("max_bytes"), default=DEFAULT_TEXT_BYTES)
    commit_ref = f"{ref}^{{commit}}"

    meta_result = await common.run_git(
        ctx, entry, ["show", "-s", "--no-color", f"--format={LOG_FORMAT}", commit_ref]
    )
    common.ensure_success(ctx, meta_result, entry=entry, what="git show --format")
    commit = parse_show_meta(meta_result.stdout)

    stat_result = await common.run_git(ctx, entry, ["show", "--numstat", "-z", "--format=", commit_ref])
    common.ensure_success(ctx, stat_result, entry=entry, what="git show --numstat")
    files = parse_numstat(stat_result.stdout)

    data: dict[str, Any] = {
        "ref": ref,
        "commit": commit.to_dict(),
        "files": [item.to_dict() for item in files],
        "file_count": len(files),
        "stat_only": stat_only,
        "truncated": False,
    }
    meta = common.meta_for(entry, stat_result)
    if not stat_only:
        patch_result = await common.run_git(
            ctx,
            entry,
            ["show", "--patch", "--no-color", "--format=", commit_ref],
            max_output_bytes=max_bytes + 1,
        )
        common.ensure_success(ctx, patch_result, entry=entry, what="git show --patch", allow_truncated=True)
        fitted = fit_text(patch_result.stdout_text(), max_bytes=max_bytes)
        data["patch"] = fitted.text
        data["patch_truncated"] = bool(patch_result.truncated or fitted.truncated)
        data["max_bytes"] = max_bytes
        data["truncated"] = data["patch_truncated"]
        if data["truncated"]:
            meta["truncated"] = True
    return ToolOutcome(data=data, meta=meta)


async def git_blame(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """行级 blame(默认只取前 200 行并标记 truncated)."""
    entry = common.resolve_entry(ctx, args.get("repo"))
    path = common.rel_path(ctx, entry, _clip(args.get("file"), name="file"))
    start = int(args.get("start_line", 1))
    max_lines = common.effective_lines(args.get("max_lines"), default=200, hard_max=2000)
    max_bytes = common.effective_bytes(ctx, args.get("max_bytes"), default=DEFAULT_TEXT_BYTES)
    end_arg = args.get("end_line")
    truncated = False
    if end_arg is None:
        line_range = f"{start},+{max_lines}"
        requested_end: Optional[int] = None
    else:
        end = int(end_arg)
        if end < start:
            raise InvalidArgumentError("end_line 不能小于 start_line")
        if end - start + 1 > max_lines:
            end = start + max_lines - 1
            truncated = True
        line_range = f"{start},{end}"
        requested_end = end

    result = await common.run_git(ctx, entry, ["blame", "--line-porcelain", "-L", line_range, "--", path])
    common.ensure_success(ctx, result, entry=entry, what="git blame", allow_truncated=True)
    entries = parse_blame(result.stdout, truncated=result.truncated)
    fitted_lines, fit_truncated, _total = fit_items(
        [item.to_dict() for item in entries], max_bytes=max_bytes
    )
    if requested_end is None:
        # 未指定结束行: 取满 max_lines 才说明"可能还有更多"
        truncated = truncated or len(entries) >= max_lines
        actual_end = entries[-1].line if entries else start
    else:
        actual_end = requested_end
    data: dict[str, Any] = {
        "file": path,
        "start_line": start,
        "end_line": actual_end,
        "lines": fitted_lines,
        "count": len(fitted_lines),
        "max_bytes": max_bytes,
        "truncated": bool(result.truncated or truncated or fit_truncated),
    }
    meta = common.meta_for(entry, result)
    if data["truncated"]:
        meta["truncated"] = True
    return ToolOutcome(data=data, meta=meta)


async def git_file_at_revision(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """指定 ref 的文件内容(文本 + 编码/大小; 二进制文件只回元信息)."""
    entry = common.resolve_entry(ctx, args.get("repo"))
    ref = common.require_ref(args.get("ref"), name="ref")
    if ":" in ref:
        raise InvalidArgumentError("ref 不能包含 ':'(文件路径请用 path 参数)")
    path = common.rel_path(ctx, entry, _clip(args.get("path"), name="path"))
    max_bytes = common.effective_bytes(ctx, args.get("max_bytes"), default=DEFAULT_FILE_BYTES)
    object_ref = f"{ref}:{path}"

    size_result = await common.run_git(ctx, entry, ["cat-file", "-s", object_ref])
    common.ensure_success(ctx, size_result, entry=entry, what="git cat-file -s")
    size_text = size_result.stdout_text().strip()
    if not size_text.isdigit():
        raise ParseError(f"git cat-file -s 输出非法: {size_text[:80]!r}", meta=common.meta_for(entry, size_result))
    size = int(size_text)

    result = await common.run_git(ctx, entry, ["show", object_ref], max_output_bytes=max_bytes + 1)
    common.ensure_success(ctx, result, entry=entry, what="git show <ref>:<path>", allow_truncated=True)
    raw = result.stdout[:max_bytes]
    truncated = bool(result.truncated or size > max_bytes)
    binary = b"\0" in raw  # 返回前缀里出现 NUL 即按二进制处理
    encoding: Optional[str] = None
    text: Optional[str] = None
    if not binary:
        for candidate in ("utf-8", "gb18030"):
            try:
                text = raw.decode(candidate)
                encoding = candidate
                break
            except UnicodeDecodeError:
                continue
        if encoding is None:
            binary = True

    data: dict[str, Any] = {
        "ref": ref,
        "path": path,
        "size": size,
        "encoding": encoding,
        "binary": binary,
        "text": text,
        "max_bytes": max_bytes,
        "truncated": truncated,
    }
    meta = common.meta_for(entry, result)
    if truncated:
        meta["truncated"] = True
    return ToolOutcome(data=data, meta=meta)


def build_core_tools() -> tuple[ToolSpec, ...]:
    """GM1+GM2 核心只读工具表."""
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
                    "repo": common.repo_param(),
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
        ToolSpec(
            name="git_diff",
            title="Git 差异",
            description=(
                "查看差异: mode=worktree 工作区 vs index, staged index vs HEAD, commit 与指定 base 比较, "
                "range base 与 head 比较. 始终返回 numstat 结构化 files; stat_only=false 时附带受 max_bytes "
                "限制的 patch 片段(truncated=true 表示被截断). 只读."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo": common.repo_param(),
                    "mode": {
                        "type": "string",
                        "enum": list(DIFF_MODES),
                        "default": "worktree",
                        "description": "对比模式; commit/range 需要 base(head) 参数",
                    },
                    "base": common.ref_param("起始 ref(mode=commit/range 必填; 例如 HEAD~1、main、v1.0)"),
                    "head": common.ref_param("结束 ref(mode=range 必填)"),
                    "paths": common.paths_param("只比较这些仓库内相对路径"),
                    "stat_only": {"type": "boolean", "default": False, "description": "true 时只返回 numstat 统计"},
                    "context_lines": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "default": 3,
                        "description": "patch 上下文行数",
                    },
                    "max_bytes": common.max_bytes_param("patch 字节上限", DEFAULT_TEXT_BYTES),
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=git_diff,
            level=LEVEL_READ,
        ),
        ToolSpec(
            name="git_log",
            title="Git 历史",
            description=(
                "查看提交历史(默认 20 条, 最多 1000): 支持 author/since/until/path/grep 过滤; "
                "has_more=true 表示还有更多提交, 可用 max_count 或过滤条件缩小范围. 只读."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo": common.repo_param(),
                    "max_count": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 20},
                    "author": {"type": "string", "minLength": 1, "description": "作者过滤(正则, 匹配姓名/邮箱)"},
                    "since": {"type": "string", "minLength": 1, "description": "起始时间(如 2026-01-01、2 weeks ago)"},
                    "until": {"type": "string", "minLength": 1, "description": "结束时间"},
                    "path": {"type": "string", "minLength": 1, "description": "只查该仓库内相对路径的历史"},
                    "grep": {"type": "string", "minLength": 1, "description": "只查 subject/正文匹配正则的提交"},
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=git_log,
            level=LEVEL_READ,
        ),
        ToolSpec(
            name="git_show",
            title="Git 提交详情",
            description=(
                "查看单个 commit/tag 的元信息 + numstat; stat_only=false 时附带受 max_bytes 限制的 patch "
                "片段. ref 会解析为 commit(附注 tag 自动解引用); 文件内容请用 git_file_at_revision. 只读."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo": common.repo_param(),
                    "ref": common.ref_param("commit/tag 等 ref(必填; tag 会自动解析到对应 commit)"),
                    "stat_only": {"type": "boolean", "default": False, "description": "true 时只返回 numstat 统计"},
                    "max_bytes": common.max_bytes_param("patch 字节上限", DEFAULT_TEXT_BYTES),
                },
                "required": ["ref"],
                "additionalProperties": False,
            },
            handler=git_show,
            level=LEVEL_READ,
        ),
        ToolSpec(
            name="git_blame",
            title="Git 行级 blame",
            description=(
                "对文件指定行范围做 blame: 每行返回 commit hash/作者/时间/内容. 未指定 end_line 时取前 "
                "max_lines 行; truncated=true 表示还有更多行或被预算裁剪, 需用 start_line/end_line 分段读取. 只读."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo": common.repo_param(),
                    "file": {"type": "string", "minLength": 1, "description": "仓库内相对文件路径(必填)"},
                    "start_line": {"type": "integer", "minimum": 1, "default": 1, "description": "起始行号(1-based)"},
                    "end_line": {"type": "integer", "minimum": 1, "description": "结束行号(可省略, 默认取 max_lines 行)"},
                    "max_lines": common.max_lines_param("本次最多返回行数", 200),
                    "max_bytes": common.max_bytes_param("返回字节上限", DEFAULT_TEXT_BYTES),
                },
                "required": ["file"],
                "additionalProperties": False,
            },
            handler=git_blame,
            level=LEVEL_READ,
        ),
        ToolSpec(
            name="git_file_at_revision",
            title="指定版本的文件内容",
            description=(
                "读取某个 ref 下的文件内容: 返回 text/encoding/size; 二进制文件返回 binary=true 且 text=null; "
                "超过 max_bytes 时 text 为前缀并置 truncated=true. 路径必须是仓库内相对路径. 只读."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo": common.repo_param(),
                    "ref": common.ref_param("版本 ref(必填; 如 HEAD、main、v1.0、HEAD~2)"),
                    "path": {"type": "string", "minLength": 1, "description": "仓库内相对文件路径(必填)"},
                    "max_bytes": common.max_bytes_param("文件内容字节上限", DEFAULT_FILE_BYTES),
                },
                "required": ["ref", "path"],
                "additionalProperties": False,
            },
            handler=git_file_at_revision,
            level=LEVEL_READ,
        ),
    )
