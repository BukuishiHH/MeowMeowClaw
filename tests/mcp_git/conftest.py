"""``tests/mcp_git`` 公共 fixtures: 本地临时 git 仓库(不依赖网络, §9.2).

- 所有 git 子进程显式注入 ``HOME``/``GIT_CONFIG_NOSYSTEM``, 避免开发机全局
  ``.gitconfig``(gpgsign/hooks)影响用例;
- 没有 ``git`` 可执行文件时整组用例 skip, 不让本地环境问题变成误报。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

GIT_BASE_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "GIT_EDITOR": "true",
    "LC_ALL": "C",
}


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    """隔离后的 git 环境(``HOME`` 指向临时目录, 不读用户全局配置)."""
    if shutil.which("git") is None:
        pytest.skip("git 不可用")
    return {**os.environ, **GIT_BASE_ENV, "HOME": str(tmp_path)}


@pytest.fixture
def git(git_env: dict[str, str]) -> Callable[..., subprocess.CompletedProcess[str]]:
    """返回 ``run(repo, *args)`` 辅助函数."""

    def _run(
        repo: Path,
        *args: str,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            env={**git_env, **(env or {})},
            timeout=60,
        )
        if check and completed.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} 失败({completed.returncode}): {completed.stderr.strip()}"
            )
        return completed

    return _run


@pytest.fixture
def empty_repo(tmp_path: Path, git: Callable[..., Any]) -> Path:
    """``git init`` 后的空仓库(无提交, 分支 main, 本地身份已配置)."""
    repo = tmp_path / "demo-repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Git MCP Tester")
    git(repo, "config", "user.email", "tester@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    return repo


@pytest.fixture
def repo(empty_repo: Path, git: Callable[..., Any]) -> Path:
    """有一个 ``init`` 提交的干净仓库."""
    (empty_repo / "README.md").write_text("hello\n", encoding="utf-8")
    git(empty_repo, "add", "README.md")
    git(empty_repo, "commit", "-qm", "init")
    return empty_repo


@pytest.fixture
def remote_setup(tmp_path: Path, repo: Path, git: Callable[..., Any]) -> tuple[Path, Path]:
    """``repo`` 加上本地 bare remote(``origin``, main 已推送并设置 upstream)."""
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-q", str(remote))
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")  # 让 clone 默认检出 main
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    return repo, remote


@pytest.fixture
def config_for() -> Callable[..., Any]:
    """构造 :class:`ServerConfig` 的小工厂(默认单根 = ``repo``)."""
    from meowmeowclaw_mcp_git.config import ServerConfig

    def _make(
        repo: Path | None = None,
        *,
        roots: tuple[str, ...] | None = None,
        repos: tuple[tuple[str, str], ...] = (),
        **overrides: Any,
    ) -> ServerConfig:
        if roots is None:
            roots = (str(repo),) if repo is not None else ()
        return ServerConfig(roots=tuple(roots), repos=repos, **overrides)

    return _make


@pytest.fixture
def history_repo(tmp_path: Path, git: Callable[..., Any]) -> Path:
    """多提交/多分支/多 tag/多文件的仓库(GM2 工具测试通用 fixture).

    内容约定(供断言使用):

    - ``app.py`` 初次为 ``print("hello")``, 最后一个提交改为 ``print("hello world")``;
    - ``bin.dat`` 含 NUL(二进制); ``data/中文.txt`` UTF-8; ``data/gbk.txt`` GB18030;
    - tag: 轻量 ``v0.1`` + 附注 ``v0.2``; 分支: ``main`` + ``feature/login``;
    - 远端由需要的用例自行用 ``remote_setup`` 或 ``git remote add`` 构造。
    """
    repo = tmp_path / "history-repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Alice")
    git(repo, "config", "user.email", "alice@example.com")
    git(repo, "config", "commit.gpgsign", "false")

    (repo / "app.py").write_text('def main():\n    print("hello")\n', encoding="utf-8")
    (repo / "README.md").write_text("hello repo\n", encoding="utf-8")
    (repo / "bin.dat").write_bytes(b"binary\x00payload\n")
    (repo / "data").mkdir()
    (repo / "data" / "中文.txt").write_text("你好, 世界\n", encoding="utf-8")
    (repo / "data" / "gbk.txt").write_bytes("你好, 世界\n".encode("gb18030"))
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "feat: initial commit")
    git(repo, "tag", "v0.1")
    git(repo, "tag", "-a", "v0.2", "-m", "release v0.2")

    git(repo, "checkout", "-q", "-b", "feature/login")
    (repo / "login.py").write_text("def login(user):\n    return user is not None\n", encoding="utf-8")
    git(repo, "add", "login.py")
    git(repo, "commit", "-qm", "feat: add login")

    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text('def main():\n    print("hello world")\n', encoding="utf-8")
    git(repo, "add", "app.py")
    git(repo, "commit", "-qm", "fix: update greeting")
    return repo
