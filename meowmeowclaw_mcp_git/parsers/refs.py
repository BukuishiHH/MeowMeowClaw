"""分支/tag/remote 解析(§5.3) + URL 脱敏(§6.4)。

- 分支: ``git for-each-ref`` 自定义格式, 字段 ``%1f``(US)、记录 ``%1e``(RS);
- tag:  ``git tag --format=...``;
- remote: ``git remote -v`` 文本行(``<name>\\t<url> (fetch|push)``)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from ..errors import ParseError

FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"

# %(refname:short) %(objectname:short) %(objectname) %(upstream:short) %(upstream:track) %(HEAD) %(symref) %(contents:subject)
BRANCH_FORMAT = (
    f"%(refname:short){FIELD_SEP}%(objectname:short){FIELD_SEP}%(objectname){FIELD_SEP}"
    f"%(upstream:short){FIELD_SEP}%(upstream:track){FIELD_SEP}%(HEAD){FIELD_SEP}%(symref){FIELD_SEP}"
    f"%(contents:subject){RECORD_SEP}"
)
# %(refname:short) %(objecttype) %(objectname:short) %(creatordate:iso-strict) %(contents:subject)
TAG_FORMAT = (
    f"%(refname:short){FIELD_SEP}%(objecttype){FIELD_SEP}%(objectname:short){FIELD_SEP}"
    f"%(creatordate:iso-strict){FIELD_SEP}%(contents:subject){RECORD_SEP}"
)

_AHEAD_RE = re.compile(r"^\[ahead (\d+)(?:, behind (\d+))?\]$")
_BEHIND_RE = re.compile(r"^\[behind (\d+)\]$")

# URL 脱敏: scheme://userinfo@host -> scheme://***@host; 查询串里的 token/password 等同样打码
_URL_USERINFO_RE = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<userinfo>[^/@]+)@")
_SENSITIVE_QUERY_RE = re.compile(
    r"(?P<prefix>[?&](?:access_token|private_token|token|password|passwd|authkey|apikey|key)=)[^&#\s]*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BranchInfo:
    name: str
    short_hash: str
    hash: str
    current: bool = False
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    upstream_gone: bool = False
    symbolic: bool = False
    subject: str = ""

    def to_dict(self, *, verbose: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "short_hash": self.short_hash,
            "hash": self.hash,
            "current": self.current,
        }
        if verbose:
            data.update(
                {
                    "upstream": self.upstream,
                    "ahead": self.ahead,
                    "behind": self.behind,
                    "upstream_gone": self.upstream_gone,
                    "symbolic": self.symbolic,
                    "subject": self.subject,
                }
            )
        return data


@dataclass(frozen=True)
class TagInfo:
    name: str
    object_type: str
    short_hash: str
    created_at: str
    subject: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "object_type": self.object_type,
            "short_hash": self.short_hash,
            "created_at": self.created_at,
            "subject": self.subject,
        }


@dataclass
class RemoteInfo:
    name: str
    fetch_urls: list[str] = field(default_factory=list)
    push_urls: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "fetch_urls": list(self.fetch_urls), "push_urls": list(self.push_urls)}


def parse_branches(raw: Union[bytes, str]) -> list[BranchInfo]:
    """解析 for-each-ref 输出."""
    branches: list[BranchInfo] = []
    for fields in _iter_records(raw, expected=8, what="branch"):
        ahead, behind, gone = parse_upstream_track(fields[4])
        branches.append(
            BranchInfo(
                name=fields[0],
                short_hash=fields[1],
                hash=fields[2],
                upstream=fields[3] or None,
                ahead=ahead,
                behind=behind,
                upstream_gone=gone,
                current=fields[5] == "*",
                symbolic=bool(fields[6]),
                subject=fields[7],
            )
        )
    return branches


def parse_tags(raw: Union[bytes, str]) -> list[TagInfo]:
    """解析 ``git tag --format=...`` 输出."""
    tags: list[TagInfo] = []
    for fields in _iter_records(raw, expected=5, what="tag"):
        tags.append(
            TagInfo(
                name=fields[0],
                object_type=fields[1],
                short_hash=fields[2],
                created_at=fields[3],
                subject=fields[4],
            )
        )
    return tags


def parse_remotes(raw: Union[bytes, str]) -> list[RemoteInfo]:
    """解析 ``git remote -v``; URL 已脱敏(§6.4)."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    remotes: dict[str, RemoteInfo] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        name, sep, rest = line.partition("\t")
        if not sep:
            name, _, rest = line.partition(" ")
        url, _, kind = rest.rpartition(" (")
        kind = kind.rstrip(")")
        if not name or not url or kind not in ("fetch", "push"):
            raise ParseError(f"remote 行无法解析: {line[:120]!r}")
        entry = remotes.setdefault(name, RemoteInfo(name=name))
        target = entry.fetch_urls if kind == "fetch" else entry.push_urls
        masked = sanitize_url(url.strip())
        if masked not in target:
            target.append(masked)
    return list(remotes.values())


def parse_upstream_track(raw: str) -> tuple[int, int, bool]:
    """解析 ``%(upstream:track)``: ``[ahead 1, behind 2]`` / ``[behind 2]`` / ``[gone]`` / 空."""
    value = raw.strip()
    if not value:
        return 0, 0, False
    if value == "[gone]":
        return 0, 0, True
    matched = _AHEAD_RE.match(value)
    if matched:
        return int(matched.group(1)), int(matched.group(2) or 0), False
    matched = _BEHIND_RE.match(value)
    if matched:
        return 0, int(matched.group(1)), False
    raise ParseError(f"无法解析 upstream track: {raw!r}")


def sanitize_url(url: str) -> str:
    """去掉 URL userinfo, 并对 token/password 查询参数打码(§6.4)."""
    sanitized = _URL_USERINFO_RE.sub(r"\g<scheme>***@", url)
    return _SENSITIVE_QUERY_RE.sub(r"\g<prefix>***", sanitized)


def _iter_records(raw: Union[bytes, str], *, expected: int, what: str) -> list[list[str]]:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    records = text.split(RECORD_SEP)
    if records and not text.endswith(RECORD_SEP):
        records.pop()  # 截断的尾段
    result: list[list[str]] = []
    for record in records:
        record = record.strip("\r\n")
        if not record:
            continue
        fields = record.split(FIELD_SEP)
        if len(fields) != expected:
            raise ParseError(f"{what} 记录字段数应为 {expected}, 实际 {len(fields)}: {record[:120]!r}")
        result.append(fields)
    return result
