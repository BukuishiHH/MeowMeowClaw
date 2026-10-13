"""``git blame --line-porcelain`` 解析(§5.3)。

porcelain 分组格式::

    <sha> <orig-line> <final-line> [<num-lines>]
    author <name>
    author-time <unix-ts>
    author-tz <+0800>
    ...
    \\t<line content>

``--line-porcelain`` 每行都会重复完整元数据; 其他键(committer-*/summary/filename/
previous/boundary)忽略。输出被截断时丢弃不完整条目。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Union

from ..errors import ParseError

_HEADER_RE = re.compile(r"^(?P<hash>[0-9a-fA-F]{7,64}) (?P<orig>\d+) (?P<final>\d+)(?: (?P<num>\d+))?$")


@dataclass(frozen=True)
class BlameLine:
    line: int
    hash: str
    author: str
    author_time: str
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "hash": self.hash,
            "author": self.author,
            "author_time": self.author_time,
            "content": self.content,
        }


def parse_blame(raw: Union[bytes, str], *, truncated: bool = False) -> list[BlameLine]:
    """解析 blame 输出; ``truncated=True`` 时丢弃尾部不完整条目, 否则报 ParseError."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    entries: list[BlameLine] = []
    current: dict[str, Any] = {}
    for line in text.split("\n"):
        if line.startswith("\t"):
            if not current:
                raise ParseError("blame 内容行缺少头部")
            entries.append(
                BlameLine(
                    line=current["line"],
                    hash=current["hash"],
                    author=current.get("author", ""),
                    author_time=_format_time(current.get("author_ts", "0"), current.get("author_tz", "+0000")),
                    content=line[1:],
                )
            )
            current = {}
            continue
        matched = _HEADER_RE.match(line)
        if matched:
            current = {
                "hash": matched.group("hash"),
                "line": int(matched.group("final")),
            }
            continue
        if not current:
            continue
        key, _, value = line.partition(" ")
        if key == "author":
            current["author"] = value
        elif key == "author-time":
            current["author_ts"] = value
        elif key == "author-tz":
            current["author_tz"] = value
    if current and not truncated:
        raise ParseError("blame 输出在条目中间结束(可能被截断)")
    return entries


def _format_time(timestamp: str, tz: str) -> str:
    try:
        seconds = int(timestamp)
    except ValueError as exc:
        raise ParseError(f"blame 时间戳非法: {timestamp!r}") from exc
    try:
        offset = _parse_tz(tz)
    except (ValueError, IndexError):
        offset = timezone.utc
    return datetime.fromtimestamp(seconds, tz=offset).isoformat()


def _parse_tz(tz: str) -> timezone:
    sign = -1 if tz.startswith("-") else 1
    hours = int(tz[1:3])
    minutes = int(tz[3:5]) if len(tz) >= 5 else 0
    return timezone(sign * timedelta(hours=hours, minutes=minutes))
