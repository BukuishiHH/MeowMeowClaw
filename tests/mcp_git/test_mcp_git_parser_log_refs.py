"""GM2 log / refs / remote 解析单测(§5.3/§6.4)."""

from __future__ import annotations

import pytest

from meowmeowclaw_mcp_git.errors import ParseError
from meowmeowclaw_mcp_git.parsers.log import parse_log, parse_show_meta
from meowmeowclaw_mcp_git.parsers.refs import (
    BRANCH_FORMAT,
    TAG_FORMAT,
    parse_branches,
    parse_remotes,
    parse_tags,
    parse_upstream_track,
    sanitize_url,
)

US = "\x1f"
RS = "\x1e"


class TestLogParser:
    def test_multiple_records_with_refs(self):
        raw = (
            f"aaa{US}aaaaaaa{US}Alice{US}2026-01-02T03:04:05+08:00{US}feat: one{US}HEAD -> main, tag: v1{RS}\n"
            f"bbb{US}bbbbbbb{US}Bob{US}2026-02-03T04:05:06+08:00{US}fix: two{US}{RS}\n"
        )

        commits = parse_log(raw)

        assert len(commits) == 2
        assert commits[0].to_dict() == {
            "hash": "aaa",
            "short_hash": "aaaaaaa",
            "author": "Alice",
            "author_date": "2026-01-02T03:04:05+08:00",
            "subject": "feat: one",
            "refs": ["HEAD -> main", "tag: v1"],
        }
        assert commits[1].refs == ()

    def test_empty_subject_is_preserved(self):
        commits = parse_log(f"aaa{US}a{US}Alice{US}2026-01-01T00:00:00+00:00{US}{US}{RS}")

        assert commits[0].subject == ""

    def test_empty_output(self):
        assert parse_log(b"") == []

    def test_truncated_tail_record_is_dropped(self):
        raw = f"aaa{US}a{US}Alice{US}2026-01-01T00:00:00+00:00{US}ok{US}{RS}" + f"bbb{US}b{US}Bob{US}2026"

        assert [c.hash for c in parse_log(raw)] == ["aaa"]

    def test_field_count_error(self):
        with pytest.raises(ParseError, match="字段数"):
            parse_log(f"aaa{US}b{US}c{RS}")

    def test_show_meta_requires_single_record(self):
        with pytest.raises(ParseError, match="恰好 1 条"):
            parse_show_meta("")
        with pytest.raises(ParseError, match="恰好 1 条"):
            parse_show_meta(f"a{US}a{US}A{US}d{US}s{US}{RS}b{US}b{US}B{US}d{US}s{US}{RS}")


class TestUpstreamTrack:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", (0, 0, False)),
            ("[ahead 1]", (1, 0, False)),
            ("[behind 2]", (0, 2, False)),
            ("[ahead 3, behind 4]", (3, 4, False)),
            ("[gone]", (0, 0, True)),
        ],
    )
    def test_formats(self, raw, expected):
        assert parse_upstream_track(raw) == expected

    def test_invalid_format(self):
        with pytest.raises(ParseError, match="upstream track"):
            parse_upstream_track("[weird]")


class TestBranchParser:
    def raw(self, *records: str) -> str:
        return "".join(record + RS for record in records)

    def test_fields(self):
        raw = self.raw(
            f"main{US}abc1234{US}abc1234567890{US}origin/main{US}[ahead 1, behind 2]{US}*{US}{US}fix: greeting",
            f"feature/x{US}def5678{US}def5678901234{US}{US}{US} {US}{US}feat: x",
        )

        branches = parse_branches(raw)

        assert branches[0].to_dict(verbose=True) == {
            "name": "main",
            "short_hash": "abc1234",
            "hash": "abc1234567890",
            "current": True,
            "upstream": "origin/main",
            "ahead": 1,
            "behind": 2,
            "upstream_gone": False,
            "symbolic": False,
            "subject": "fix: greeting",
        }
        assert branches[1].current is False
        assert branches[1].upstream is None

    def test_compact_dict_omits_tracking(self):
        raw = self.raw(f"main{US}abc{US}abc{US}{US}{US}*{US}{US}s")

        data = parse_branches(raw)[0].to_dict(verbose=False)

        assert set(data) == {"name", "short_hash", "hash", "current"}

    def test_gone_upstream(self):
        raw = self.raw(f"main{US}abc{US}abc{US}origin/main{US}[gone]{US}*{US}{US}s")

        branch = parse_branches(raw)[0]

        assert branch.upstream_gone is True

    def test_truncated_tail_dropped(self):
        raw = self.raw(f"main{US}abc{US}abc{US}{US}{US}*{US}{US}s") + "partial"

        assert [b.name for b in parse_branches(raw)] == ["main"]

    def test_format_constant_uses_unit_separators(self):
        assert "%(refname:short)" in BRANCH_FORMAT and US in BRANCH_FORMAT and RS in BRANCH_FORMAT


class TestTagParser:
    def test_fields(self):
        raw = f"v1{US}tag{US}abc1234{US}2026-01-01T00:00:00+08:00{US}release{RS}"

        tag = parse_tags(raw)[0]

        assert tag.to_dict() == {
            "name": "v1",
            "object_type": "tag",
            "short_hash": "abc1234",
            "created_at": "2026-01-01T00:00:00+08:00",
            "subject": "release",
        }

    def test_field_count_error(self):
        with pytest.raises(ParseError, match="tag 记录字段数"):
            parse_tags(f"v1{US}tag{RS}")

    def test_format_constant(self):
        assert TAG_FORMAT.endswith(RS)


class TestRemoteParser:
    def test_parses_and_masks_urls(self):
        raw = (
            "origin\thttps://user:token@example.com/repo.git (fetch)\n"
            "origin\thttps://user:token@example.com/repo.git (push)\n"
            "mirror\tgit@example.com:mirror.git (fetch)\n"
        )

        remotes = parse_remotes(raw)

        assert [r.name for r in remotes] == ["origin", "mirror"]
        assert remotes[0].to_dict() == {
            "name": "origin",
            "fetch_urls": ["https://***@example.com/repo.git"],
            "push_urls": ["https://***@example.com/repo.git"],
        }
        assert remotes[1].fetch_urls == ["git@example.com:mirror.git"]

    def test_empty_output(self):
        assert parse_remotes("") == []

    def test_malformed_line(self):
        with pytest.raises(ParseError, match="remote 行"):
            parse_remotes("origin\thttps://example.com/repo.git\n")

    def test_duplicate_urls_deduplicated(self):
        raw = "origin\thttps://a.example/x (fetch)\norigin\thttps://a.example/x (fetch)\n"

        assert parse_remotes(raw)[0].fetch_urls == ["https://a.example/x"]


class TestSanitizeUrl:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("https://user:token@example.com/repo.git", "https://***@example.com/repo.git"),
            ("ssh://git@example.com/repo.git", "ssh://***@example.com/repo.git"),
            ("https://example.com/repo.git", "https://example.com/repo.git"),
            ("git@example.com:mirror.git", "git@example.com:mirror.git"),
            ("https://example.com/repo.git?token=abc123&x=1", "https://example.com/repo.git?token=***&x=1"),
            ("https://example.com/repo.git?private_token=abc", "https://example.com/repo.git?private_token=***"),
        ],
    )
    def test_masking(self, raw, expected):
        assert sanitize_url(raw) == expected

    def test_credentials_in_scp_like_path_left_to_caller(self):
        # scp 形式只有用户名, 不视为 secret
        assert sanitize_url("user@host:path/repo.git") == "user@host:path/repo.git"
