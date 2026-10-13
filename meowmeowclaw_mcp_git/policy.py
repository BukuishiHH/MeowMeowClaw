"""写操作策略(§3.3/§6.3): 权限开关、保护分支、dirty 保护、删除保护。

- 策略检查必须与写操作一起放在仓库锁内执行(先查后写要原子, 避免 TOCTOU);
- 错误码语义: 只读模式/危险操作 → ``policy_denied``; 保护分支 → ``protected_branch``;
  工作区不干净 → ``dirty_worktree``;
- 本模块不 import MCP SDK, 也不依赖 tools 层, 可独立单测。
"""

from __future__ import annotations

from typing import Optional

from .config import ServerConfig
from .errors import DirtyWorktreeError, PolicyDeniedError, ProtectedBranchError
from .parsers.status import StatusSummary


class PolicyEngine:
    """写工具共享的策略检查器(无状态, 只读 config)."""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.protected_branches = frozenset(config.protected_branches)

    # ---- 权限矩阵 --------------------------------------------------------

    def ensure_writes_enabled(self, *, action: str = "写操作") -> None:
        """read_only=true 时任何写操作都应被拒绝(工具理论上不会注册, 这是第二道防线)."""
        if self.config.read_only:
            raise PolicyDeniedError(
                f"服务器以只读模式运行(--read-only=true), 禁止 {action}; 需要显式 --read-only=false"
            )

    def ensure_remote_enabled(self, *, action: str = "远端操作") -> None:
        """``--allow-remote=false``(默认)时任何远端操作都应被拒绝(远端工具不会注册)."""
        if not self.config.allow_remote:
            raise PolicyDeniedError(
                f"远端能力未开启, 禁止 {action}; 需要显式 --allow-remote=true"
            )

    # ---- 分支保护 --------------------------------------------------------

    def is_protected(self, branch: Optional[str]) -> bool:
        return bool(branch) and branch in self.protected_branches

    def ensure_branch_not_protected(self, branch: Optional[str], *, action: str) -> None:
        """保护分支(default main,master)上禁止直接提交/改写历史."""
        if self.is_protected(branch):
            raise ProtectedBranchError(
                f"分支 {branch!r} 在 protected-branches({', '.join(sorted(self.protected_branches))}) 中, "
                f"禁止直接 {action}; 请新建分支操作, 或调整 --protected-branches"
            )

    def ensure_branch_deletable(self, summary: StatusSummary, name: str) -> None:
        """删除分支的前置保护: 当前分支与保护分支默认拒绝."""
        if summary.branch == name:
            raise PolicyDeniedError(f"不能删除当前分支 {name!r}; 请先切换到其他分支")
        if self.is_protected(name):
            raise ProtectedBranchError(
                f"分支 {name!r} 在 protected-branches 中, 禁止删除; 请调整 --protected-branches"
            )

    # ---- dirty 保护 ------------------------------------------------------

    def ensure_clean(self, summary: StatusSummary, *, action: str, force: bool = False) -> None:
        """工作区不干净时默认拒绝会覆盖/搬运本地修改的操作; ``force=true`` 显式放行."""
        if force or summary.clean:
            return
        raise DirtyWorktreeError(
            f"工作区不干净(staged={len(summary.staged)}, unstaged={len(summary.unstaged)}, "
            f"untracked={len(summary.untracked)}, conflicts={len(summary.conflicts)}), 拒绝 {action}; "
            "请先提交/暂存, 或显式传 force=true 承担丢弃修改的风险"
        )
