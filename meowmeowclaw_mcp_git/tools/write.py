"""安全写工具(§4.1 B 组, GM3)。

共同约定:

- 仅 ``read_only=false`` 时注册(``app.select_tools``), 且每个 handler 仍二次校验权限;
- 每个写操作在仓库锁内执行(``ctx.locks.acquire``): dirty 检查与写操作原子, 并发写串行;
- 返回 ``before``/``after`` 状态摘要(分支/HEAD/dirty 文件数 + 分组计数, §4.2);
- 危险开关必须显式: ``force``(丢弃本地修改/强删分支)、``amend``、``allow_empty``、
  ``confirm``(stash drop), 默认一律拒绝;
- 不实现 destructive 工具(reset --hard/clean/push), 见 §6.2。
"""

from __future__ import annotations

from typing import Any, Optional

from ..errors import InvalidArgumentError, PolicyDeniedError
from ..parsers.refs import STASH_FORMAT, parse_stash_list
from ..parsers.status import StatusSummary
from ..registry import RepoEntry
from ..runner import GitResult
from . import common
from .base import LEVEL_WRITE, ToolContext, ToolOutcome, ToolSpec

STASH_ACTIONS = ("list", "push", "pop", "apply", "drop")


def _outcome(
    entry: RepoEntry,
    result: GitResult,
    *,
    action: str,
    before: Any,
    after: Any,
    extra: Optional[dict[str, Any]] = None,
) -> ToolOutcome:
    """组装写工具返回: before/after 摘要 + 工具特定字段 + 变更命令 meta(§4.2)."""
    data: dict[str, Any] = {
        "action": action,
        "before": before.to_state_dict(),
        "after": after.to_state_dict(),
    }
    if extra:
        data.update(extra)
    return ToolOutcome(data=data, meta=common.meta_for(entry, result))


