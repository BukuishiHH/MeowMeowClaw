"""``git status --porcelain=v2 --branch`` 结构化解析(§5.3)。

输入同时兼容两种形态:

- **NUL 分隔**(``-z``, 生产路径): 文件名可含空格/换行/引号, 不解码转义;
- **行分隔**(单测 fixture / 人工调试): 以 TAB 分隔 rename 的 path/origPath,
  路径按 git 的 C-style 引号规则解码(``\\t``、``\\NNN`` 等)。

未知行会抛 :class:`ParseError`(§5.3: 不做静默丢数据)。输出被截断时, 尾部不完整
token 会被丢弃(完整输出一定以 NUL/换行结尾), 保证解析不会把半个文件名当数据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from ..errors import ParseError

_HEADER_PREFIX = "# "
_BRANCH_AB_RE = re.compile(r"^\+(\d+)\s+-(\d+)$")

# ordinary: 1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>            → 9 段
_ORDINARY_FIELDS = 9
# rename/copy: 2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <X><score> <path> → 10 段
_RENAME_FIELDS = 10
# unmerged: u <XY> <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path>    → 11 段
_UNMERGED_FIELDS = 11


@dataclass(frozen=True)
class RenameEntry:
    """rename/copy 条目(§4.2 只返回 path 列表, 重命名额外保留原路径)."""

    path: str
    orig_path: str
    index_status: str
    score: str

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "orig_path": self.orig_path,
            "index_status": self.index_status,
            "score": self.score,
        }


@dataclass
class StatusSummary:
    """``git_status`` 的结构化结果(branch/ahead/behind + 四组路径)."""

    branch: Optional[str] = None
    head_oid: Optional[str] = None
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    detached: bool = False
    unborn: bool = False
    staged: list[str] = field(default_factory=list)
    unstaged: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    renames: list[RenameEntry] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.staged or self.unstaged or self.untracked or self.conflicts or self.renames)

    @property
    def dirty_paths(self) -> tuple[str, ...]:
        """去重后的 dirty 路径(staged/unstaged/untracked/conflicts 的并集)."""
        merged: dict[str, None] = {}
        for path in (*self.staged, *self.unstaged, *self.untracked, *self.conflicts):
            merged.setdefault(path, None)
        return tuple(merged)

    @property
    def dirty_count(self) -> int:
        return len(self.dirty_paths)

    def to_state_dict(self) -> dict[str, Any]:
        """写工具 before/after 摘要(§4.2: 分支/HEAD/dirty 文件数)."""
        return {
            "branch": self.branch,
            "head_oid": self.head_oid,
            "detached": self.detached,
            "unborn": self.unborn,
            "dirty_files": self.dirty_count,
            "staged": len(self.staged),
            "unstaged": len(self.unstaged),
            "untracked": len(self.untracked),
            "conflicts": len(self.conflicts),
        }

    def counts(self) -> dict[str, int]:
        return {
            "staged": len(self.staged),
            "unstaged": len(self.unstaged),
            "untracked": len(self.untracked),
            "conflicts": len(self.conflicts),
            "renames": len(self.renames),
        }

    def to_data(self) -> dict[str, Any]:
        """完整结构化数据(§4.2 示例字段 + head/upstream/detached/renames)."""
        return {
            "branch": self.branch,
            "head_oid": self.head_oid,
            "upstream": self.upstream,
            "ahead": self.ahead,
            "behind": self.behind,
            "detached": self.detached,
            "unborn": self.unborn,
            "clean": self.clean,
            "staged": list(self.staged),
            "unstaged": list(self.unstaged),
            "untracked": list(self.untracked),
            "conflicts": list(self.conflicts),
            "renames": [entry.to_dict() for entry in self.renames],
        }

    def to_short_data(self) -> dict[str, Any]:
        """摘要模式: 省略逐文件列表, 只给分支与各组计数(上下文预算, G3)."""
        return {
            "branch": self.branch,
            "head_oid": self.head_oid,
            "upstream": self.upstream,
            "ahead": self.ahead,
            "behind": self.behind,
            "detached": self.detached,
            "unborn": self.unborn,
            "clean": self.clean,
            "counts": self.counts(),
        }


def parse_porcelain_v2(raw: Union[bytes, str]) -> StatusSummary:
    """解析 porcelain v2 输出; 任何结构不识别时抛 :class:`ParseError`."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    if not text:
        raise ParseError("git status 输出为空")
    nul_mode = "\0" in text
    tokens = _tokenize(text, nul_mode=nul_mode)

    summary = StatusSummary()
    header_seen = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        kind = token[0]
        if token.startswith(_HEADER_PREFIX):
            header_seen = True
            _parse_header(token[len(_HEADER_PREFIX) :], summary)
        elif kind == "1":
            fields = token.split(" ", _ORDINARY_FIELDS - 1)
            _require_fields(fields, _ORDINARY_FIELDS, token)
            xy, path = fields[1], _unquote(fields[8])
            _add_entry(summary, xy, path)
        elif kind == "2":
            fields = token.split(" ", _RENAME_FIELDS - 1)
            _require_fields(fields, _RENAME_FIELDS, token)
            xy, score = fields[1], fields[8]
            if nul_mode:
                if index >= len(tokens):
                    raise ParseError(f"rename 条目缺少原路径: {token!r}")
                path = fields[9]
                orig_path = tokens[index]
                index += 1
            else:
                path_field, sep, orig_field = fields[9].partition("\t")
                if not sep:
                    raise ParseError(f"rename 条目缺少 TAB 分隔的原路径: {token!r}")
                path, orig_path = _unquote(path_field), _unquote(orig_field)
            summary.renames.append(
                RenameEntry(path=path, orig_path=orig_path, index_status=xy[0], score=score)
            )
            _add_entry(summary, xy, path)
        elif kind == "u":
            fields = token.split(" ", _UNMERGED_FIELDS - 1)
            _require_fields(fields, _UNMERGED_FIELDS, token)
            summary.conflicts.append(_unquote(fields[10]))
        elif kind == "?":
            summary.untracked.append(_unquote(token[2:]))
        elif kind == "!":
            continue  # 未传 --ignored, 忽略该分支仅作兼容
        else:
            raise ParseError(f"无法识别的 porcelain v2 行: {token[:120]!r}")

    if not header_seen:
        raise ParseError("git status 输出缺少 --branch 头部(branch.oid/branch.head)")
    return summary


