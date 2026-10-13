"""MCP Prompts(GM4, §4.4): 只生成提示词文本, **不执行任何写操作**。

四个 prompt 都基于只读查询(状态/diff/log/冲突文件)拼装 self-contained 提示词:

- ``write_commit_message``: 基于 staged(或工作区)diff 生成规范提交信息;
- ``review_staged_changes``: 生成代码 review 提示词(严重度/风险点/测试缺口);
- ``summarize_branch``: 汇总当前分支相对 base 的提交与差异;
- ``explain_conflict``: 解释冲突文件与解决思路(不自动写)。

MCP prompt 参数都是字符串, 布尔参数按 ``true/false/1/0/yes/no`` 解析; 提示词文本统一做
字节预算裁剪(默认 24KB), 避免把大 diff 原样塞进模型上下文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .budget import fit_text
from .errors import InvalidArgumentError
from .parsers.log import LOG_FORMAT, parse_log
from .registry import RepoEntry
from .tools import common
from .tools.base import ToolContext, ToolOutcome
from .tools.read import git_diff, git_status

PROMPT_TEXT_BYTES = 24_576
PROMPT_DIFF_BYTES = 16_384
PROMPT_FILE_BYTES = 16_384
PROMPT_MAX_FILES = 80

PromptBuilder = Callable[[ToolContext, dict[str, str]], Awaitable[str]]


@dataclass(frozen=True)
class PromptSpec:
    """一个 MCP prompt 的声明(name/参数/构建函数)."""

    name: str
    description: str
    builder: PromptBuilder
    arguments: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    title: Optional[str] = None

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title or self.name,
            "description": self.description,
            "arguments": [dict(item) for item in self.arguments],
        }


def build_prompt_specs() -> tuple[PromptSpec, ...]:
    """GM4 的 4 个 prompt(顺序稳定, 供 list_prompts 使用)."""
    return (
        PromptSpec(
            name="write_commit_message",
            title="生成提交信息",
            description="基于暂存区(或工作区)diff 生成 conventional commits 风格的中文提交信息; 只生成文本, 不提交.",
            arguments=(
                {"name": "repo", "description": "仓库 alias(单仓库模式可省略)", "required": False},
                {"name": "staged_only", "description": "true(默认)只看暂存区, false 看工作区", "required": False},
            ),
            builder=build_write_commit_message,
        ),
        PromptSpec(
            name="review_staged_changes",
            title="评审已暂存变更",
            description="针对已暂存 diff 生成代码 review 提示词: 严重度分级、风险点、测试缺口; 不修改代码.",
            arguments=({"name": "repo", "description": "仓库 alias(单仓库模式可省略)", "required": False},),
            builder=build_review_staged_changes,
        ),
        PromptSpec(
            name="summarize_branch",
            title="汇总分支变更",
            description="汇总当前分支相对 base 的提交与差异(变更主题/影响面/风险); 只生成报告文本.",
            arguments=(
                {"name": "repo", "description": "仓库 alias(单仓库模式可省略)", "required": False},
                {"name": "base", "description": "对比基线 ref(如 main、origin/main)", "required": True},
            ),
            builder=build_summarize_branch,
        ),
        PromptSpec(
            name="explain_conflict",
            title="解释冲突文件",
            description="解释冲突文件的内容与解决思路(展示冲突标记与上下文); 不自动解决冲突.",
            arguments=(
                {"name": "repo", "description": "仓库 alias(单仓库模式可省略)", "required": False},
                {"name": "path", "description": "冲突文件的仓库内相对路径", "required": True},
            ),
            builder=build_explain_conflict,
        ),
    )


# ---------------------------------------------------------------- builders


async def build_write_commit_message(ctx: ToolContext, args: dict[str, str]) -> str:
    entry = common.resolve_entry(ctx, args.get("repo"))
    staged_only = _bool_arg(args, "staged_only", default=True)
    status = await _status(ctx, entry)
    mode = "staged" if staged_only else "worktree"
    diff = await _diff(ctx, entry, mode=mode, max_bytes=PROMPT_DIFF_BYTES)
    scope = "暂存区(index)相对 HEAD" if staged_only else "工作区相对 index"
    text = f"""你是 Git 提交信息助手。请根据以下变更生成一条规范、可直接提交的中文提交信息。