async def git_stage(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """暂存路径(``git add``)或全部变更(``--all``)."""
    ctx.policy.ensure_writes_enabled(action="git_stage")
    entry = common.resolve_entry(ctx, args.get("repo"))
    stage_all = bool(args.get("all", False))
    paths = args.get("paths") or []
    if stage_all and paths:
        raise InvalidArgumentError("all=true 时不能再传 paths")
    if not stage_all and not paths:
        raise InvalidArgumentError("需要 paths(数组)或 all=true")
    rel = common.rel_paths(ctx, entry, paths)
    async with ctx.locks.acquire(entry):
        before = await common.read_status(ctx, entry)
        argv = ["add", "--all"] if stage_all else ["add", "--", *rel]
        result = await common.run_git(ctx, entry, argv)
        common.ensure_success(ctx, result, entry=entry, what="git add")
        after = await common.read_status(ctx, entry)
    return _outcome(entry, result, action="stage", before=before, after=after, extra={"paths": rel, "all": stage_all})


async def git_unstage(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """取消暂存(``git restore --staged`` / unborn 仓库用 ``git rm --cached``)."""
    ctx.policy.ensure_writes_enabled(action="git_unstage")
    entry = common.resolve_entry(ctx, args.get("repo"))
    rel = common.rel_paths(ctx, entry, common.require_paths(args.get("paths")))
    async with ctx.locks.acquire(entry):
        before = await common.read_status(ctx, entry)
        result = await common.run_git(ctx, entry, _unstage_argv(before, rel))
        common.ensure_success(ctx, result, entry=entry, what="git restore --staged")
        after = await common.read_status(ctx, entry)
    return _outcome(entry, result, action="unstage", before=before, after=after, extra={"paths": rel})


async def git_restore(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """恢复工作区(默认)或暂存区(staged=true); 工作区恢复默认要求干净, 需 force 才覆盖."""
    ctx.policy.ensure_writes_enabled(action="git_restore")
    entry = common.resolve_entry(ctx, args.get("repo"))
    rel = common.rel_paths(ctx, entry, common.require_paths(args.get("paths")))
    staged = bool(args.get("staged", False))
    force = bool(args.get("force", False))
    async with ctx.locks.acquire(entry):
        before = await common.read_status(ctx, entry)
        if staged:
            argv = _unstage_argv(before, rel)
        else:
            ctx.policy.ensure_clean(before, action=f"git_restore(覆盖工作区: {', '.join(rel[:5])})", force=force)
            argv = ["restore", "--", *rel]
        result = await common.run_git(ctx, entry, argv)
        common.ensure_success(ctx, result, entry=entry, what="git restore")
        after = await common.read_status(ctx, entry)
    return _outcome(
        entry, result, action="restore", before=before, after=after,
        extra={"paths": rel, "staged": staged, "force": force},
    )


async def git_commit(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """创建提交; paths 模式提交这些路径的工作区内容(不要求已暂存)."""
    ctx.policy.ensure_writes_enabled(action="git_commit")
    entry = common.resolve_entry(ctx, args.get("repo"))
    message = common.require_text(args.get("message"), name="message")
    if message.strip() == "":
        raise InvalidArgumentError("commit message 不能为空")
    amend = bool(args.get("amend", False))
    signoff = bool(args.get("signoff", False))
    allow_empty = bool(args.get("allow_empty", False))
    paths = args.get("paths") or []
    rel = common.rel_paths(ctx, entry, paths)
    async with ctx.locks.acquire(entry):
        before = await common.read_status(ctx, entry)
        ctx.policy.ensure_branch_not_protected(before.branch, action="commit(amend)" if amend else "commit")
        if not paths and not allow_empty and not amend and len(before.staged) == 0:
            raise InvalidArgumentError(
                "没有已暂存的变更; 请先 git_stage, 或显式传 allow_empty=true(amend 不受此限制)"
            )
        argv = ["commit", "-m", message]
        if amend:
            argv.append("--amend")
        if signoff:
            argv.append("--signoff")
        if allow_empty:
            argv.append("--allow-empty")
        if rel:
            argv += ["--", *rel]
        result = await common.run_git(ctx, entry, argv)
        common.ensure_success(ctx, result, entry=entry, what="git commit")
        after = await common.read_status(ctx, entry)
    return _outcome(
        entry, result, action="commit", before=before, after=after,
        extra={"message": message, "amend": amend, "signoff": signoff, "allow_empty": allow_empty, "paths": rel},
    )


async def git_branch_create(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """创建分支, 可选直接切换(switch=true)."""
    ctx.policy.ensure_writes_enabled(action="git_branch_create")
    entry = common.resolve_entry(ctx, args.get("repo"))
    name = await _validate_branch_name(ctx, entry, args.get("name"))
    start_point = args.get("start_point")
    if start_point is not None:
        start_point = common.require_ref(start_point, name="start_point")
    switch = bool(args.get("switch", False))
    async with ctx.locks.acquire(entry):
        before = await common.read_status(ctx, entry)
        argv = ["switch", "-c", name] if switch else ["branch", name]
        if start_point:
            argv.append(start_point)
        result = await common.run_git(ctx, entry, argv)
        common.ensure_success(ctx, result, entry=entry, what="git branch/switch -c")
        after = await common.read_status(ctx, entry)
    return _outcome(
        entry, result, action="branch_create", before=before, after=after,
        extra={"name": name, "start_point": start_point, "switch": switch},
    )


async def git_branch_switch(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """切换分支; 工作区不干净时默认拒绝, force=true 才丢弃修改(--discard-changes)."""
    ctx.policy.ensure_writes_enabled(action="git_branch_switch")
    entry = common.resolve_entry(ctx, args.get("repo"))
    name = _require_plain_name(args.get("name"), name="name")
    force = bool(args.get("force", False))
    async with ctx.locks.acquire(entry):
        before = await common.read_status(ctx, entry)
        ctx.policy.ensure_clean(before, action=f"切换到分支 {name!r}", force=force)
        argv = ["switch", "--discard-changes", name] if force else ["switch", name]
        result = await common.run_git(ctx, entry, argv)
        common.ensure_success(ctx, result, entry=entry, what="git switch")
        after = await common.read_status(ctx, entry)
    return _outcome(
        entry, result, action="branch_switch", before=before, after=after,
        extra={"name": name, "force": force},
    )


async def git_branch_delete(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """删除分支; 当前分支/保护分支/未合并分支默认拒绝, force=true 才强删(-D)."""
    ctx.policy.ensure_writes_enabled(action="git_branch_delete")
    entry = common.resolve_entry(ctx, args.get("repo"))
    name = _require_plain_name(args.get("name"), name="name")
    force = bool(args.get("force", False))
    async with ctx.locks.acquire(entry):
        before = await common.read_status(ctx, entry)
        ctx.policy.ensure_branch_deletable(before, name)
        if not force:
            merged = await common.run_git(ctx, entry, ["merge-base", "--is-ancestor", name, "HEAD"])
            if merged.exit_code == 1:
                raise PolicyDeniedError(
                    f"分支 {name!r} 未合并到当前分支, 默认拒绝删除; 确认要丢弃时传 force=true(git branch -D)"
                )
            if merged.exit_code not in (0, 1):
                common.ensure_success(ctx, merged, entry=entry, what="git merge-base --is-ancestor")
        argv = ["branch", "-D" if force else "-d", name]
        result = await common.run_git(ctx, entry, argv)
        common.ensure_success(ctx, result, entry=entry, what="git branch -d/-D")
        after = await common.read_status(ctx, entry)
    return _outcome(
        entry, result, action="branch_delete", before=before, after=after,
        extra={"name": name, "force": force},
    )


async def git_stash(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """stash 管理: list 只读; push/pop/apply/drop 在锁内执行."""
    ctx.policy.ensure_writes_enabled(action="git_stash")
    entry = common.resolve_entry(ctx, args.get("repo"))
    action = args.get("action") or "list"
    if action not in STASH_ACTIONS:
        raise InvalidArgumentError(f"stash action 只能是 {list(STASH_ACTIONS)}, 收到: {action!r}")
    message = args.get("message")
    paths = args.get("paths") or []
    confirm = bool(args.get("confirm", False))
    if message is not None and action != "push":
        raise InvalidArgumentError("message 仅用于 action=push")
    if paths and action != "push":
        raise InvalidArgumentError("paths 仅用于 action=push")
    if confirm and action != "drop":
        raise InvalidArgumentError("confirm 仅用于 action=drop")
    if action == "list":
        result = await common.run_git(ctx, entry, ["stash", "list", f"--format={STASH_FORMAT}"])
        common.ensure_success(ctx, result, entry=entry, what="git stash list")
        entries = parse_stash_list(result.stdout)
        data = {"action": "list", "entries": [item.to_dict() for item in entries], "count": len(entries)}
        return ToolOutcome(data=data, meta=common.meta_for(entry, result))
    rel = common.rel_paths(ctx, entry, paths) if paths else []

    async with ctx.locks.acquire(entry):
        before = await common.read_status(ctx, entry)
        if action == "push":
            argv = ["stash", "push"]
            if message is not None:
                argv += ["-m", common.require_text(message, name="message")]
            if rel:
                argv += ["--", *rel]
        elif action == "drop":
            if not confirm:
                raise PolicyDeniedError("stash drop 不可逆, 需要显式 confirm=true(并由 Cline 用户确认)")
            argv = ["stash", "drop"]
        else:  # pop / apply
            ctx.policy.ensure_clean(before, action=f"stash {action}(会覆盖工作区)")
            argv = ["stash", action]
        result = await common.run_git(ctx, entry, argv)
        common.ensure_success(ctx, result, entry=entry, what=f"git stash {action}")
        after = await common.read_status(ctx, entry)
        extra: dict[str, Any] = {"message": message, "paths": rel, "confirm": confirm}
        if action == "push":
            extra["created"] = "No local changes to save" not in result.stdout_text()
        return _outcome(entry, result, action=f"stash_{action}", before=before, after=after, extra=extra)


async def _validate_branch_name(ctx: ToolContext, entry: RepoEntry, value: Any) -> str:
    """用 ``git check-ref-format --branch`` 校验分支名(拒绝可疑转义与选项注入)."""
    name = _require_plain_name(value, name="name")
    if "@{" in name:
        raise InvalidArgumentError(f"分支名不能包含 '@{{'(会被 git 解释为 reflog 语法): {name!r}")
    result = await common.run_git(ctx, entry, ["check-ref-format", "--branch", name])
    if result.exit_code != 0:
        detail = result.stderr_text().strip().splitlines()
        raise InvalidArgumentError(f"分支名非法: {name!r}{('; ' + detail[0]) if detail else ''}")
    return name


def _require_plain_name(value: Any, *, name: str) -> str:
    text = common.require_text(value, name=name)
    if text.startswith("-"):
        raise InvalidArgumentError(f"{name} 不能以 - 开头: {text!r}")
    if text != text.strip():
        raise InvalidArgumentError(f"{name} 首尾不能有空白: {text!r}")
    return text


def _unstage_argv(before: StatusSummary, rel: list[str]) -> list[str]:
    """unborn 仓库没有 HEAD, ``git restore --staged`` 会失败, 改用 ``git rm --cached``."""
    if before.unborn:
        return ["rm", "--cached", "-r", "-q", "--", *rel]
    return ["restore", "--staged", "--quiet", "--", *rel]


def build_write_tools() -> tuple[ToolSpec, ...]:
    """GM3 安全写工具表(8 个; 仅 read_only=false 时注册)."""
    repo = common.repo_param()
    return (
        ToolSpec(
            name="git_stage",
            title="Git 暂存",
            description="暂存变更: paths 指定仓库内相对路径, 或 all=true 暂存全部(含删除). 写操作, 返回 before/after 状态.",
            parameters={
                "type": "object",
                "properties": {
                    "repo": repo,
                    "paths": common.paths_param("要暂存的仓库内相对路径"),
                    "all": {"type": "boolean", "default": False, "description": "true 时暂存所有变更(git add --all)"},
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=git_stage,
            level=LEVEL_WRITE,
        ),
        ToolSpec(
            name="git_unstage",
            title="Git 取消暂存",
            description="把 paths 从暂存区移出(git restore --staged; unborn 仓库用 git rm --cached). 写操作.",
            parameters={
                "type": "object",
                "properties": {"repo": repo, "paths": common.paths_param("要取消暂存的仓库内相对路径")},
                "required": ["paths"],
                "additionalProperties": False,
            },
            handler=git_unstage,
            level=LEVEL_WRITE,
        ),
        ToolSpec(
            name="git_restore",
            title="Git 恢复",
            description=(
                "staged=false(默认)时用 index 覆盖工作区, 会丢弃这些路径的本地修改: 工作区不干净时默认拒绝, "
                "需 force=true 显式放行; staged=true 时恢复暂存区(等价取消暂存, 不动工作区). 写操作."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo": repo,
                    "paths": common.paths_param("要恢复的仓库内相对路径"),
                    "staged": {"type": "boolean", "default": False, "description": "true 恢复暂存区(取消暂存)"},
                    "force": {"type": "boolean", "default": False, "description": "true 允许在工作区不干净时覆盖"},
                },
                "required": ["paths"],
                "additionalProperties": False,
            },
            handler=git_restore,
            level=LEVEL_WRITE,
        ),
        ToolSpec(
            name="git_commit",
            title="Git 提交",
            description=(
                "创建提交: message 必填且非空; 默认要求暂存区非空(allow_empty=true 或 amend 例外); "
                "protected-branches 上直接提交会被拒绝; paths 模式提交这些路径的工作区内容. 写操作, 返回 before/after."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo": repo,
                    "message": {"type": "string", "minLength": 1, "description": "提交信息(必填, 非空)"},
                    "paths": common.paths_param("限定提交路径(提交其工作区内容)"),
                    "amend": {"type": "boolean", "default": False, "description": "true 时改写最近一次提交"},
                    "signoff": {"type": "boolean", "default": False, "description": "true 时追加 Signed-off-by"},
                    "allow_empty": {"type": "boolean", "default": False, "description": "true 时允许空提交"},
                },
                "required": ["message"],
                "additionalProperties": False,
            },
            handler=git_commit,
            level=LEVEL_WRITE,
        ),
        ToolSpec(
            name="git_branch_create",
            title="Git 创建分支",
            description="创建分支(名字经 git check-ref-format 校验), switch=true 时直接切换过去. 写操作.",
            parameters={
                "type": "object",
                "properties": {
                    "repo": repo,
                    "name": {"type": "string", "minLength": 1, "description": "新分支名(必填)"},
                    "start_point": common.ref_param("起点 ref(默认当前 HEAD)"),
                    "switch": {"type": "boolean", "default": False, "description": "true 时创建后直接切换"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=git_branch_create,
            level=LEVEL_WRITE,
        ),
        ToolSpec(
            name="git_branch_switch",
            title="Git 切换分支",
            description="切换分支; 工作区不干净时默认拒绝, force=true 会用 --discard-changes 丢弃本地修改. 写操作.",
            parameters={
                "type": "object",
                "properties": {
                    "repo": repo,
                    "name": {"type": "string", "minLength": 1, "description": "目标分支名(必填)"},
                    "force": {"type": "boolean", "default": False, "description": "true 时丢弃本地修改强制切换"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=git_branch_switch,
            level=LEVEL_WRITE,
        ),
        ToolSpec(
            name="git_branch_delete",
            title="Git 删除分支",
            description=(
                "删除分支: 当前分支与 protected-branches 默认拒绝; 未合并分支默认拒绝, "
                "force=true 才用 git branch -D 强删. 写操作."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo": repo,
                    "name": {"type": "string", "minLength": 1, "description": "要删除的分支名(必填)"},
                    "force": {"type": "boolean", "default": False, "description": "true 时强删未合并分支"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=git_branch_delete,
            level=LEVEL_WRITE,
        ),
        ToolSpec(
            name="git_stash",
            title="Git stash",
            description=(
                "stash 管理: action=list 查看(push/pop/apply/drop); push 可带 message/paths; "
                "pop/apply 要求工作区干净; drop 不可逆, 需要 confirm=true. 写操作."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo": repo,
                    "action": {
                        "type": "string",
                        "enum": list(STASH_ACTIONS),
                        "default": "list",
                        "description": "list/push/pop/apply/drop",
                    },
                    "message": {"type": "string", "minLength": 1, "description": "action=push 时的 stash 说明"},
                    "paths": common.paths_param("action=push 时限定路径"),
                    "confirm": {"type": "boolean", "default": False, "description": "action=drop 必须显式 confirm=true"},
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=git_stash,
            level=LEVEL_WRITE,
        ),
    )
