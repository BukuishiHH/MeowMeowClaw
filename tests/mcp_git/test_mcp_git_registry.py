"""GM1 RepoRegistry 单测(``meowmeowclaw_mcp_git/registry.py``, 对照 §3.2/§6.5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from meowmeowclaw_mcp_git.config import ServerConfig
from meowmeowclaw_mcp_git.errors import ConfigError, InvalidArgumentError, InvalidPathError, RepoNotFoundError
from meowmeowclaw_mcp_git.registry import RepoRegistry


def make_registry(config: ServerConfig) -> RepoRegistry:
    return RepoRegistry.build(config)


class TestBuild:
    def test_single_root_alias_is_basename(self, repo: Path):
        registry = make_registry(ServerConfig(roots=(str(repo),)))

        entries = registry.list_repos()
        assert [entry.alias for entry in entries] == [repo.name]
        assert entries[0].path == repo.resolve()
        assert entries[0].root == repo.resolve()

    def test_explicit_repos_alias(self, repo: Path):
        registry = make_registry(ServerConfig(roots=(str(repo.parent),), repos=(("web", str(repo)),)))

        assert [entry.alias for entry in registry.list_repos()] == ["web"]
        assert registry.resolve("web").path == repo.resolve()

    def test_missing_root_rejected(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="不存在"):
            make_registry(ServerConfig(roots=(str(tmp_path / "nope"),)))

    def test_non_repo_root_rejected(self, tmp_path: Path):
        plain = tmp_path / "plain"
        plain.mkdir()

        with pytest.raises(ConfigError, match="git 工作树顶层"):
            make_registry(ServerConfig(roots=(str(plain),)))

    def test_repo_subdirectory_rejected(self, repo: Path):
        subdir = repo / "src"
        subdir.mkdir()

        # 子目录会让 git 操作作用到父仓库, 等于绕过 alias 边界, 启动即拒绝
        with pytest.raises(ConfigError, match="git 工作树顶层"):
            make_registry(ServerConfig(roots=(str(subdir),)))

    def test_bare_repo_rejected(self, tmp_path: Path, git):
        bare = tmp_path / "bare.git"
        git(tmp_path, "init", "--bare", "-q", str(bare))

        with pytest.raises(ConfigError, match="git 工作树顶层"):
            make_registry(ServerConfig(roots=(str(bare),)))

    def test_repo_outside_allow_roots_rejected(self, repo: Path, tmp_path: Path):
        other_root = tmp_path / "other"
        other_root.mkdir()

        config = ServerConfig(roots=(str(other_root),), repos=(("demo", str(repo)),))
        with pytest.raises(ConfigError, match="允许根内"):
            make_registry(config)

    def test_duplicate_basename_aliases_rejected(self, tmp_path: Path, git):
        first, second = tmp_path / "one" / "app", tmp_path / "two" / "app"
        for path in (first, second):
            path.mkdir(parents=True)
            git(path, "init", "-q", "-b", "main")

        with pytest.raises(ConfigError, match="alias 重复"):
            make_registry(ServerConfig(roots=(str(first), str(second))))

    def test_explicit_repos_disambiguate_duplicate_basenames(self, tmp_path: Path, git):
        first, second = tmp_path / "one" / "app", tmp_path / "two" / "app"
        for path in (first, second):
            path.mkdir(parents=True)
            git(path, "init", "-q", "-b", "main")

        config = ServerConfig(
            roots=(str(first), str(second)),
            repos=(("app-one", str(first)), ("app-two", str(second))),
        )
        registry = make_registry(config)

        assert sorted(entry.alias for entry in registry.list_repos()) == ["app-one", "app-two"]

    def test_worktree_roots_are_deduplicated(self, repo: Path):
        config = ServerConfig(roots=(str(repo), str(repo)))

        registry = make_registry(config)

        assert len(registry.roots()) == 1
        assert [entry.alias for entry in registry.list_repos()] == [repo.name]


class TestResolve:
    def test_single_repo_alias_optional(self, repo: Path):
        registry = make_registry(ServerConfig(roots=(str(repo),)))

        assert registry.resolve(None).alias == repo.name
        assert registry.resolve("").alias == repo.name
        assert registry.resolve(repo.name).path == repo.resolve()

    def test_unknown_alias(self, repo: Path):
        registry = make_registry(ServerConfig(roots=(str(repo),)))

        with pytest.raises(RepoNotFoundError) as excinfo:
            registry.resolve("ghost")

        assert excinfo.value.code == "repo_not_found"
        assert repo.name in str(excinfo.value)

    def test_multi_repo_requires_explicit_alias(self, repo: Path, tmp_path: Path, git):
        second = tmp_path / "second"
        second.mkdir()
        git(second, "init", "-q", "-b", "main")
        registry = make_registry(ServerConfig(roots=(str(repo), str(second))))

        with pytest.raises(InvalidArgumentError) as excinfo:
            registry.resolve(None)

        assert excinfo.value.code == "invalid_argument"
        assert "repo" in str(excinfo.value)


class TestResolvePath:
    def registry_and_entry(self, repo: Path):
        registry = make_registry(ServerConfig(roots=(str(repo),)))
        return registry, registry.resolve(None)

    def test_relative_path_resolves_inside_repo(self, repo: Path):
        registry, entry = self.registry_and_entry(repo)

        assert registry.resolve_path(entry, "src/a.py") == (repo / "src" / "a.py").resolve()
        assert registry.resolve_path(entry, ".") == repo.resolve()

    def test_dot_segments_are_normalized(self, repo: Path):
        registry, entry = self.registry_and_entry(repo)

        assert registry.resolve_path(entry, "a/./b/c.txt") == (repo / "a" / "b" / "c.txt").resolve()

    @pytest.mark.parametrize(
        "raw",
        [
            "/etc/passwd",
            "../outside",
            "a/../../outside",
            "src/../README.md",  # 即使不越界也拒绝 ..(§3.2)
            "..\\..\\windows",
            "C:\\temp",
        ],
    )
    def test_escape_attempts_rejected(self, repo: Path, raw: str):
        registry, entry = self.registry_and_entry(repo)

        with pytest.raises(InvalidPathError) as excinfo:
            registry.resolve_path(entry, raw)

        assert excinfo.value.code == "invalid_path"

    def test_empty_and_non_string_rejected(self, repo: Path):
        registry, entry = self.registry_and_entry(repo)

        with pytest.raises(InvalidPathError):
            registry.resolve_path(entry, "")
        with pytest.raises(InvalidPathError):
            registry.resolve_path(entry, None)  # type: ignore[arg-type]

    def test_symlink_escape_rejected(self, repo: Path, tmp_path: Path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("s3cret", encoding="utf-8")
        (repo / "leak").symlink_to(outside, target_is_directory=True)

        registry, entry = self.registry_and_entry(repo)
        with pytest.raises(InvalidPathError, match="越界"):
            registry.resolve_path(entry, "leak/secret.txt")

    def test_symlink_inside_repo_allowed(self, repo: Path):
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
        (repo / "alias-src").symlink_to(repo / "src", target_is_directory=True)

        registry, entry = self.registry_and_entry(repo)

        assert registry.resolve_path(entry, "alias-src/a.py") == (repo / "src" / "a.py").resolve()
