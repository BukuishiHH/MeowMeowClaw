"""GM2 grep/blame 解析与输出预算单测(§4.2/§5.3/§7.3)."""

from __future__ import annotations

import pytest

from meowmeowclaw_mcp_git.budget import (
    count_lines,
    fit_items,
    fit_text,
    utf8_size,
)
from meowmeowclaw_mcp_git.errors import ParseError
from meowmeowclaw_mcp_git.parsers.blame import parse_blame
from meowmeowclaw_mcp_git.parsers.grep import parse_grep


class TestGrepParser:
    def test_matches_with_line_numbers(self):
        # -z 格式: <path>\0<lineno>\0<content>\n(内容后仍是换行, 不是 NUL)
        raw = b"src/a.py\x003\x00print('hello')\ndocs/b.md\x001\x00hello doc\n"

        matches = parse_grep(raw)

        assert [m.to_dict() for m in matches] == [
            {"path": "src/a.py", "line": 3, "content": "print('hello')"},
            {"path": "docs/b.md", "line": 1, "content": "hello doc"},
        ]

    def test_empty_output(self):
        assert parse_grep(b"") == []
        assert parse_grep("") == []

    def test_truncated_tail_dropped(self):
        raw = b"src/a.py\x003\x00hello\nsrc/b.py\x007\x00partial"

        assert [m.path for m in parse_grep(raw)] == ["src/a.py"]

    def test_invalid_line_number(self):
        with pytest.raises(ParseError, match="行号"):
            parse_grep(b"src/a.py\x00x\x00hello\n")

    def test_malformed_line(self):
        with pytest.raises(ParseError, match="匹配行"):
            parse_grep(b"only-one-field\n")


class TestBlameParser:
    RAW = (
        "aaaa1111bbbb2222cccc3333dddd4444eeee5555 1 1 2\n"
        "author Alice\n"
        "author-mail <alice@example.com>\n"
        "author-time 1700000000\n"
        "author-tz +0800\n"
        "summary first\n"
        "boundary\n"
        "filename app.py\n"
        "\tdef main():\n"
        "aaaa1111bbbb2222cccc3333dddd4444eeee5555 2 2\n"
        "author Bob\n"
        "author-time 1700000000\n"
        "author-tz -0530\n"
        "\t    return 1\n"
    )

    def test_entries(self):
        lines = parse_blame(self.RAW)

        assert [line.line for line in lines] == [1, 2]
        assert lines[0].hash == "aaaa1111bbbb2222cccc3333dddd4444eeee5555"
        assert lines[0].author == "Alice"
        assert lines[0].content == "def main():"
        assert lines[0].author_time == "2023-11-15T06:13:20+08:00"
        assert lines[1].author_time == "2023-11-14T16:43:20-05:30"

    def test_empty_output(self):
        assert parse_blame("") == []

    def test_truncated_entry_dropped_when_flagged(self):
        raw = self.RAW + "bbbb1234 3 3\nauthor Carol\n"

        assert len(parse_blame(raw, truncated=True)) == 2

    def test_truncated_entry_raises_without_flag(self):
        raw = self.RAW + "bbbb1234 3 3\nauthor Carol\n"

        with pytest.raises(ParseError, match="截断"):
            parse_blame(raw)

    def test_content_without_header_rejected(self):
        with pytest.raises(ParseError, match="缺少头部"):
            parse_blame("\torphan content\n")

    def test_short_hash_header_ignored_as_metadata(self):
        # 少于 7 位 hex 的伪头部不构成新条目, 不会破坏解析
        raw = "aaaa1111bbbb2222cccc3333dddd4444eeee5555 1 1\nauthor A\nauthor-time 1700000000\nauthor-tz +0000\n\tx\n"
        assert len(parse_blame(raw)) == 1

    def test_bad_timestamp(self):
        raw = "aaaa1234 1 1\nauthor A\nauthor-time not-a-number\nauthor-tz +0000\n\tx\n"

        with pytest.raises(ParseError, match="时间戳"):
            parse_blame(raw)


class TestBudget:
    def test_count_lines(self):
        assert count_lines("") == 0
        assert count_lines("a") == 1
        assert count_lines("a\nb\n") == 2

    def test_fit_text_under_budget(self):
        fitted = fit_text("hello\nworld\n", max_bytes=100)

        assert fitted.truncated is False
        assert fitted.text == "hello\nworld\n"
        assert fitted.total_bytes == utf8_size("hello\nworld\n")
        assert fitted.returned_bytes == fitted.total_bytes

    def test_fit_text_by_lines(self):
        fitted = fit_text("a\nb\nc\n", max_bytes=100, max_lines=2)

        assert fitted.text == "a\nb\n"
        assert fitted.truncated is True
        assert fitted.total_lines == 3
        assert fitted.returned_lines == 2

    def test_fit_text_by_bytes_keeps_utf8_boundary(self):
        fitted = fit_text("中文内容" * 10, max_bytes=10)

        assert fitted.truncated is True
        assert fitted.returned_bytes <= 10
        assert "�" not in fitted.text  # 丢弃半个多字节字符, 不产生替换符

    def test_fit_text_zero_lines(self):
        fitted = fit_text("a\nb\n", max_bytes=100, max_lines=0)

        assert fitted.text == ""
        assert fitted.truncated is True

    def test_fit_items_by_bytes(self):
        items = [{"path": "a", "line": 1, "content": "x"}] * 5

        kept, truncated, total = fit_items(items, max_bytes=60)

        assert 0 < len(kept) < len(items)
        assert truncated is True
        assert total > 60

    def test_fit_items_by_count(self):
        items = [{"x": 1}] * 5

        kept, truncated, _total = fit_items(items, max_bytes=10_000, max_items=2)

        assert len(kept) == 2
        assert truncated is True

    def test_fit_items_not_truncated(self):
        items = [{"x": 1}]

        kept, truncated, _total = fit_items(items, max_bytes=10_000, max_items=10)

        assert kept == items
        assert truncated is False

    def test_invalid_limits(self):
        with pytest.raises(ValueError):
            fit_text("x", max_bytes=0)
        with pytest.raises(ValueError):
            fit_items([], max_bytes=0)
