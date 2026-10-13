"""MCP Resources(GM4, §4.3): 低频只读资源.

URI 约定:

| URI | 内容 |
|---|---|
| ``git://<repo>/status`` | 当前状态摘要(JSON) |
| ``git://<repo>/head`` | HEAD/分支/upstream/ahead-behind(JSON) |
| ``git://<repo>/log/<ref>`` | 最近 20 条提交(JSON) |
| ``git://<repo>/file/<ref>/<path>`` | 指定 revision 的文件内容(text/plain; 二进制/截断时回 JSON 元信息) |

- 只读: 不修改 index/工作区/refs;
- 文件路径经 ``RepoRegistry.resolve_path`` 校验(拒绝绝对路径/``..``/符号链接逃逸);
- ``ref``/``path`` 的分界是模糊的: 先按**最长可解析 ref 前缀**猜测, 因此 ``origin/main`` 这类
  含 ``/`` 的 ref 可以直接写在 URI 里(也支持 URL 编码 ``%2F``);
- alias 匹配大小写不敏感(URL host 会被规范化), 但存在大小写歧义时返回错误。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from .errors import InvalidArgumentError, ParseError, RepoNotFoundError
from .parsers.log import LOG_FORMAT, parse_log
from .registry import RepoEntry
from .tools import common
from .tools.base import ToolContext
from .tools.read import git_file_at_revision, git_status

RESOURCE_LOG_COUNT = 20
JSON_MIME = "application/json"
TEXT_MIME = "text/plain"

_STATUS_DESCRIPTION = "仓库状态摘要: 分支/ahead-behind/暂存区与工作区文件(JSON)"
_HEAD_DESCRIPTION = "HEAD: 分支/commit/upstream/ahead-behind(JSON)"
_LOG_DESCRIPTION = "指定 ref 的最近提交列表(JSON)"
_FILE_DESCRIPTION = "指定 ref 的文件内容(text/plain; 二进制或截断时回 JSON)"


@dataclass(frozen=True)
class ResourcePayload:
    """一次 resource 读取的结果(text + mime)."""

    text: str
    mime_type: str = TEXT_MIME


def resource_listing(ctx: ToolContext) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 ``(静态资源, 资源模板)``; 静态资源按已配置仓库展开."""
    resources: list[dict[str, Any]] = []
    for entry in ctx.registry.list_repos():
        resources.append(
            {
                "uri": f"git://{entry.alias}/status",
                "name": f"{entry.alias}: status",
                "description": _STATUS_DESCRIPTION,
                "mimeType": JSON_MIME,
            }
        )
        resources.append(
            {
                "uri": f"git://{entry.alias}/head",
                "name": f"{entry.alias}: head",
                "description": _HEAD_DESCRIPTION,
                "mimeType": JSON_MIME,
            }
        )
    templates = [
        {
            "uriTemplate": "git://{repo}/log/{ref}",
            "name": "Git log",
            "description": _LOG_DESCRIPTION,
            "mimeType": JSON_MIME,
        },
        {
            "uriTemplate": "git://{repo}/file/{ref}/{path}",
            "name": "Git file at revision",
            "description": _FILE_DESCRIPTION,
            "mimeType": TEXT_MIME,
        },
    ]
    return resources, templates


async def read_git_resource(ctx: ToolContext, uri: str) -> ResourcePayload:
    """读取一个 ``git://`` 资源; 未知 URI/仓库/ref 抛业务错误."""
    parts = urlsplit(uri)
    if parts.scheme != "git":
        raise InvalidArgumentError(f"不支持的 resource scheme: {parts.scheme!r}; 只支持 git://")
    alias = unquote(parts.netloc)
    segments = [unquote(segment) for segment in parts.path.split("/") if segment]
    if not alias or not segments:
        raise InvalidArgumentError(f"resource URI 不完整: {uri!r}; 形如 git://<repo>/status")
    entry = _resolve_alias(ctx, alias)
    kind, rest = segments[0], segments[1:]

    if kind == "status":
        outcome = await git_status(ctx, {"repo": entry.alias, "short": False})
        return ResourcePayload(text=_dumps(outcome.data), mime_type=JSON_MIME)
    if kind == "head":
        summary = await common.read_status(ctx, entry)
        payload = {
            "branch": summary.branch,
            "head_oid": summary.head_oid,
            "detached": summary.detached,
            "unborn": summary.unborn,
            "upstream": summary.upstream,
            "ahead": summary.ahead,
            "behind": summary.behind,
        }
        return ResourcePayload(text=_dumps(payload), mime_type=JSON_MIME)
    if kind == "log":
        ref = await _resolve_ref(ctx, entry, rest, allow_range=True)
        return await _read_log(ctx, entry, ref)
    if kind == "file":
        ref, path = await _split_ref_and_path(ctx, entry, rest)
        return await _read_file(ctx, entry, ref, path)
    raise InvalidArgumentError(
        f"未知 resource: {uri!r}; 支持 status/head/log/<ref>/file/<ref>/<path>"
    )