def _tokenize(text: str, *, nul_mode: bool) -> list[str]:
    if nul_mode:
        tokens = text.split("\0")
        # 截断/异常情况下最后一段不完整, 直接丢弃; 完整 -z 输出必以 NUL 结尾
        if tokens and tokens[-1] != "":
            tokens.pop()
        return tokens
    lines = text.split("\n")
    if lines and lines[-1] != "":
        lines.pop()
    return lines


def _parse_header(payload: str, summary: StatusSummary) -> None:
    name, _, value = payload.partition(" ")
    if name == "branch.oid":
        if value == "(initial)":
            summary.unborn = True
        else:
            summary.head_oid = value or None
    elif name == "branch.head":
        if value == "(detached)":
            summary.detached = True
        else:
            summary.branch = value or None
    elif name == "branch.upstream":
        summary.upstream = value or None
    elif name == "branch.ab":
        matched = _BRANCH_AB_RE.match(value)
        if matched is None:
            raise ParseError(f"无法解析 branch.ab: {value!r}")
        summary.ahead, summary.behind = int(matched.group(1)), int(matched.group(2))
    # 其余头部(git 版本新增)按前向兼容忽略


def _add_entry(summary: StatusSummary, xy: str, path: str) -> None:
    if len(xy) < 2:
        raise ParseError(f"porcelain v2 XY 字段非法: {xy!r}")
    index_status, worktree_status = xy[0], xy[1]
    if index_status != ".":
        summary.staged.append(path)
    if worktree_status != ".":
        summary.unstaged.append(path)


def _require_fields(fields: list[str], expected: int, token: str) -> None:
    if len(fields) != expected:
        raise ParseError(f"porcelain v2 字段数应为 {expected}, 实际 {len(fields)}: {token[:120]!r}")


def _unquote(value: str) -> str:
    """解码 git C-style 引号路径(仅行分隔模式会出现)."""
    if len(value) < 2 or not value.startswith('"') or not value.endswith('"'):
        return value
    body = value[1:-1]
    out = bytearray()
    index = 0
    simple = {'"': 0x22, "\\": 0x5C, "n": 0x0A, "t": 0x09, "r": 0x0D, "a": 0x07, "b": 0x08, "f": 0x0C, "v": 0x0B}
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.extend(char.encode("utf-8"))
            index += 1
            continue
        if index + 1 >= len(body):
            raise ParseError(f"路径转义不完整: {value!r}")
        escaped = body[index + 1]
        if escaped in simple:
            out.append(simple[escaped])
            index += 2
        elif escaped.isdigit():
            octal = body[index + 1 : index + 4]
            if len(octal) != 3 or any(digit not in "01234567" for digit in octal):
                raise ParseError(f"路径八进制转义非法: {value!r}")
            out.append(int(octal, 8) & 0xFF)
            index += 4
        else:
            raise ParseError(f"路径转义非法: \\{escaped} in {value!r}")
    return bytes(out).decode("utf-8", errors="replace")
