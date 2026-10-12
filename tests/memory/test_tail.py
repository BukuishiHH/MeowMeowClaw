"""A+ A2 尾读工具单元测试(``meowmeowclaw/memory/tail.py``).

对照 docs/A_PLUS_OPTIMIZATION.md §3.2 与 §4 测试策略:
空文件 / 仅 header / 无末尾换行 / 末尾损坏行 / 中间损坏行 / 空行 /
UTF-8 跨块边界 / 超大行 / max_lines / max_bytes, 并校验与全量读取的等价性。
"""

import json

import pytest

from meowmeowclaw.memory.tail import (
    BLOCK_SIZE,
    read_header,
    read_last_lines,
    read_last_valid_turn,
)


def write_lines(path, *lines: str) -> None:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def header(**extra) -> str:
    return json.dumps({"type": "header", "schema_version": 1, **extra}, ensure_ascii=False)


def turn(seq: int, text: str = "你好") -> str:
    return json.dumps(
        {
            "type": "turn",
            "schema_version": 1,
            "seq": seq,
            "message_total": seq,
            "ts_ms": 1_700_000_000_000 + seq,
            "messages": [{"role": "user", "content": text}],
        },
        ensure_ascii=False,
    )


class TestReadHeader:
    def test_reads_first_line(self, tmp_path):
        path = tmp_path / "a.jsonl"
        write_lines(path, header(storage_id="s1"), turn(1))

        record = read_header(path)

        assert record is not None and record["type"] == "header"
        assert record["storage_id"] == "s1"

    def test_empty_file_returns_none(self, tmp_path):
        path = tmp_path / "a.jsonl"
        path.write_bytes(b"")

        assert read_header(path) is None

    @pytest.mark.parametrize("first_line", ["这不是 JSON", "[1, 2, 3]", "   ", '{"a": '])
    def test_invalid_first_line_returns_none(self, tmp_path, first_line):
        path = tmp_path / "a.jsonl"
        path.write_text(first_line + "\n" + turn(1) + "\n", encoding="utf-8")

        assert read_header(path) is None

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(OSError):
            read_header(tmp_path / "missing.jsonl")


class TestReadLastLines:
    def test_empty_file(self, tmp_path):
        path = tmp_path / "a.jsonl"
        path.write_bytes(b"")

        assert read_last_lines(path) == []

    def test_returns_newest_first(self, tmp_path):
        path = tmp_path / "a.jsonl"
        write_lines(path, "l1", "l2", "l3")

        assert read_last_lines(path) == [b"l3", b"l2", b"l1"]

    def test_trailing_newline_does_not_create_empty_line(self, tmp_path):
        path = tmp_path / "a.jsonl"
        write_lines(path, "l1", "l2")

        assert read_last_lines(path) == [b"l2", b"l1"]

    def test_last_line_without_newline_is_candidate(self, tmp_path):
        # 崩溃半行与"写全但缺尾换行"都作为候选返回, 由调用方解析判定
        path = tmp_path / "a.jsonl"
        path.write_bytes(b"l1\nl2")

        assert read_last_lines(path) == [b"l2", b"l1"]

    def test_blank_lines_are_skipped(self, tmp_path):
        path = tmp_path / "a.jsonl"
        path.write_bytes(b"l1\n\n   \nl2\n")

        assert read_last_lines(path) == [b"l2", b"l1"]

    def test_max_lines(self, tmp_path):
        path = tmp_path / "a.jsonl"
        write_lines(path, "l1", "l2", "l3", "l4")

        assert read_last_lines(path, max_lines=2) == [b"l4", b"l3"]
        assert read_last_lines(path, max_lines=0) == []
        assert read_last_lines(path, max_lines=-1) == []

    def test_max_bytes_stops_after_limit(self, tmp_path):
        path = tmp_path / "a.jsonl"
        write_lines(path, "l1", "l2", "l3", "l4")

        assert read_last_lines(path, max_bytes=5) == [b"l4", b"l3"]
        assert read_last_lines(path, max_bytes=0) == []

    def test_utf8_multibyte_across_block_boundary(self, tmp_path):
        """块边界可能落在多字节字符中间: 只解码整行, 内容必须完整."""
        path = tmp_path / "a.jsonl"
        long_line = ("汉" * (BLOCK_SIZE // 3 + 7)).encode("utf-8")  # 让块边界落在字符中间
        tail_line = ("字" * 128).encode("utf-8")
        path.write_bytes(long_line + b"\n" + tail_line + b"\n")

        lines = read_last_lines(path)

        assert lines[0].decode("utf-8") == "字" * 128
        assert lines[1] == long_line  # 跨块拼回, 逐字节一致

    def test_huge_single_line_without_newline(self, tmp_path):
        path = tmp_path / "a.jsonl"
        huge = ("超" * (BLOCK_SIZE * 2 // 3)).encode("utf-8")
        path.write_bytes(huge)

        lines = read_last_lines(path)

        assert lines == [huge]

    def test_multi_block_file_keeps_order(self, tmp_path):
        path = tmp_path / "a.jsonl"
        payloads = [f"line-{index:05d}-" + "x" * 100 for index in range(2000)]
        write_lines(path, *payloads)

        lines = read_last_lines(path)

        assert [item.decode("utf-8") for item in lines] == list(reversed(payloads))

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(OSError):
            read_last_lines(tmp_path / "missing.jsonl")


class TestReadLastValidTurn:
    def test_returns_newest_valid_turn(self, tmp_path):
        path = tmp_path / "a.jsonl"
        write_lines(path, header(storage_id="s1"), turn(1, "第一条"), turn(2, "第二条"))

        record = read_last_valid_turn(path)

        assert record is not None
        assert record["seq"] == 2
        assert record["messages"][0]["content"] == "第二条"

    def test_skips_corrupt_and_partial_tail(self, tmp_path):
        path = tmp_path / "a.jsonl"
        write_lines(path, header(storage_id="s1"), turn(1), turn(2))
        with path.open("a", encoding="utf-8") as handle:
            handle.write("这不是 JSON\n")
            handle.write('{"type":"turn","seq":3,"messages":[')  # 无换行的崩溃半行

        record = read_last_valid_turn(path)

        assert record is not None and record["seq"] == 2

    def test_header_only_returns_none(self, tmp_path):
        path = tmp_path / "a.jsonl"
        write_lines(path, header(storage_id="s1"))

        assert read_last_valid_turn(path) is None

    def test_non_dict_and_non_turn_records_skipped(self, tmp_path):
        path = tmp_path / "a.jsonl"
        write_lines(path, header(storage_id="s1"), turn(1), "[1, 2, 3]", '{"type":"meta"}')

        record = read_last_valid_turn(path)

        assert record is not None and record["seq"] == 1

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(OSError):
            read_last_valid_turn(tmp_path / "missing.jsonl")


class TestEquivalenceWithFullScan:
    """尾读结果必须与"全量读取 + 取最后一条有效 turn"一致(A2 硬约束)."""

    @staticmethod
    def full_scan_last_turn(path) -> dict | None:
        last = None
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("type") == "turn":
                last = record
        return last

    @pytest.mark.parametrize("tail", ["", "损坏行\n", '{"type":"turn","seq":9', "   \n"])
    def test_same_last_turn_as_full_scan(self, tmp_path, tail):
        path = tmp_path / "a.jsonl"
        write_lines(path, header(storage_id="s1"), turn(1), turn(2), turn(3))
        if tail:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(tail)

        assert read_last_valid_turn(path) == self.full_scan_last_turn(path)
