"""GM1 status porcelain v2 解析单测(``meowmeowclaw_mcp_git/parsers/status.py`` §5.3)."""

from __future__ import annotations

import pytest

from meowmeowclaw_mcp_git.errors import ParseError
from meowmeowclaw_mcp_git.parsers.status import parse_porcelain_v2

HEADERS = "# branch.oid e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"

FULL_LINE_FIXTURE = (
    HEADERS
    + "# branch.head main\n"
    + "# branch.upstream origin/main\n"
    + "# branch.ab +2 -3\n"
    + "1 M. N... 100644 100644 100644 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb src/staged.py\n"
    + "1 .M N... 100644 100644 100644 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb src/unstaged.py\n"
    + "1 MM N... 100644 100644 100644 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb src/both.py\n"
    + "2 R. N... 100644 100644 100644 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb R100 src/new name.py\tsrc/old name.py\n"
    + "? notes.md\n"
    + "u UU N... 100644 100644 100644 100644 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb cccccccccccccccccccccccccccccccccccccccc src/conflict.py\n"
)


def nul(*tokens: str) -> bytes:
    return ("\0".join(tokens) + "\0").encode("utf-8")


class TestHeaders:
    def test_branch_upstream_and_ab(self):
        summary = parse_porcelain_v2(FULL_LINE_FIXTURE)

        assert summary.branch == "main"
        assert summary.upstream == "origin/main"
        assert summary.ahead == 2
        assert summary.behind == 3
        assert summary.detached is False
        assert summary.unborn is False
        assert summary.head_oid == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def test_unborn_repository(self):
        summary = parse_porcelain_v2("# branch.oid (initial)\n# branch.head main\n? first.txt\n")

        assert summary.unborn is True
        assert summary.branch == "main"
        assert summary.head_oid is None
        assert summary.untracked == ["first.txt"]

    def test_detached_head(self):
        summary = parse_porcelain_v2("# branch.oid abc123\n# branch.head (detached)\n")

        assert summary.detached is True
        assert summary.branch is None
        assert summary.head_oid == "abc123"

    def test_no_upstream_no_ab_header(self):
        summary = parse_porcelain_v2("# branch.oid abc\n# branch.head main\n")

        assert summary.upstream is None
        assert summary.ahead == 0
        assert summary.behind == 0


class TestEntries:
    def test_groups_are_split_by_xy(self):
        summary = parse_porcelain_v2(FULL_LINE_FIXTURE)

        assert summary.staged == ["src/staged.py", "src/both.py", "src/new name.py"]
        assert summary.unstaged == ["src/unstaged.py", "src/both.py"]
        assert summary.untracked == ["notes.md"]
        assert summary.conflicts == ["src/conflict.py"]

    def test_rename_keeps_original_path(self):
        summary = parse_porcelain_v2(FULL_LINE_FIXTURE)

        assert len(summary.renames) == 1
        rename = summary.renames[0]
        assert rename.path == "src/new name.py"
        assert rename.orig_path == "src/old name.py"
        assert rename.index_status == "R"
        assert rename.score == "R100"
        assert rename.to_dict()["orig_path"] == "src/old name.py"

    def test_clean_repo_has_no_entries(self):
        summary = parse_porcelain_v2("# branch.oid abc\n# branch.head main\n")

        assert summary.clean is True
        assert summary.counts() == {"staged": 0, "unstaged": 0, "untracked": 0, "conflicts": 0, "renames": 0}

    def test_dirty_repo_counts_and_clean_false(self):
        summary = parse_porcelain_v2(FULL_LINE_FIXTURE)

        assert summary.clean is False
        assert summary.counts() == {"staged": 3, "unstaged": 2, "untracked": 1, "conflicts": 1, "renames": 1}

    def test_data_and_short_data_shapes(self):
        summary = parse_porcelain_v2(FULL_LINE_FIXTURE)

        data = summary.to_data()
        assert data["branch"] == "main"
        assert data["staged"] == ["src/staged.py", "src/both.py", "src/new name.py"]
        assert data["renames"][0]["orig_path"] == "src/old name.py"
        short = summary.to_short_data()
        assert short["counts"]["staged"] == 3
        assert "staged" not in short and "unstaged" not in short


