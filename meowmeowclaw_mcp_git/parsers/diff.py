"""diff 统计解析(§5.3): ``git diff --numstat -z`` / ``git show --numstat -z``。

``-z`` 输出格式(git ≥2.30):

- 普通文件: ``<added>\\t<deleted>\\t<path>\\0``
- 重命名:   ``<added>\\t<deleted>\\t\\0<old>\\0<new>\\0``
- 二进制:   ``-\\t-\\t<path>\\0``

行分隔模式仅用于单测 fixture(不支持 rename, git 在非 ``-z`` 下用 ``{old => new}``
花括号语法, 不在此解析)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Union

from ..errors import ParseError


@dataclass(frozen=True)
class FileStat:
    """单个文件的增删统计(二进制文件 added/deleted 为 None)."""

    path: str
    added: Optional[int]
    deleted: Optional[int]
    binary: bool = False
    orig_path: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "path": self.path,
            "added": self.added,
            "deleted": self.deleted,
            "binary": self.binary,
        }
        if self.orig_path is not None:
            data["orig_path"] = self.orig_path
        return data


def parse_numstat(raw: Union[bytes, str]) -> list[FileStat]:
    """解析 numstat 输出; 空输出(无差异)返回空列表."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    if not text.strip("\0\r\n"):
        return []
    if "\0" in text:
        return _parse_nul(text)
    return _parse_lines(text)


def _parse_nul(text: str) -> list[FileStat]:
    tokens = text.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()  # -z 正常终止符
    elif tokens:
        tokens.pop()  # 截断的不完整尾段, 丢弃
    files: list[FileStat] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        fields = token.split("\t", 2)
        if len(fields) != 3:
            raise ParseError(f"numstat 字段数应为 3, 实际 {len(fields)}: {token[:120]!r}")
        added, deleted, path = fields
        if path == "":
            if index + 1 >= len(tokens):
                raise ParseError("numstat 重命名条目缺少 old/new 路径")
            orig_path, path = tokens[index], tokens[index + 1]
            index += 2
            files.append(FileStat(path=path, added=_parse_count(added), deleted=_parse_count(deleted), orig_path=orig_path))
        else:
            files.append(FileStat(path=path, added=_parse_count(added), deleted=_parse_count(deleted), binary=added == "-"))
    return files


def _parse_lines(text: str) -> list[FileStat]:
    files: list[FileStat] = []
    for line in text.splitlines():
        if not line:
            continue
        fields = line.split("\t", 2)
        if len(fields) != 3 or fields[2] == "":
            raise ParseError(f"numstat 行无法解析(rename 需要 -z 模式): {line[:120]!r}")
        added, deleted, path = fields
        files.append(FileStat(path=path, added=_parse_count(added), deleted=_parse_count(deleted), binary=added == "-"))
    return files


def _parse_count(value: str) -> Optional[int]:
    if value == "-":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ParseError(f"numstat 计数非法: {value!r}") from exc
