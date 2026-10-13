"""引用类只读工具(§4.1 A 组): branches / tags / remotes / rev_parse / config_get.

- 列表类工具要求 git 输出完整(截断即报 ``output_truncated``, 不给残缺列表);
- remote URL 统一脱敏(§6.4); config 敏感键(``http.*.extraheader``/credential/含
  token/password 的键)只返回 ``redacted=true``, 不泄露值。
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import InvalidArgumentError, ParseError
from ..parsers.refs import BRANCH_FORMAT, TAG_FORMAT, parse_branches, parse_remotes, parse_tags, sanitize_url
from ..runner import GitCommandError
from . import common
from .base import LEVEL_READ, ToolContext, ToolOutcome, ToolSpec

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SENSITIVE_KEY_HINTS = ("extraheader", "password", "passwd", "token", "secret", "oauth", "authkey", "apikey")


def _validate_key(value: Any) -> str:
    if not isinstance(value, str) or not _KEY_RE.match(value):
        raise InvalidArgumentError(
            f"config key 非法: {value!r}; 只允许字母/数字/./_/-(如 user.name、core.autocrlf)"
        )
    return value


def _is_sensitive_key(key: str) -> bool:
    lower = key.lower()
    if lower == "credential" or lower.startswith("credential."):
        return True
    return any(hint in lower for hint in _SENSITIVE_KEY_HINTS)


async def git_list_branches(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """分支列表: remote=false 本地分支, remote=true 远端跟踪分支."""
    entry = common.resolve_entry(ctx, args.get("repo"))
    remote = bool(args.get("remote", False))
    verbose = bool(args.get("verbose", False))
    scope = "refs/remotes" if remote else "refs/heads"
    result = await common.run_git(ctx, entry, ["for-each-ref", "--sort=refname", f"--format={BRANCH_FORMAT}", scope])
    common.ensure_success(ctx, result, entry=entry, what="git for-each-ref")
    branches = parse_branches(result.stdout)
    data = {
        "scope": "remote" if remote else "local",
        "branches": [item.to_dict(verbose=verbose) for item in branches],
        "count": len(branches),
    }
    return ToolOutcome(data=data, meta=common.meta_for(entry, result))


async def git_list_tags(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """tag 列表, 支持 glob pattern 过滤(如 v1.*)."""
    entry = common.resolve_entry(ctx, args.get("repo"))
    pattern = args.get("pattern")
    if pattern is not None:
        pattern = common.require_text(pattern, name="pattern")
        if pattern.startswith("-"):
            raise InvalidArgumentError(f"pattern 不能以 - 开头: {pattern!r}")
    argv = ["tag", f"--format={TAG_FORMAT}", "--list"]
    if pattern:
        argv.append(pattern)
    result = await common.run_git(ctx, entry, argv)
    common.ensure_success(ctx, result, entry=entry, what="git tag")
    tags = parse_tags(result.stdout)
    data = {"pattern": pattern, "tags": [item.to_dict() for item in tags], "count": len(tags)}
    return ToolOutcome(data=data, meta=common.meta_for(entry, result))


async def git_list_remotes(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """remote 列表; URL 已脱敏(userinfo/敏感查询参数打码)."""
    entry = common.resolve_entry(ctx, args.get("repo"))
    result = await common.run_git(ctx, entry, ["remote", "-v"])
    common.ensure_success(ctx, result, entry=entry, what="git remote -v")
    remotes = parse_remotes(result.stdout)
    data = {
        "remotes": [item.to_dict() for item in remotes],
        "count": len(remotes),
        "urls_masked": True,
    }
    return ToolOutcome(data=data, meta=common.meta_for(entry, result))


async def git_rev_parse(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """ref 解析为对象 hash(short=true 时返回缩写)."""
    entry = common.resolve_entry(ctx, args.get("repo"))
    ref = common.require_ref(args.get("ref"), name="ref")
    short = bool(args.get("short", False))
    argv = ["rev-parse", "--verify"]
    if short:
        argv.append("--short")
    argv.append(ref)
    result = await common.run_git(ctx, entry, argv)
    common.ensure_success(ctx, result, entry=entry, what="git rev-parse --verify")
    lines = [line.strip() for line in result.stdout_text().splitlines() if line.strip()]
    if not lines:
        raise ParseError("git rev-parse 未返回对象 hash", meta=common.meta_for(entry, result))
    data = {"ref": ref, "oid": lines[0], "short": short}
    return ToolOutcome(data=data, meta=common.meta_for(entry, result))


async def git_config_get(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """读取 git 配置; 缺失时 found=false, 敏感键只回 redacted=true."""
    entry = common.resolve_entry(ctx, args.get("repo"))
    try:
        key = _validate_key(args.get("key"))
    except InvalidArgumentError as exc:
        exc.meta.setdefault("repo", entry.alias)
        raise
    result = await common.run_git(ctx, entry, ["config", "--get", key])
    if result.timed_out:
        common.ensure_success(ctx, result, entry=entry, what="git config --get")
    if result.truncated:
        common.ensure_success(ctx, result, entry=entry, what="git config --get")
    meta = common.meta_for(entry, result)
    stdout = result.stdout_text()
    if result.exit_code == 1 and not stdout.strip():
        data = {"key": key, "found": False, "value": None, "redacted": False, "masked": False}
        return ToolOutcome(data=data, meta=meta)
    if result.exit_code != 0:
        raise GitCommandError(result, meta={"repo": entry.alias})

    raw_value = stdout.rstrip("\n")
    if _is_sensitive_key(key):
        data = {"key": key, "found": True, "value": None, "redacted": True, "masked": False}
        return ToolOutcome(data=data, meta=meta)
    masked_value = sanitize_url(raw_value)
    data = {
        "key": key,
        "found": True,
        "value": masked_value,
        "redacted": False,
        "masked": masked_value != raw_value,
    }
    return ToolOutcome(data=data, meta=meta)


def build_ref_tools() -> tuple[ToolSpec, ...]:
    """GM2 引用类只读工具表."""
    return (
        ToolSpec(
            name="git_list_branches",
            title="Git 分支列表",
            description=(
                "列出本地分支(remote=false)或远端跟踪分支(remote=true); verbose=true 时附带 "
                "upstream/ahead/behind/subject 等 tracking 信息. 只读."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo": common.repo_param(),
                    "remote": {"type": "boolean", "default": False, "description": "true 时列出远端跟踪分支"},
                    "verbose": {"type": "boolean", "default": False, "description": "true 时附带 tracking/主题等详情"},
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=git_list_branches,
            level=LEVEL_READ,
        ),
        ToolSpec(
            name="git_list_tags",
            title="Git tag 列表",
            description="列出 tag(名称/类型/短 hash/创建时间/主题), 可用 glob pattern 过滤(如 v1.*). 只读.",
            parameters={
                "type": "object",
                "properties": {
                    "repo": common.repo_param(),
                    "pattern": {"type": "string", "minLength": 1, "description": "可选 glob(如 v1.*)"},
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=git_list_tags,
            level=LEVEL_READ,
        ),
        ToolSpec(
            name="git_list_remotes",
            title="Git remote 列表",
            description="列出 remote 的 fetch/push URL; URL 已脱敏(userinfo 与 token 查询参数打码). 只读.",
            parameters={
                "type": "object",
                "properties": {"repo": common.repo_param()},
                "required": [],
                "additionalProperties": False,
            },
            handler=git_list_remotes,
            level=LEVEL_READ,
        ),
        ToolSpec(
            name="git_rev_parse",
            title="Git ref 解析",
            description="把 ref(HEAD、main、v1.0、HEAD~2 等)解析为对象 hash; short=true 返回缩写. 只读.",
            parameters={
                "type": "object",
                "properties": {
                    "repo": common.repo_param(),
                    "ref": common.ref_param("要解析的 ref(必填)"),
                    "short": {"type": "boolean", "default": False, "description": "true 时返回缩写 hash"},
                },
                "required": ["ref"],
                "additionalProperties": False,
            },
            handler=git_rev_parse,
            level=LEVEL_READ,
        ),
        ToolSpec(
            name="git_config_get",
            title="Git 配置读取",
            description=(
                "读取 git 配置(仓库/全局合并视图): 缺失时 found=false; 含 extraheader/credential/token 等 "
                "敏感键只返回 redacted=true(不泄露值), URL 值的 userinfo 会打码. 只读."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo": common.repo_param(),
                    "key": {"type": "string", "minLength": 1, "description": "配置键(如 user.name、core.autocrlf; 含 :// 的 URL 作用域键暂不支持)"},
                },
                "required": ["key"],
                "additionalProperties": False,
            },
            handler=git_config_get,
            level=LEVEL_READ,
        ),
    )
