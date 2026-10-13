"""GM2 numstat 解析单测(``meowmeowclaw_mcp_git/parsers/diff.py``, §5.3)."""

from __future__ import annotations

import pytest

from meowmeowclaw_mcp_git.errors import ParseError
from meowmeowclaw_mcp_git.parsers.diff import parse_numstat


class TestNulMode:
    def test_normal_entries(self):
        files = parse_numstat(b"3\t2\tsrc/app.py\x00-\t-\tassets/logo.png\x00")

        assert [(f.path, f.added, f.deleted, f.binary) for f in files] == [
            ("src/app.py", 3, 2, False),
            ("assets/logo.png", None, None, True),
        ]

    def test_rename_entry_keeps_original_path(self):
        files = parse_numstat(b"0\t0\t\x00src/old name.py\x00src/new name.py\x00")

        assert len(files) == 1
        rename = files[0]
        assert rename.path == "src/new name.py"
        assert rename.orig_path == "src/old name.py"
        assert rename.to_dict()["orig_path"] == "src/old name.py"

    def test_normal_entry_to_dict_has_no_orig_path(self):
        files = parse_numstat(b"1\t1\ta.py\x00")

        assert "orig_path" not in files[0].to_dict()

    def test_path_with_spaces_is_preserved(self):
        files = parse_numstat(b"1\t0\tsrc/with space.py\x00")

        assert files[0].path == "src/with space.py"

    def test_truncated_tail_is_dropped(self):
        files = parse_numstat(b"1\t0\ta.py\x00" + b"2\t0\tpartial")

        assert [f.path for f in files] == ["a.py"]

    def test_rename_with_missing_paths_raises_parse_error(self):
        with pytest.raises(ParseError, match="重命名"):
            parse_numstat(b"0\t0\t\x00old-only\x00")


class TestLineMode:
    def test_basic_lines(self):
        files = parse_numstat("4\t0\tapp.py\n-\t-\tbin.dat\n")

        assert [f.path for f in files] == ["app.py", "bin.dat"]
        assert files[0].added == 4 and files[0].deleted == 0
        assert files[1].binary is True


class TestInvalid:
    def test_empty_output_is_no_changes(self):
        assert parse_numstat("") == []
        assert parse_numstat(b"\x00") == []

    def test_wrong_field_count(self):
        with pytest.raises(ParseError, match="字段数"):
            parse_numstat(b"3\t2\x00")

    def test_non_numeric_count(self):
        with pytest.raises(ParseError, match="计数"):
            parse_numstat(b"x\t2\ta.py\x00")

    def test_rename_in_line_mode_rejected(self):
        with pytest.raises(ParseError):
            parse_numstat("0\t0\t\n")
