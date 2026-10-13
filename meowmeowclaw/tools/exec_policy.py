"""子代理 exec 安全策略与敏感路径策略(SA4, 见 docs/SUBAGENT_DESIGN.md §7).

- :func:`build_subagent_env`: 子代理 exec 最小环境(剔除密钥/代理变量, 隔离 HOME/TMPDIR);
- :class:`SubagentCommandPolicy`: ``guarded`` 拒绝后台/守护化, ``allowlist`` 仅允许配置前缀
  且拒绝 shell 组合符;
- :class:`SensitivePathPolicy`: 子代理 read/write/list 的敏感文件拒绝名单。
"""

import os
import re
import shlex
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable, Mapping, Optional

# 环境变量: 默认保留的基础键(其他变量必须显式 allowlist)
DEFAULT_ENV_ALLOWLIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
)
# 名称匹配这些模式的变量永远不进入子代理 exec 环境(即使显式 allowlist)
SECRET_ENV_PATTERN = re.compile(
    r"(API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PROXY)", re.IGNORECASE
)

# guarded 模式的额外拒绝: 后台/守护化会让命令逃逸变更锁的生命周期
BACKGROUND_DENY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bnohup\b", re.IGNORECASE), "nohup"),
    (re.compile(r"\bsetsid\b", re.IGNORECASE), "setsid"),
    (re.compile(r"\bdisown\b", re.IGNORECASE), "disown"),
    (re.compile(r"(?<![&])&\s*(?:$|[#;])"), "尾随 & 后台执行"),
)
# allowlist 模式禁止的 shell 组合符(包括重定向/命令替换/管道)
SHELL_CONTROL_CHARS = frozenset(";|&><`$\n\r")


def build_subagent_env(
    *,
    home_dir: str | os.PathLike,
    base_env: Optional[Mapping[str, str]] = None,
    allowlist: Iterable[str] = (),
    tmp_dir: Optional[str | os.PathLike] = None,
) -> dict[str, str]:
    """构造子代理 exec 的最小环境.

    - 仅保留 ``DEFAULT_ENV_ALLOWLIST`` + 显式 ``allowlist`` 中存在的变量;
    - 名称命中 ``SECRET_ENV_PATTERN`` 的变量一律剔除;
    - ``HOME`` / ``TMPDIR`` 指向每次 spawn 的隔离目录;
    - PATH 缺失时回退 ``os.defpath``。
    """
    source = os.environ if base_env is None else base_env
    allowed = {name for name in DEFAULT_ENV_ALLOWLIST}
    allowed.update(str(name) for name in allowlist if str(name).strip())

    env: dict[str, str] = {}
    for name in sorted(allowed):
        if SECRET_ENV_PATTERN.search(name):
            continue
        value = source.get(name)
        if value is None:
            continue
        env[name] = str(value)

    env.setdefault("PATH", os.defpath)
    env["HOME"] = str(Path(home_dir))
    env["TMPDIR"] = str(Path(tmp_dir) if tmp_dir is not None else Path(home_dir))
    return env


class SubagentCommandPolicy:
    """子代理 exec 命令策略(guarded / allowlist)."""

    def __init__(self, *, mode: str = "guarded", allowlist: Iterable[str] = ()) -> None:
        normalized = str(mode or "guarded").strip().lower()
        if normalized not in ("guarded", "allowlist"):
            raise ValueError(f"不支持的 exec 策略档位: {mode!r}")
        self.mode = normalized
        self.allowlist = tuple(
            str(entry).strip() for entry in allowlist if str(entry).strip()
        )

    def __repr__(self) -> str:
        return f"<SubagentCommandPolicy mode={self.mode!r} allowlist={list(self.allowlist)!r}>"

    def check(self, command: str) -> Optional[str]:
        """返回 None 表示允许; 否则返回拒绝原因(可读文本)."""
        text = str(command or "").strip()
        if not text:
            return "命令不能为空"

        if self.mode == "allowlist":
            return self._check_allowlist(text)

        for pattern, reason in BACKGROUND_DENY_PATTERNS:
            if pattern.search(text):
                return f"禁止后台/守护化命令({reason})"
        return None

    def _check_allowlist(self, command: str) -> Optional[str]:
        if not self.allowlist:
            return "allowlist 模式未配置任何命令前缀"
        if any(char in command for char in SHELL_CONTROL_CHARS):
            return "allowlist 模式禁止 shell 组合符(; | & > < ` $ 等)"
        try:
            shlex.split(command)
        except ValueError as exc:
            return f"命令解析失败: {exc}"

        for prefix in self.allowlist:
            if command == prefix or command.startswith(prefix + " "):
                return None
        return f"命令不在 allowlist 中: {command[:120]}"


class SensitivePathPolicy:
    """子代理敏感文件拒绝名单(相对 workspace 的 fnmatch 模式)."""

    def __init__(self, patterns: Iterable[str] = ()) -> None:
        self.patterns = tuple(
            str(pattern).strip().replace("\\", "/")
            for pattern in patterns
            if str(pattern).strip()
        )

    def __repr__(self) -> str:
        return f"<SensitivePathPolicy patterns={list(self.patterns)!r}>"

    def is_denied(self, absolute_path: str | os.PathLike, *, workspace: str | os.PathLike) -> bool:
        if not self.patterns:
            return False
        real_workspace = os.path.realpath(os.path.abspath(os.fspath(workspace)))
        real_path = os.path.realpath(os.path.abspath(os.fspath(absolute_path)))
        try:
            relative = os.path.relpath(real_path, real_workspace).replace(os.sep, "/")
        except ValueError:
            return False
        if relative.startswith("../") or relative == "..":
            return False
        basename = os.path.basename(relative)
        parts = relative.split("/")
        for pattern in self.patterns:
            if relative == pattern or fnmatch(relative, pattern):
                return True
            if fnmatch(basename, pattern):
                return True
            if "/" not in pattern and any(fnmatch(part, pattern) for part in parts):
                return True
        return False


__all__ = [
    "BACKGROUND_DENY_PATTERNS",
    "DEFAULT_ENV_ALLOWLIST",
    "SECRET_ENV_PATTERN",
    "SHELL_CONTROL_CHARS",
    "SensitivePathPolicy",
    "SubagentCommandPolicy",
    "build_subagent_env",
]
