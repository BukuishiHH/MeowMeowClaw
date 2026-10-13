"""Git MCP Server 启动配置(``docs/GIT_MCP_DESIGN.md`` §3.1)。

规则:

- 优先级 **命令行 > 环境变量 > 默认值**;
- 本模块只做解析与基础校验, 不访问文件系统(根目录存在性 / git 仓库校验属于
  :class:`~meowmeowclaw_mcp_git.registry.RepoRegistry`);
- 不 import MCP SDK, 可脱离 ``mcp`` 依赖单测(G7)。

GM1 只消费 ``roots`` / ``repos`` / ``read_only`` / ``timeout_seconds`` /
``max_output_bytes`` / ``log_level``; 其余键(GM3 `lock_dir`、GM4 remote、
GM6 audit)先按 §3.1 收下并保存在配置里, 由后续里程碑消费。
"""

from __future__ import annotations

import argparse
import math
import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from . import __version__
from .errors import ConfigError

# ---- 环境变量(§3.1)-------------------------------------------------------
ENV_ROOT = "MCP_GIT_ROOT"
ENV_ROOTS = "MCP_GIT_ROOTS"
ENV_REPOS = "MCP_GIT_REPOS"
ENV_READ_ONLY = "MCP_GIT_READ_ONLY"
ENV_ALLOW_REMOTE = "MCP_GIT_ALLOW_REMOTE"
ENV_ALLOW_DESTRUCTIVE = "MCP_GIT_ALLOW_DESTRUCTIVE"
ENV_PROTECTED_BRANCHES = "MCP_GIT_PROTECTED_BRANCHES"
ENV_TIMEOUT = "MCP_GIT_TIMEOUT"
ENV_MAX_OUTPUT_BYTES = "MCP_GIT_MAX_OUTPUT_BYTES"
ENV_AUDIT_LOG = "MCP_GIT_AUDIT_LOG"
ENV_LOCK_DIR = "MCP_GIT_LOCK_DIR"
ENV_LOG_LEVEL = "MCP_GIT_LOG_LEVEL"

TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_OUTPUT_BYTES = 131_072
DEFAULT_PROTECTED_BRANCHES = ("main", "master")
DEFAULT_LOG_LEVEL = "INFO"


@dataclass(frozen=True)
class ServerConfig:
    """不可变启动配置; 由 :func:`load_config` 构造."""

    roots: tuple[str, ...] = ()
    repos: tuple[tuple[str, str], ...] = ()
    read_only: bool = True
    allow_remote: bool = False
    allow_destructive: bool = False
    protected_branches: tuple[str, ...] = DEFAULT_PROTECTED_BRANCHES
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    audit_log: str = ""
    lock_dir: str = ""
    log_level: str = DEFAULT_LOG_LEVEL

    def repo_aliases(self) -> tuple[str, ...]:
        """显式配置的 alias(单根模式下的 basename alias 由 registry 生成)."""
        return tuple(alias for alias, _ in self.repos)

    def describe(self) -> str:
        """单行摘要, 用于 stderr 启动日志(不含任何密钥)."""
        aliases = ",".join(self.repo_aliases()) or "(单根 basename)"
        return (
            f"roots={list(self.roots)} repos={aliases} read_only={self.read_only} "
            f"allow_remote={self.allow_remote} timeout={self.timeout_seconds:g}s "
            f"max_output_bytes={self.max_output_bytes} log_level={self.log_level}"
        )


def parse_bool(value: Any, *, name: str) -> bool:
    """解析 true/false(兼容 1/0/yes/no/on/off), 非法值抛 :class:`ConfigError`."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    raise ConfigError(f"{name} 需要 true/false(或 1/0/yes/no), 收到: {value!r}")


def parse_positive_float(value: Any, *, name: str) -> float:
    """解析 > 0 的浮点数(超时等)."""
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 需要数字, 收到: {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ConfigError(f"{name} 必须为正数, 收到: {value!r}")
    return number


def parse_positive_int(value: Any, *, name: str) -> int:
    """解析 > 0 的整数(输出上限等)."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 需要整数, 收到: {value!r}") from exc
    if number <= 0:
        raise ConfigError(f"{name} 必须为正整数, 收到: {value!r}")
    return number


