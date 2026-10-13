"""Git MCP Server 错误码与异常(``docs/GIT_MCP_DESIGN.md`` §4.2).

工具失败统一映射为 ``{ok:false, error_code, message, meta}``; 本模块不依赖 MCP SDK。
错误码是稳定契约, 模型/客户端可编程处理, 不要随意改名。
"""

from __future__ import annotations

from typing import Any, Optional

# ---- §4.2 错误码----------------------------------------------------------
ERROR_NOT_A_REPO = "not_a_repo"
ERROR_REPO_NOT_FOUND = "repo_not_found"
ERROR_POLICY_DENIED = "policy_denied"
ERROR_DIRTY_WORKTREE = "dirty_worktree"
ERROR_PROTECTED_BRANCH = "protected_branch"
ERROR_INVALID_PATH = "invalid_path"
ERROR_INVALID_ARGUMENT = "invalid_argument"
ERROR_COMMAND_TIMEOUT = "command_timeout"
ERROR_COMMAND_FAILED = "command_failed"
ERROR_OUTPUT_TRUNCATED = "output_truncated"
ERROR_UNSUPPORTED_IN_V1 = "unsupported_in_v1"
ERROR_PARSE_FAILED = "parse_failed"
# GM1 补充: 兜底异常(见 §14.1 实现记录), 避免任何未处理异常泄漏到协议层
ERROR_INTERNAL = "internal_error"

ALL_ERROR_CODES = (
    ERROR_NOT_A_REPO,
    ERROR_REPO_NOT_FOUND,
    ERROR_POLICY_DENIED,
    ERROR_DIRTY_WORKTREE,
    ERROR_PROTECTED_BRANCH,
    ERROR_INVALID_PATH,
    ERROR_INVALID_ARGUMENT,
    ERROR_COMMAND_TIMEOUT,
    ERROR_COMMAND_FAILED,
    ERROR_OUTPUT_TRUNCATED,
    ERROR_UNSUPPORTED_IN_V1,
    ERROR_PARSE_FAILED,
    ERROR_INTERNAL,
)


class GitMcpError(Exception):
    """业务错误基类: 携带稳定错误码与可选 meta(工具返回的失败信封直接用)."""

    code: str = ERROR_INTERNAL

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.meta: dict[str, Any] = dict(meta or {})


class ConfigError(GitMcpError):
    """启动配置非法: ``main`` 捕获后写 stderr 并以退出码 2 结束(不会进入工具信封)."""

    code = ERROR_INVALID_ARGUMENT


class InvalidArgumentError(GitMcpError):
    """参数缺失/类型错误/枚举越界/未知参数."""

    code = ERROR_INVALID_ARGUMENT


class RepoNotFoundError(GitMcpError):
    """alias 未在 RepoRegistry 中注册."""

    code = ERROR_REPO_NOT_FOUND


class NotARepoError(GitMcpError):
    """路径存在但不是 git 工作树(或不是工作树顶层)."""

    code = ERROR_NOT_A_REPO


class InvalidPathError(GitMcpError):
    """路径越界(绝对路径/``..``/符号链接逃逸)."""

    code = ERROR_INVALID_PATH


class ParseError(GitMcpError):
    """git 输出无法解析; 调用方应保留原始 stdout/stderr(§5.3)."""

    code = ERROR_PARSE_FAILED


class OutputTruncatedError(GitMcpError):
    """输出被截断且无法保证结果完整(工具会选择报错而不是返回残缺数据)."""

    code = ERROR_OUTPUT_TRUNCATED


class PolicyDeniedError(GitMcpError):
    """权限矩阵/分支保护/dirty 保护拒绝(GM3 起使用)."""

    code = ERROR_POLICY_DENIED


class DirtyWorktreeError(GitMcpError):
    """工作区有未提交修改, 操作被拒绝(GM3 起使用)."""

    code = ERROR_DIRTY_WORKTREE


class ProtectedBranchError(GitMcpError):
    """目标分支在 protected-branches 列表中(GM3 起使用)."""

    code = ERROR_PROTECTED_BRANCH


class UnsupportedInV1Error(GitMcpError):
    """能力明确不在 v1 范围(如 destructive)."""

    code = ERROR_UNSUPPORTED_IN_V1
