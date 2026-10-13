"""仓库注册表(``docs/GIT_MCP_DESIGN.md`` §3.2)。

- alias → 绝对路径(realpath 规范化), alias 唯一、大小写敏感(GM1 决策见 §14.1);
- 所有 repo 路径必须落在允许根内, 且是 git 工作树**顶层**
  (``git rev-parse --show-toplevel`` 与 realpath 一致, 防止一个子目录 alias
  意外把整个父仓库暴露给模型);
- 工具侧只接受 alias 与仓库内相对路径; ``resolve_path`` 拒绝绝对路径、``..``、
  以及 realpath 后逃逸到仓库外的符号链接(§6.5)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

from .errors import ConfigError, InvalidArgumentError, InvalidPathError, RepoNotFoundError
from .runner import GitRunner

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示, 避免运行时循环依赖
    from .config import ServerConfig

_PROBE_LIMIT_BYTES = 65_536


@dataclass(frozen=True)
class RepoEntry:
    """一个已注册仓库: alias、realpath 后的工作树顶层、所属允许根."""

    alias: str
    path: Path
    root: Path

    def __post_init__(self) -> None:
        if not self.alias:
            raise ConfigError("仓库 alias 不能为空")


class RepoRegistry:
    """alias/允许根/路径校验的唯一入口."""

    def __init__(
        self,
        entries: Sequence[RepoEntry],
        roots: Sequence[Path],
        *,
        runner: Optional[GitRunner] = None,
    ) -> None:
        if not entries:
            raise ConfigError("至少需要配置一个 git 仓库")
        if not roots:
            raise ConfigError("至少需要一个允许根")
        self._entries: tuple[RepoEntry, ...] = tuple(entries)
        self._roots: tuple[Path, ...] = tuple(roots)
        self._by_alias: dict[str, RepoEntry] = {entry.alias: entry for entry in self._entries}
        self._runner = runner or GitRunner()

    # ---- 构造 ------------------------------------------------------------

    @classmethod
    def build(cls, config: "ServerConfig", *, runner: Optional[GitRunner] = None) -> "RepoRegistry":
        """按配置构造并做启动期校验(路径存在 + 在允许根内 + 是 git 工作树顶层)."""
        git = runner or GitRunner.from_config(config)
        roots = _unique_realpaths(config.roots, what="允许根")
        for root in roots:
            if not root.is_dir():
                raise ConfigError(f"允许根不存在或不是目录: {root}")

        explicit = [(alias, _realpath(path)) for alias, path in config.repos]
        if explicit:
            entries = [
                cls._make_entry(alias, path, roots, git, explicit=True) for alias, path in explicit
            ]
        else:
            entries = []
            for root in roots:
                alias = root.name
                if not alias:
                    raise ConfigError(f"无法从路径推导 alias(请用 --repos 显式指定): {root}")
                entries.append(cls._make_entry(alias, root, roots, git, explicit=False))

        aliases = [entry.alias for entry in entries]
        duplicates = sorted({alias for alias in aliases if aliases.count(alias) > 1})
        if duplicates:
            raise ConfigError(f"alias 重复(请用 --repos 显式区分): {', '.join(duplicates)}")
        return cls(entries, roots, runner=git)

    @classmethod
    def _make_entry(
        cls,
        alias: str,
        path: Path,
        roots: Sequence[Path],
        runner: GitRunner,
        *,
        explicit: bool,
    ) -> RepoEntry:
        if not alias:
            raise ConfigError(f"仓库 alias 不能为空: {path}")
        if not path.is_dir():
            raise ConfigError(f"仓库路径不存在或不是目录: {path}")
        root = cls._containing_root(path, roots)
        if root is None:
            raise ConfigError(f"仓库路径必须在允许根内: {path}; 允许根: {[str(item) for item in roots]}")
        if not cls._is_worktree_top(path, runner):
            origin = "--repos" if explicit else "--root/--roots"
            raise ConfigError(
                f"{origin} 指向的不是 git 工作树顶层: {path}; "
                "请指向仓库根目录(或先 git init), 不要指向仓库子目录/裸仓库"
            )
        return RepoEntry(alias=alias, path=path, root=root)

    @staticmethod
    def _containing_root(path: Path, roots: Sequence[Path]) -> Optional[Path]:
        for root in roots:
            if path == root or root in path.parents:
                return root
        return None

    @classmethod
    def _is_worktree_top(cls, path: Path, runner: GitRunner) -> bool:
        """``rev-parse --is-inside-work-tree`` + ``--show-toplevel`` 双校验."""
        try:
            inside = runner.run(["rev-parse", "--is-inside-work-tree"], cwd=path, max_output_bytes=_PROBE_LIMIT_BYTES)
        except OSError as exc:  # git 不存在等
            raise ConfigError(f"无法执行 git 探测仓库: {path} ({exc})") from exc
        if not inside.ok or inside.stdout_text().strip() != "true":
            return False
        try:
            top = runner.run(["rev-parse", "--show-toplevel"], cwd=path, max_output_bytes=_PROBE_LIMIT_BYTES)
        except OSError as exc:
            raise ConfigError(f"无法执行 git 探测仓库: {path} ({exc})") from exc
        if not top.ok:
            return False
        toplevel = top.stdout_text().strip()
        if not toplevel:
            return False
        return _realpath(toplevel) == path

    # ---- 查询 ------------------------------------------------------------

    def list_repos(self) -> tuple[RepoEntry, ...]:
        return self._entries

    def roots(self) -> tuple[Path, ...]:
        return self._roots

    def resolve(self, alias: Optional[str] = None) -> RepoEntry:
        """按 alias 解析仓库; 单仓库模式下 ``None`` 可省略(§4.2)."""
        if alias is None or str(alias).strip() == "":
            if len(self._entries) == 1:
                return self._entries[0]
            known = ", ".join(entry.alias for entry in self._entries)
            raise InvalidArgumentError(f"未指定 repo 且配置了多个仓库; 可选 alias: {known}")
        entry = self._by_alias.get(str(alias))
        if entry is None:
            known = ", ".join(entry.alias for entry in self._entries)
            raise RepoNotFoundError(f"未知仓库 alias: {alias!r}; 已配置: {known}")
        return entry

    def resolve_path(self, entry: RepoEntry, relative_path: str) -> Path:
        """把仓库内相对路径解析为绝对路径; 越界(绝对路径/``..``/符号链接)直接拒绝."""
        if not isinstance(relative_path, str) or relative_path == "":
            raise InvalidPathError("路径不能为空")
        raw = relative_path
        if os.path.isabs(raw) or raw.startswith(("\\", "/")) or _has_drive_prefix(raw):
            raise InvalidPathError(f"只接受仓库内相对路径, 收到: {relative_path!r}")
        parts = [part for part in _split_separators(raw) if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise InvalidPathError(f"路径不能包含 ..: {relative_path!r}")
        candidate = entry.path.joinpath(*parts)
        resolved = _realpath(candidate)
        if resolved != entry.path and entry.path not in resolved.parents:
            raise InvalidPathError(f"路径越界(realpath 后不在仓库内): {relative_path!r}")
        return resolved


def _unique_realpaths(paths: Sequence[str], *, what: str) -> tuple[Path, ...]:
    seen: dict[Path, None] = {}
    for raw in paths:
        seen.setdefault(_realpath(raw))
    if not seen:
        raise ConfigError(f"{what}不能为空")
    return tuple(seen)


def _realpath(raw: os.PathLike[str] | str) -> Path:
    return Path(os.path.realpath(os.path.expanduser(os.fspath(raw))))


def _has_drive_prefix(raw: str) -> bool:
    return len(raw) >= 2 and raw[0].isalpha() and raw[1] == ":"


def _split_separators(raw: str) -> list[str]:
    """同时视 ``/`` 与 ``\\`` 为分隔符, 避免 Windows 风格 ``..\\`` 在 POSIX 上绕过校验."""
    normalized = raw.replace("\\", "/")
    return normalized.split("/")