def normalize_path(raw: str) -> str:
    """展开 ``~`` 并转绝对路径(不解析符号链接, realpath 由 registry 负责)."""
    return os.path.abspath(os.path.expanduser(str(raw)))


def split_paths(raw: str) -> list[str]:
    """按平台分隔符切分允许根(§3.1: POSIX ``:`` / Windows ``;``)."""
    parts = [part.strip() for part in str(raw).split(os.pathsep)]
    return [normalize_path(part) for part in parts if part]


def parse_repos(raw: str) -> list[tuple[str, str]]:
    """解析 ``alias=path,...``; alias 唯一(大小写敏感, 见 §14.1)."""
    repos: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in str(raw).split(","):
        item = item.strip()
        if not item:
            continue
        alias, sep, path = item.partition("=")
        alias = alias.strip()
        path = path.strip()
        if not sep or not alias or not path:
            raise ConfigError(f"--repos 需要 alias=path 形式, 收到: {item!r}")
        if alias in seen:
            raise ConfigError(f"--repos 中 alias 重复: {alias!r}")
        seen.add(alias)
        repos.append((alias, normalize_path(path)))
    if not repos:
        raise ConfigError("--repos 不能为空")
    return repos


def split_branches(raw: str) -> tuple[str, ...]:
    """解析逗号分隔的分支名; 显式空字符串表示“无保护分支”."""
    return tuple(part.strip() for part in str(raw).split(",") if part.strip())


class _ConfigArgumentParser(argparse.ArgumentParser):
    """参数错误统一抛 :class:`ConfigError`(由 main 落 stderr + 退出码 2)."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise ConfigError(f"参数错误: {message}")


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器(§3.1 全量键; ``--help`` 正常打印)."""
    parser = _ConfigArgumentParser(
        prog="meowmeowclaw-mcp-git",
        description="MeowMeowClaw Git MCP Server(stdio, 默认只读)",
    )
    parser.add_argument("--root", metavar="PATH", help=f"单仓库模式根目录(环境变量 {ENV_ROOT}, 默认当前目录)")
    parser.add_argument(
        "--roots",
        metavar="PATH1:PATH2",
        help=f"多根模式允许根, 按平台分隔符切分(环境变量 {ENV_ROOTS})",
    )
    parser.add_argument(
        "--repos",
        metavar="alias=path,...",
        help=f"显式 alias → 路径(环境变量 {ENV_REPOS}); 未配置时用根目录 basename",
    )
    parser.add_argument(
        "--read-only",
        nargs="?",
        const="true",
        metavar="{true,false}",
        help=f"是否允许写工具(默认 true; 环境变量 {ENV_READ_ONLY})",
    )
    parser.add_argument(
        "--allow-remote",
        nargs="?",
        const="true",
        metavar="{true,false}",
        help=f"是否允许远端只读/fetch(默认 false; 环境变量 {ENV_ALLOW_REMOTE})",
    )
    parser.add_argument(
        "--allow-destructive",
        nargs="?",
        const="true",
        metavar="{true,false}",
        help=f"保留键: v1 不实现破坏性工具(默认 false; 环境变量 {ENV_ALLOW_DESTRUCTIVE})",
    )
    parser.add_argument(
        "--protected-branches",
        metavar="main,master",
        help=f"禁止直接提交的分支(默认 main,master; 环境变量 {ENV_PROTECTED_BRANCHES})",
    )
    parser.add_argument("--timeout", metavar="SECONDS", help=f"单条 git 命令超时(默认 30; 环境变量 {ENV_TIMEOUT})")
    parser.add_argument(
        "--max-output-bytes",
        metavar="BYTES",
        help=f"单工具 stdout 上限, 超出截断(默认 131072; 环境变量 {ENV_MAX_OUTPUT_BYTES})",
    )
    parser.add_argument("--audit-log", metavar="PATH", help=f"审计 JSONL 路径(GM1 起收下; 环境变量 {ENV_AUDIT_LOG})")
    parser.add_argument("--lock-dir", metavar="PATH", help=f"跨进程锁目录(GM3 起使用; 环境变量 {ENV_LOCK_DIR})")
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        help=f"stderr 日志级别 {LOG_LEVELS}(默认 {DEFAULT_LOG_LEVEL}; 环境变量 {ENV_LOG_LEVEL})",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _pick(cli_value: Any, env: Mapping[str, str], env_key: str, default: Any) -> Any:
    """优先级: 命令行(非 None) > 环境变量(非 None) > 默认值."""
    if cli_value is not None:
        return cli_value
    if env_key in env and env[env_key] is not None:
        return env[env_key]
    return default


