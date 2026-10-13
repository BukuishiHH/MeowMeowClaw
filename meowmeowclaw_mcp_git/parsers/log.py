"""提交历史解析(§5.3)。

格式由 :data:`LOG_FORMAT` 定义: 字段用 ``0x1f``(US) 分隔、记录用 ``0x1e``(RS) 结尾。
选 US/RS 而不是 NUL 双分隔, 是为了让空 subject 也保持记录边界无歧义(§14.1 GM2 决策)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

from ..errors import ParseError

FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"

# %H 全 hash / %h 短 hash / %an 作者 / %aI 作者时间(ISO) / %s subject / %D refs
LOG_FORMAT = f"%H{FIELD_SEP}%h{FIELD_SEP}%an{FIELD_SEP}%aI{FIELD_SEP}%s{FIELD_SEP}%D{RECORD_SEP}"


@dataclass(frozen=True)
class CommitInfo:
    """一条提交(``git_log`` / ``git_search_history`` / ``git_show`` 共用)."""

    hash: str
    short_hash: str
    author: str
    author_date: str
    subject: str
    refs: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hash": self.hash,
            "short_hash": self.short_hash,
            "author": self.author,
            "author_date": self.author_date,
            "subject": self.subject,
            "refs": list(self.refs),
        }


def parse_log(raw: Union[bytes, str]) -> list[CommitInfo]:
    """解析 ``git log --pretty=format:<LOG_FORMAT>``; 截断的尾部记录会被丢弃."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    records = text.split(RECORD_SEP)
    if records and not text.endswith(RECORD_SEP):
        records.pop()  # 输出被截断, 最后一条不完整
    commits: list[CommitInfo] = []
    for record in records:
        record = record.strip("\r\n")
        if not record:
            continue
        fields = record.split(FIELD_SEP)
        if len(fields) < 5:
            raise ParseError(f"log 记录字段数不足({len(fields)}): {record[:120]!r}")
        refs_raw = fields[5] if len(fields) > 5 else ""
        commits.append(
            CommitInfo(
                hash=fields[0],
                short_hash=fields[1],
                author=fields[2],
                author_date=fields[3],
                subject=fields[4],
                refs=tuple(part.strip() for part in refs_raw.split(",") if part.strip()),
            )
        )
    return commits


def parse_show_meta(raw: Union[bytes, str]) -> CommitInfo:
    """解析 ``git show -s --format:<LOG_FORMAT> <ref>^{commit}`` 的单条记录."""
    commits = parse_log(raw)
    if len(commits) != 1:
        raise ParseError(f"show 元信息应恰好 1 条记录, 实际 {len(commits)} 条")
    return commits[0]