class TestNulMode:
    def test_paths_with_spaces_and_newlines(self):
        payload = nul(
            "# branch.oid abc",
            "# branch.head main",
            "1 M. N... 100644 100644 100644 h h src/with space.py",
            "1 .M N... 100644 100644 100644 h h src/with\nnewline.py",
            "? untracked file.txt",
        )

        summary = parse_porcelain_v2(payload)

        assert summary.staged == ["src/with space.py"]
        assert summary.unstaged == ["src/with\nnewline.py"]
        assert summary.untracked == ["untracked file.txt"]

    def test_rename_paths_use_next_nul_token(self):
        payload = nul(
            "# branch.oid abc",
            "# branch.head main",
            "2 R. N... 100644 100644 100644 h h R100 new name.py",
            "old name.py",
        )

        summary = parse_porcelain_v2(payload)

        assert summary.renames[0].path == "new name.py"
        assert summary.renames[0].orig_path == "old name.py"

    def test_truncated_tail_is_dropped(self):
        # 截断可能切在 token 中间: 不完整的尾段必须丢弃, 不能当文件名
        payload = b"# branch.oid abc\0# branch.head main\0? notes.md\0? partial-tail"

        summary = parse_porcelain_v2(payload)

        assert summary.untracked == ["notes.md"]

    def test_unmerged_and_untracked_in_nul_mode(self):
        payload = nul(
            "# branch.oid abc",
            "# branch.head main",
            "u UU N... 100644 100644 100644 100644 h1 h2 h3 src/conflict.py",
            "? new.txt",
        )

        summary = parse_porcelain_v2(payload)

        assert summary.conflicts == ["src/conflict.py"]
        assert summary.untracked == ["new.txt"]


class TestCQuotedPaths:
    def test_tab_and_quote_escapes(self):
        summary = parse_porcelain_v2(
            "# branch.oid abc\n# branch.head main\n"
            '1 .M N... 100644 100644 100644 h h "a\\tb\\"q.py"\n'
        )

        assert summary.unstaged == ['a\tb"q.py']

    def test_octal_utf8_escape(self):
        summary = parse_porcelain_v2(
            "# branch.oid abc\n# branch.head main\n"
            '1 .M N... 100644 100644 100644 h h "\\346\\226\\207\\344\\273\\266.txt"\n'
        )

        assert summary.unstaged == ["文件.txt"]


class TestMalformed:
    def test_empty_output(self):
        with pytest.raises(ParseError) as excinfo:
            parse_porcelain_v2("")

        assert excinfo.value.code == "parse_failed"

    def test_missing_branch_header(self):
        with pytest.raises(ParseError, match="缺少"):
            parse_porcelain_v2("? notes.md\n")

    def test_unknown_entry_kind(self):
        with pytest.raises(ParseError, match="无法识别"):
            parse_porcelain_v2("# branch.oid abc\n# branch.head main\nx strange\n")

    def test_bad_branch_ab(self):
        with pytest.raises(ParseError, match="branch.ab"):
            parse_porcelain_v2("# branch.oid abc\n# branch.head main\n# branch.ab +x -3\n")

    def test_wrong_field_count(self):
        with pytest.raises(ParseError, match="字段数"):
            parse_porcelain_v2("# branch.oid abc\n# branch.head main\n1 M. N...\n")

    def test_rename_without_original_path(self):
        with pytest.raises(ParseError):
            parse_porcelain_v2(
                "# branch.oid abc\n# branch.head main\n"
                "2 R. N... 100644 100644 100644 h h R100 new.py\n"
            )