仓库: {entry.alias}
分支: {_branch_name(status)}
变更范围: {scope}
变更统计:
{_format_files(diff.data.get("files", []))}

diff{_truncated_note(diff.data)}:
```diff
{diff.data.get("patch", "")}
```

要求:
1. 只输出提交信息本身, 不要解释、不要调用任何工具、不要执行写操作;
2. 首行不超过 72 个字符, 使用 conventional commits 风格(type(scope): 描述);
3. 需要时空一行后用 `-` 列出要点; 不要编造 diff 中不存在的改动;
4. 若没有任何变更, 直接说明"无变更", 不要编造内容。
"""
    return _fit_prompt(text)


async def build_review_staged_changes(ctx: ToolContext, args: dict[str, str]) -> str:
    entry = common.resolve_entry(ctx, args.get("repo"))
    status = await _status(ctx, entry)
    diff = await _diff(ctx, entry, mode="staged", max_bytes=PROMPT_DIFF_BYTES)
    text = f"""你是严格的代码评审者。请 review 以下已暂存(index)但尚未提交的变更。

仓库: {entry.alias}
分支: {_branch_name(status)}
变更统计:
{_format_files(diff.data.get("files", []))}

diff{_truncated_note(diff.data)}:
```diff
{diff.data.get("patch", "")}
```

要求:
1. 按严重度(blocker/major/minor/nit)列出问题, 每条给出 文件/位置 与 具体修改建议;
2. 重点关注: 正确性与边界、安全、并发与资源泄漏、性能、可维护性、测试缺口;
3. 指出变更中做得好的地方(如有);
4. 如果没有阻塞性问题, 明确说明, 并列出仍需人工确认的点;
5. 只输出评审意见, 不要调用任何工具、不要执行写操作。
"""
    return _fit_prompt(text)


async def build_summarize_branch(ctx: ToolContext, args: dict[str, str]) -> str:
    entry = common.resolve_entry(ctx, args.get("repo"))
    base = _require_arg(args, "base")
    base = common.require_ref(base, name="base")
    status = await _status(ctx, entry)
    diff = await _diff(ctx, entry, mode="range", base=base, head="HEAD", max_bytes=PROMPT_DIFF_BYTES)
    commits = await _log_range(ctx, entry, base)
    text = f"""你是变更汇总助手。请把当前分支相对基线 ref 的变更整理成一份可读报告。

仓库: {entry.alias}
当前分支: {_branch_name(status)}
基线 ref: {base}
提交列表({base}..HEAD, 最多 30 条):
{_format_commits(commits)}

变更统计:
{_format_files(diff.data.get("files", []))}

diff{_truncated_note(diff.data)}:
```diff
{diff.data.get("patch", "")}
```

要求:
1. 用 3-6 条要点概括变更主题与影响面;
2. 单独列出风险、不兼容变更、需要回归验证的点;
3. 不要编造未出现的提交或文件; 不要执行写操作。
"""
    return _fit_prompt(text)


async def build_explain_conflict(ctx: ToolContext, args: dict[str, str]) -> str:
    entry = common.resolve_entry(ctx, args.get("repo"))
    raw_path = _require_arg(args, "path")
    rel_path = common.rel_path(ctx, entry, raw_path)
    resolved = entry.path / rel_path
    if not resolved.is_file():
        raise InvalidArgumentError(f"冲突文件不存在或不是普通文件: {raw_path!r}")
    status = await _status(ctx, entry)
    conflicts = set(status.data.get("conflicts", []))
    in_conflict_list = raw_path in conflicts or rel_path in conflicts
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise InvalidArgumentError(f"读取文件失败: {raw_path!r} ({exc})") from exc
    truncated = len(raw) > PROMPT_FILE_BYTES
    content = raw[:PROMPT_FILE_BYTES].decode("utf-8", errors="replace")
    has_markers = _has_conflict_markers(content)
    text = f"""你是 Git 冲突解决助手。请解释以下冲突文件并提供解决思路(**不要自动修改任何文件**)。

