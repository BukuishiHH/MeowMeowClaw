"""``git grep -z --line-number`` 解析(§5.3)。

``-z`` 输出(记录以换行分隔, 字段以 NUL 分隔)::

    <path>\\0<lineno>\\0<content>\\n

输出被截断时尾行会被丢弃(不完整的行不能当匹配用)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Union

from ..errors import ParseError


@dataclass(frozen=True)
class GrepMatch:
    path: str
    line: int
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "content": self.content}


def parse_grep(raw: Union[bytes, str]) -> list[GrepMatch]:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    if text == "":
        return []
    lines = text.split("\n")
    if lines and lines[-1] != "":
        lines.pop()  # 截断的尾行
    matches: list[GrepMatch] = []
    for line in lines:
        if not line:
            continue
        fields = line.split("\0", 2)
        if len(fields) != 3:
            raise ParseError(f"grep 匹配行无法解析: {line[:120]!r}")
        path, lineno, content = fields
        try:
            number = int(lineno)
        except ValueError as exc:
            raise ParseError(f"grep 行号非法: {lineno!r}") from exc
        matches.append(GrepMatch(path=path, line=number, content=content))
    return matches
