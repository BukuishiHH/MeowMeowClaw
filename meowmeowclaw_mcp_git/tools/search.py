"""搜索类只读工具(§4.1 A 组): ``git_grep``(工作区文本搜索)与
``git_search_history``(历史 pickaxe 搜索 ``-S``/``-G``)。

两个工具都受行数/字节预算约束: grep 结果按 ``max_lines``/``max_bytes`` 裁剪,
历史搜索按 ``max_count`` 限制并用 ``has_more`` 标记后续。
"""

from __future__ import annotations

from typing import Any

from ..budget import DEFAULT_ITEMS_BYTES, fit_items
from ..errors import InvalidArgumentError
from ..parsers.grep import parse_grep
from ..parsers.log import LOG_FORMAT, parse_log
from ..runner import GitCommandError
from . import common
from .base import LEVEL_READ, ToolContext, ToolOutcome, ToolSpec

PICKAXE_VALUES = ("S", "G")


async def git_grep(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """仓库内文本搜索(默认扩展正则 + 大小写敏感), 可限定 ref 与路径."""
    entry = common.resolve_entry(ctx, args.get("repo"))
    pattern = common.require_text(args.get("pattern"), name="pattern")
    ref = args.get("ref")
    paths = common.rel_paths(ctx, entry, args.get("paths"))
    regex = bool(args.get("regex", True))
    case_sensitive = bool(args.get("case_sensitive", True))
    max_lines = common.effective_lines(args.get("max_lines"), default=100, hard_max=1000)
    max_bytes = common.effective_bytes(ctx, args.get("max_bytes"), default=DEFAULT_ITEMS_BYTES)

    argv = ["grep", "--no-color", "--line-number", "-z", "-E" if regex else "-F"]
    if not case_sensitive:
        argv.append("-i")
    argv.append(f"-e{pattern}")  # 粘连传递, pattern 以 - 开头也不会被当作选项
    if ref is not None:
        argv.append(common.require_ref(ref, name="ref"))
    if paths:
        argv += ["--", *paths]

    result = await common.run_git(ctx, entry, argv)
    if result.timed_out:
        common.ensure_success(ctx, result, entry=entry, what="git grep")
    if not result.truncated and result.exit_code not in (0, 1):
        raise GitCommandError(result, meta={"repo": entry.alias})
    matches = parse_grep(result.stdout) if result.stdout else []
    kept, fit_truncated, _total = fit_items(
        [item.to_dict() for item in matches], max_bytes=max_bytes, max_items=max_lines
    )
    data = {
        "pattern": pattern,
        "ref": ref,
        "matches": kept,
        "count": len(kept),
        "max_lines": max_lines,
        "max_bytes": max_bytes,
        "truncated": bool(result.truncated or fit_truncated),
    }
    meta = common.meta_for(entry, result)
    if data["truncated"]:
        meta["truncated"] = True
    return ToolOutcome(data=data, meta=meta)


async def git_search_history(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """历史搜索: ``-S``(增删了该字符串的提交)或 ``-G``(diff 中出现该模式的提交)."""
    entry = common.resolve_entry(ctx, args.get("repo"))
    pattern = common.require_text(args.get("pattern"), name="pattern")
    pickaxe = args.get("pickaxe") or "S"
    if pickaxe not in PICKAXE_VALUES:
        raise InvalidArgumentError(f"pickaxe 只能是 {list(PICKAXE_VALUES)}, 收到: {pickaxe!r}")
    max_count = int(args.get("max_count", 20))

    argv = [
        "log",
        "--no-color",
        f"--max-count={max_count + 1}",
        f"--pretty=format:{LOG_FORMAT}",
        f"-{pickaxe}{pattern}",
    ]
    result = await common.run_git(ctx, entry, argv)
    common.ensure_success(ctx, result, entry=entry, what="git log -S/-G", allow_truncated=True)
    commits = parse_log(result.stdout)
    has_more = bool(result.truncated or len(commits) > max_count)
    commits = commits[:max_count]
    data = {
        "pattern": pattern,
        "pickaxe": pickaxe,
        "commits": [item.to_dict() for item in commits],
        "count": len(commits),
        "has_more": has_more,
    }
    meta = common.meta_for(entry, result)
    if has_more:
        meta["has_more"] = True
    return ToolOutcome(data=data, meta=meta)


def build_search_tools() -> tuple[ToolSpec, ...]:
    """GM2 搜索类只读工具表."""
    return (
        ToolSpec(
            name="git_grep",
            title="Git 文本搜索",
            description=(
                "在工作区或指定 ref 中搜索文本: 默认扩展正则(-E)、大小写敏感; 返回 path/line/content; "
                "结果受 max_lines/max_bytes 预算裁剪, truncated=true 表示还有更多匹配. 只读."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo": common.repo_param(),
                    "pattern": {"type": "string", "minLength": 1, "description": "搜索模式(必填)"},
                    "paths": common.paths_param("只搜索这些仓库内相对路径"),
                    "ref": common.ref_param("可选: 在指定 ref 而非工作区搜索(如 HEAD、v1.0)"),
                    "regex": {"type": "boolean", "default": True, "description": "true=扩展正则(-E), false=固定字符串(-F)"},
                    "case_sensitive": {"type": "boolean", "default": True, "description": "false 时忽略大小写(-i)"},
                    "max_lines": common.max_lines_param("最多返回匹配条数", 100, maximum=1000),
                    "max_bytes": common.max_bytes_param("匹配结果字节上限", DEFAULT_ITEMS_BYTES),
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            handler=git_grep,
            level=LEVEL_READ,
        ),
        ToolSpec(
            name="git_search_history",
            title="Git 历史搜索",
            description=(
                "搜索历史提交: pickaxe=S 找新增/删除该字符串的提交, G 找 diff 中出现该模式的提交; "
                "返回提交列表, has_more=true 表示还有更多. 只读."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo": common.repo_param(),
                    "pattern": {"type": "string", "minLength": 1, "description": "搜索字符串/模式(必填)"},
                    "pickaxe": {
                        "type": "string",
                        "enum": list(PICKAXE_VALUES),
                        "default": "S",
                        "description": "S=-S(增删字符串), G=-G(diff 模式)",
                    },
                    "max_count": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            handler=git_search_history,
            level=LEVEL_READ,
        ),
    )