仓库: {entry.alias}
文件: {rel_path}
在 status.conflicts 列表中: {"是" if in_conflict_list else "否"}
检测到冲突标记: {"是" if has_markers else "否"}
文件内容{("(已截断至 %d 字节)" % PROMPT_FILE_BYTES) if truncated else ""}:
```
{content}
```

要求:
1. 解释冲突双方的意图与分歧点(依据 `<<<<<<<` / `=======` / `>>>>>>>` 标记);
2. 给出逐步解决思路(保留哪一侧/如何合并/如何验证), 但不要直接给出未经确认的大段重写;
3. 指出需要人工决策的地方;
4. 只输出解释与建议, 不要调用写工具、不要修改文件。
"""
    return _fit_prompt(text)


# ---------------------------------------------------------------- helpers


async def _status(ctx: ToolContext, entry: RepoEntry) -> ToolOutcome:
    return await git_status(ctx, {"repo": entry.alias, "short": True})


async def _diff(ctx: ToolContext, entry: RepoEntry, **arguments: Any) -> ToolOutcome:
    return await git_diff(ctx, {"repo": entry.alias, **arguments})


async def _log_range(ctx: ToolContext, entry: RepoEntry, base: str) -> list[dict[str, Any]]:
    result = await common.run_git(
        ctx,
        entry,
        ["log", "--no-color", "--max-count=30", f"--pretty=format:{LOG_FORMAT}", f"{base}..HEAD"],
    )
    common.ensure_success(ctx, result, entry=entry, what="git log base..HEAD")
    return [item.to_dict() for item in parse_log(result.stdout)]


def _require_arg(args: dict[str, str], name: str) -> str:
    value = args.get(name)
    if value is None or str(value).strip() == "":
        raise InvalidArgumentError(f"prompt 参数 {name!r} 必填")
    return str(value).strip()


def _bool_arg(args: dict[str, str], name: str, *, default: bool) -> bool:
    if name not in args or args[name] is None or str(args[name]).strip() == "":
        return default
    text = str(args[name]).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise InvalidArgumentError(f"prompt 参数 {name} 需要 true/false, 收到: {args[name]!r}")


def _branch_name(status: ToolOutcome) -> str:
    branch = status.data.get("branch")
    if branch:
        return str(branch)
    return "(detached HEAD)" if status.data.get("detached") else "(unknown)"


def _format_files(files: list[dict[str, Any]]) -> str:
    if not files:
        return "(无变更文件)"
    lines: list[str] = []
    for item in files[:PROMPT_MAX_FILES]:
        suffix = f" (from {item['orig_path']})" if item.get("orig_path") else ""
        if item.get("binary"):
            lines.append(f"- {item['path']} (binary){suffix}")
        else:
            lines.append(f"- {item['path']} (+{item.get('added')}/-{item.get('deleted')}){suffix}")
    if len(files) > PROMPT_MAX_FILES:
        lines.append(f"- ... 其余 {len(files) - PROMPT_MAX_FILES} 个文件省略")
    return "\n".join(lines)


def _format_commits(commits: list[dict[str, Any]]) -> str:
    if not commits:
        return "(没有提交)"
    return "\n".join(f"- {item['short_hash']} {item['subject']} ({item['author']})" for item in commits)


def _truncated_note(data: dict[str, Any]) -> str:
    return "(已按预算截断)" if data.get("truncated") else ""


def _has_conflict_markers(content: str) -> bool:
    return any(
        line.startswith("<<<<<<<") or line.startswith("=======") or line.startswith(">>>>>>>")
        for line in content.splitlines()
    )


def _fit_prompt(text: str) -> str:
    fitted = fit_text(text, max_bytes=PROMPT_TEXT_BYTES)
    if fitted.truncated:
        return fitted.text + "\n\n...(提示词超长, 已按预算截断)"
    return text
