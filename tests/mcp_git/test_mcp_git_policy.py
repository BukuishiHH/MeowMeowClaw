"""GM3 PolicyEngine 单测(``meowmeowclaw_mcp_git/policy.py``, §3.3/§6.3)."""

from __future__ import annotations

import pytest

from meowmeowclaw_mcp_git.config import ServerConfig
from meowmeowclaw_mcp_git.errors import DirtyWorktreeError, PolicyDeniedError, ProtectedBranchError
from meowmeowclaw_mcp_git.parsers.status import StatusSummary
from meowmeowclaw_mcp_git.policy import PolicyEngine


def summary(
    *,
    branch: str | None = "work",
    staged: tuple[str, ...] = (),
    unstaged: tuple[str, ...] = (),
    untracked: tuple[str, ...] = (),
    conflicts: tuple[str, ...] = (),
) -> StatusSummary:
    state = StatusSummary(branch=branch)
    state.staged = list(staged)
    state.unstaged = list(unstaged)
    state.untracked = list(untracked)
    state.conflicts = list(conflicts)
    return state


class TestWritesEnabled:
    def test_read_only_mode_denies_writes(self):
        policy = PolicyEngine(ServerConfig(read_only=True))

        with pytest.raises(PolicyDeniedError) as excinfo:
            policy.ensure_writes_enabled(action="git_commit")

        assert excinfo.value.code == "policy_denied"
        assert "只读" in str(excinfo.value)

    def test_writes_enabled_passes(self):
        PolicyEngine(ServerConfig(read_only=False)).ensure_writes_enabled()


class TestProtectedBranches:
    def test_default_protected_branches(self):
        policy = PolicyEngine(ServerConfig())

        assert policy.is_protected("main") is True
        assert policy.is_protected("master") is True
        assert policy.is_protected("work") is False
        assert policy.is_protected(None) is False

    def test_custom_and_empty_protected_branches(self):
        custom = PolicyEngine(ServerConfig(protected_branches=("release",)))
        empty = PolicyEngine(ServerConfig(protected_branches=()))

        assert custom.is_protected("release") is True
        assert custom.is_protected("main") is False
        assert empty.is_protected("main") is False

    def test_protected_commit_rejected(self):
        policy = PolicyEngine(ServerConfig())

        with pytest.raises(ProtectedBranchError) as excinfo:
            policy.ensure_branch_not_protected("main", action="commit")

        assert excinfo.value.code == "protected_branch"
        assert "main" in str(excinfo.value)

    def test_detached_head_passes(self):
        PolicyEngine(ServerConfig()).ensure_branch_not_protected(None, action="commit")


class TestBranchDeletable:
    def test_current_branch_rejected(self):
        policy = PolicyEngine(ServerConfig())

        with pytest.raises(PolicyDeniedError, match="当前分支"):
            policy.ensure_branch_deletable(summary(branch="work"), "work")

    def test_protected_branch_rejected(self):
        policy = PolicyEngine(ServerConfig())

        with pytest.raises(ProtectedBranchError) as excinfo:
            policy.ensure_branch_deletable(summary(branch="work"), "main")

        assert excinfo.value.code == "protected_branch"

    def test_other_branch_allowed(self):
        PolicyEngine(ServerConfig()).ensure_branch_deletable(summary(branch="work"), "feat/x")


class TestCleanGuard:
    def test_clean_passes(self):
        PolicyEngine(ServerConfig()).ensure_clean(summary(), action="git_restore")

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"staged": ("a.py",)},
            {"unstaged": ("a.py",)},
            {"untracked": ("new.md",)},
            {"conflicts": ("c.py",)},
        ],
    )
    def test_dirty_rejected(self, kwargs):
        policy = PolicyEngine(ServerConfig())

        with pytest.raises(DirtyWorktreeError) as excinfo:
            policy.ensure_clean(summary(**kwargs), action="git_restore")

        assert excinfo.value.code == "dirty_worktree"

    def test_force_bypasses_dirty(self):
        PolicyEngine(ServerConfig()).ensure_clean(summary(unstaged=("a.py",)), action="git_restore", force=True)
