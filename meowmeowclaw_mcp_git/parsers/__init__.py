"""git 输出的结构化解析器(§5.3): status / diff / log / refs / grep / blame。

每个解析器都遵循同一条约定: **输出被截断时丢弃尾部不完整记录, 不静默产生半条数据**;
无法识别的格式抛 :class:`~meowmeowclaw_mcp_git.errors.ParseError`(错误码 ``parse_failed``)。
"""

from .blame import BlameLine, parse_blame
from .diff import FileStat, parse_numstat
from .grep import GrepMatch, parse_grep
from .log import FIELD_SEP, LOG_FORMAT, RECORD_SEP, CommitInfo, parse_log, parse_show_meta
from .refs import (
    BRANCH_FORMAT,
    TAG_FORMAT,
    BranchInfo,
    RemoteInfo,
    TagInfo,
    parse_branches,
    parse_remotes,
    parse_tags,
    parse_upstream_track,
    sanitize_url,
)
from .status import RenameEntry, StatusSummary, parse_porcelain_v2

__all__ = [
    "BRANCH_FORMAT",
    "BlameLine",
    "BranchInfo",
    "CommitInfo",
    "FIELD_SEP",
    "FileStat",
    "GrepMatch",
    "LOG_FORMAT",
    "RECORD_SEP",
    "RemoteInfo",
    "RenameEntry",
    "StatusSummary",
    "TAG_FORMAT",
    "TagInfo",
    "parse_blame",
    "parse_branches",
    "parse_grep",
    "parse_log",
    "parse_numstat",
    "parse_porcelain_v2",
    "parse_remotes",
    "parse_show_meta",
    "parse_tags",
    "parse_upstream_track",
    "sanitize_url",
]