def load_config(
    argv: Optional[Sequence[str]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> ServerConfig:
    """解析命令行 + 环境变量, 返回 :class:`ServerConfig`; 非法配置抛 :class:`ConfigError`."""
    env = os.environ if environ is None else environ
    namespace = build_parser().parse_args(list(argv) if argv is not None else None)
    raw = vars(namespace)

    repos_raw = _pick(raw.get("repos"), env, ENV_REPOS, "")
    repos = tuple(parse_repos(repos_raw)) if repos_raw else ()

    roots_raw = _pick(raw.get("roots"), env, ENV_ROOTS, "")
    root_raw = _pick(raw.get("root"), env, ENV_ROOT, "")
    if roots_raw:
        roots = split_paths(roots_raw)
    elif root_raw:
        roots = [normalize_path(root_raw)]
    elif repos:
        # 只给 --repos 时, 每个 repo 路径自身即允许根(§14.1 实现说明)
        roots = list(dict.fromkeys(path for _, path in repos))
    else:
        roots = [normalize_path(os.getcwd())]
    if not roots:
        raise ConfigError("允许根不能为空")

    protected_raw = _pick(raw.get("protected_branches"), env, ENV_PROTECTED_BRANCHES, None)
    protected = DEFAULT_PROTECTED_BRANCHES if protected_raw is None else split_branches(protected_raw)

    log_level = str(_pick(raw.get("log_level"), env, ENV_LOG_LEVEL, DEFAULT_LOG_LEVEL)).strip().upper()
    if log_level not in LOG_LEVELS:
        raise ConfigError(f"--log-level 需要 {LOG_LEVELS} 之一, 收到: {log_level!r}")

    return ServerConfig(
        roots=tuple(roots),
        repos=repos,
        read_only=parse_bool(_pick(raw.get("read_only"), env, ENV_READ_ONLY, True), name="--read-only"),
        allow_remote=parse_bool(_pick(raw.get("allow_remote"), env, ENV_ALLOW_REMOTE, False), name="--allow-remote"),
        allow_destructive=parse_bool(
            _pick(raw.get("allow_destructive"), env, ENV_ALLOW_DESTRUCTIVE, False),
            name="--allow-destructive",
        ),
        protected_branches=protected,
        timeout_seconds=parse_positive_float(
            _pick(raw.get("timeout"), env, ENV_TIMEOUT, DEFAULT_TIMEOUT_SECONDS),
            name="--timeout",
        ),
        max_output_bytes=parse_positive_int(
            _pick(raw.get("max_output_bytes"), env, ENV_MAX_OUTPUT_BYTES, DEFAULT_MAX_OUTPUT_BYTES),
            name="--max-output-bytes",
        ),
        audit_log=str(_pick(raw.get("audit_log"), env, ENV_AUDIT_LOG, "")).strip(),
        lock_dir=str(_pick(raw.get("lock_dir"), env, ENV_LOCK_DIR, "")).strip(),
        log_level=log_level,
    )