async def _read_log(ctx: ToolContext, entry: RepoEntry, ref: str) -> ResourcePayload:
    result = await common.run_git(
        ctx,
        entry,
        ["log", "--no-color", f"--max-count={RESOURCE_LOG_COUNT}", f"--pretty=format:{LOG_FORMAT}", ref],
    )
    common.ensure_success(ctx, result, entry=entry, what="git log <ref>")
    commits = [item.to_dict() for item in parse_log(result.stdout)]
    payload = {"ref": ref, "count": len(commits), "commits": commits}
    return ResourcePayload(text=_dumps(payload), mime_type=JSON_MIME)


async def _read_file(ctx: ToolContext, entry: RepoEntry, ref: str, path: str) -> ResourcePayload:
    outcome = await git_file_at_revision(ctx, {"repo": entry.alias, "ref": ref, "path": path})
    data = outcome.data
    if data.get("binary") or data.get("truncated"):
        # 二进制/截断时返回 JSON 元信息(text 可能为 null), 让消费方明确知道内容不完整
        return ResourcePayload(text=_dumps(data), mime_type=JSON_MIME)
    return ResourcePayload(text=str(data.get("text") or ""), mime_type=TEXT_MIME)


async def _resolve_ref(ctx: ToolContext, entry: RepoEntry, segments: list[str], *, allow_range: bool) -> str:
    """把 URI 剩余段解析为 ref; 支持含 ``/`` 的 ref(最长可解析前缀)与 range(``a..b``)."""
    if not segments:
        raise InvalidArgumentError("resource URI 缺少 ref")
    full = "/".join(segments)
    if allow_range and ".." in full and await _log_accepts_ref(ctx, entry, full):
        return full
    for cut in range(len(segments), 0, -1):
        candidate = "/".join(segments[:cut])
        if await _is_valid_ref(ctx, entry, candidate):
            return candidate
    raise InvalidArgumentError(f"无法解析 resource 中的 ref: {full!r}")


async def _split_ref_and_path(ctx: ToolContext, entry: RepoEntry, segments: list[str]) -> tuple[str, str]:
    """把 ``file/<...>`` 的剩余段切成 ``(ref, path)``: 从最长前缀开始找可解析 ref."""
    if len(segments) < 2:
        raise InvalidArgumentError("file resource 需要 <ref>/<path>")
    for cut in range(len(segments) - 1, 0, -1):
        candidate = "/".join(segments[:cut])
        if await _is_valid_ref(ctx, entry, candidate):
            return candidate, "/".join(segments[cut:])
    raise InvalidArgumentError(f"无法解析 file resource 的 ref/path: {'/'.join(segments)!r}")


async def _is_valid_ref(ctx: ToolContext, entry: RepoEntry, candidate: str) -> bool:
    try:
        common.require_ref(candidate, name="ref")
    except InvalidArgumentError:
        return False
    result = await common.run_git(ctx, entry, ["rev-parse", "--verify", "--quiet", candidate])
    return result.exit_code == 0 and not result.timed_out and not result.truncated


async def _log_accepts_ref(ctx: ToolContext, entry: RepoEntry, candidate: str) -> bool:
    try:
        common.require_ref(candidate, name="ref")
    except InvalidArgumentError:
        return False
    result = await common.run_git(ctx, entry, ["log", "--max-count=1", "--pretty=format:%H", candidate])
    return result.exit_code == 0 and not result.timed_out and not result.truncated


def _resolve_alias(ctx: ToolContext, alias: str) -> RepoEntry:
    """先精确匹配; URL host 大小写被规范化时退化为大小写不敏感匹配(歧义则报错)."""
    try:
        return ctx.registry.resolve(alias)
    except RepoNotFoundError as exc:
        folded = [entry for entry in ctx.registry.list_repos() if entry.alias.casefold() == alias.casefold()]
        if len(folded) == 1:
            return folded[0]
        if len(folded) > 1:
            raise InvalidArgumentError(f"resource alias 大小写歧义: {alias!r}") from exc
        raise


def _dumps(payload: Any) -> str:
    """资源内容用紧凑 JSON(与工具信封同风格, 但资源本身不带 ok/meta 包装)."""
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 正常路径不会发生
        raise ParseError(f"resource JSON 序列化失败: {exc!r}") from exc
