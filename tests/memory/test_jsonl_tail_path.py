"""A+ A2: ``JsonlSessionStore`` 三个读路径改走尾读 + 回退与口径语义.

对照 docs/A_PLUS_OPTIMIZATION.md §3.2:
- 正常文件: ``get_meta / append_turn / list_sessions`` 不再全量扫描(用打桩证明);
- 异常文件(首行损坏/末轮缺 seq 等): **回退全量扫描**, 行为与旧实现一致;
- A+1 口径: 中间行损坏时 ``turn_count`` 取末轮 ``seq``(累计), 正常文件与全量扫描完全一致。
"""

import json

import pytest

from meowmeowclaw.memory import JsonlSessionStore, SessionKey, SessionMessage


def make_key(conversation_id: str = "tail-1") -> SessionKey:
    return SessionKey(channel="cli", scope="session", conversation_id=conversation_id)


def header_line(key: SessionKey) -> str:
    return json.dumps(
        {
            "type": "header",
            "schema_version": 1,
            "storage_id": key.storage_id,
            "session_key": key.canonical,
            "created_at_ms": 1_700_000_000_000,
        },
        ensure_ascii=False,
    )


def turn_line(seq: int, text: str = "你好", *, with_counters: bool = True) -> str:
    record = {
        "type": "turn",
        "schema_version": 1,
        "seq": seq,
        "ts_ms": 1_700_000_000_000 + seq,
        "messages": [{"role": "user", "content": text}],
    }
    if with_counters:
        record["message_total"] = seq
    return json.dumps(record, ensure_ascii=False)


def session_path(store: JsonlSessionStore, key: SessionKey):
    return store.sessions_dir / f"{key.storage_id}.jsonl"


def forbid_full_scan(monkeypatch) -> None:
    """正常文件不允许落到全量扫描(落到即断言失败)."""

    def _fail(cls, path):  # noqa: ANN001
        raise AssertionError(f"不应走全量扫描: {path}")

    monkeypatch.setattr(JsonlSessionStore, "_read_file", classmethod(_fail))


class TestTailPathIsUsed:
    @pytest.mark.asyncio
    async def test_get_meta_uses_tail_path(self, tmp_path, monkeypatch):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key()
        await store.append_turn(key, [SessionMessage(role="user", content="第一条")])

        forbid_full_scan(monkeypatch)
        meta = await store.get_meta(key)

        assert meta is not None and meta.turn_count == 1

    @pytest.mark.asyncio
    async def test_append_turn_uses_tail_path(self, tmp_path, monkeypatch):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key()
        await store.append_turn(key, [SessionMessage(role="user", content="第一条")])

        forbid_full_scan(monkeypatch)
        meta = await store.append_turn(key, [SessionMessage(role="user", content="第二条")])

        assert meta.turn_count == 2
        assert meta.message_count == 2

    @pytest.mark.asyncio
    async def test_list_sessions_uses_tail_path(self, tmp_path, monkeypatch):
        store = JsonlSessionStore(tmp_path / "memory")
        for index in range(3):
            key = make_key(f"tail-{index}")
            await store.append_turn(key, [SessionMessage(role="user", content=f"第{index}条")])

        forbid_full_scan(monkeypatch)
        summaries = await store.list_sessions(include_archived=True)

        assert len(summaries) == 3
        assert all(summary.turn_count == 1 for summary in summaries)

    @pytest.mark.asyncio
    async def test_archived_meta_uses_tail_path(self, tmp_path, monkeypatch):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key()
        await store.append_turn(key, [SessionMessage(role="user", content="第一条")])
        await store.archive(key)

        forbid_full_scan(monkeypatch)
        meta = await store.get_meta(key)

        assert meta is not None and meta.archived is True and meta.turn_count == 1


class TestFallbackToFullScan:
    @pytest.mark.asyncio
    async def test_corrupt_first_line_falls_back(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key()
        path = session_path(store, key)
        path.write_text(
            "这不是 JSON\n" + header_line(key) + "\n" + turn_line(1) + "\n",
            encoding="utf-8",
        )

        meta = await store.get_meta(key)

        # 全量扫描能跳过损坏首行并找到合法 header
        assert meta is not None and meta.turn_count == 1

    @pytest.mark.asyncio
    async def test_turn_without_counters_falls_back(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key()
        path = session_path(store, key)
        path.write_text(
            header_line(key)
            + "\n"
            + turn_line(1)
            + "\n"
            + turn_line(2, with_counters=False)
            + "\n",
            encoding="utf-8",
        )

        meta = await store.get_meta(key)

        # 回退全量扫描: turn_count = 有效 turn 行数
        assert meta is not None and meta.turn_count == 2
        assert meta.message_count == 2

    @pytest.mark.asyncio
    async def test_header_only_file(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key()
        path = session_path(store, key)
        path.write_text(header_line(key) + "\n", encoding="utf-8")

        meta = await store.get_meta(key)

        assert meta is not None
        assert meta.turn_count == 0 and meta.message_count == 0
        assert meta.updated_at_ms == meta.created_at_ms


class TestMetaSemantics:
    @pytest.mark.asyncio
    async def test_tail_state_equals_full_scan_on_normal_file(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key()
        for index in range(1, 4):
            await store.append_turn(key, [SessionMessage(role="user", content=f"第{index}条")])
        path = session_path(store, key)

        tail = JsonlSessionStore._read_tail_state(path)
        full = JsonlSessionStore._read_file(path)

        assert tail is not None and full is not None
        assert tail.header == full.header
        assert tail.last_turn == full.last_turn
        assert tail.turn_count == full.turn_count == 3
        assert tail.message_count == full.message_count == 3

    @pytest.mark.asyncio
    async def test_middle_corrupt_line_uses_cumulative_seq(self, tmp_path):
        """A+1 已确认口径: 中间行损坏时 turn_count 取末轮 seq(累计序号)."""
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key()
        path = session_path(store, key)
        path.write_text(
            header_line(key)
            + "\n"
            + turn_line(1)
            + "\n"
            + "这不是 JSON\n"
            + turn_line(3)
            + "\n",
            encoding="utf-8",
        )

        meta = await store.get_meta(key)
        full = JsonlSessionStore._read_file(path)

        assert meta is not None and meta.turn_count == 3  # 尾读: 累计口径(seq)
        assert meta.message_count == 3  # message_total
        assert full is not None and full.turn_count == 2  # 对照: 全量为"有效行计数"

    @pytest.mark.asyncio
    async def test_append_after_middle_corrupt_line_continues_seq(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key()
        path = session_path(store, key)
        path.write_text(
            header_line(key) + "\n" + turn_line(1) + "\n" + "坏行\n" + turn_line(3) + "\n",
            encoding="utf-8",
        )

        await store.append_turn(key, [SessionMessage(role="user", content="接着写")])

        turns = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("{") and json.loads(line).get("type") == "turn"
        ]
        assert [item["seq"] for item in turns] == [1, 3, 4]  # 从末轮 seq 继续, 不重复
