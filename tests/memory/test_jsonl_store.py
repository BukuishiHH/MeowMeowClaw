"""meowmeowclaw/memory/jsonl.py 的单元测试.

- 继承 ``SessionStoreContract`` 复用后端无关的行为规格;
- 额外覆盖 JSONL 特有的文件布局、UTF-8 原文、损坏行容错、归档/删除、关闭语义与短 ID。
"""

import asyncio
import json

import pytest

from meowmeowclaw.memory import (
    TOOL_RESULT_TRUNCATE_NOTICE,
    JsonlSessionStore,
    SessionKey,
    SessionMessage,
    SessionStoreError,
)

from contract import SessionStoreContract


class TestJsonlSessionStore(SessionStoreContract):
    @pytest.fixture
    def store(self, tmp_path) -> JsonlSessionStore:
        return JsonlSessionStore(tmp_path / "memory")


# ------------------------------------------------------------------ 文件布局


class TestJsonlLayout:
    @pytest.mark.asyncio
    async def test_creates_directories_on_init(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")

        assert store.sessions_dir.is_dir()
        assert store.archive_dir.is_dir()
        assert str(tmp_path) in repr(store)

    @pytest.mark.asyncio
    async def test_header_and_turn_lines(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = SessionKey(channel="cli", scope="session", conversation_id="conv", session_id="s1")

        await store.append_turn(key, [SessionMessage(role="user", content="你好，世界")])

        path = store.sessions_dir / f"{key.storage_id}.jsonl"
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        header = json.loads(lines[0])
        turn = json.loads(lines[1])

        assert header["type"] == "header"
        assert header["session_key"] == key.canonical
        assert header["storage_id"] == key.storage_id
        assert turn["type"] == "turn"
        assert turn["seq"] == 1
        assert turn["message_total"] == 1
        # ensure_ascii=False: 原文中文不被转义
        assert "你好，世界" in lines[1]

    @pytest.mark.asyncio
    async def test_archived_file_is_moved(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = SessionKey(channel="qq", scope="private", conversation_id="10001", session_id="s2")
        await store.append_turn(key, [SessionMessage(role="user", content="hi")])

        active = store.sessions_dir / f"{key.storage_id}.jsonl"
        archived = store.archive_dir / f"{key.storage_id}.jsonl"
        assert active.is_file()

        await store.archive(key)

        assert not active.exists()
        assert archived.is_file()

        await store.purge(key)
        assert not archived.exists()

    @pytest.mark.asyncio
    async def test_corrupt_lines_are_skipped(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = SessionKey(channel="cli", scope="session", conversation_id="conv", session_id="s3")
        await store.append_turn(key, [SessionMessage(role="user", content="第一条")])

        path = store.sessions_dir / f"{key.storage_id}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write("这不是 JSON\n")
            handle.write("[1, 2, 3]\n")  # 合法 JSON 但不是 dict

        loaded = await store.load_recent(key)
        meta = await store.get_meta(key)

        assert [message.content for message in loaded] == ["第一条"]
        assert meta is not None
        assert meta.turn_count == 1


# ------------------------------------------------------------------ 配置与容错


class TestJsonlBehavior:
    @pytest.mark.asyncio
    async def test_custom_tool_result_limit(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory", max_tool_result_chars=10)
        key = SessionKey(channel="cli", scope="session", conversation_id="conv", session_id="s4")

        await store.append_turn(
            key,
            [SessionMessage(role="tool", tool_call_id="c1", content="y" * 50)],
        )

        loaded = await store.load_recent(key)
        assert loaded[0].content == "y" * 10 + TOOL_RESULT_TRUNCATE_NOTICE

    @pytest.mark.asyncio
    async def test_invalid_key_type_is_rejected(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")

        with pytest.raises(SessionStoreError):
            await store.append_turn("not-a-key", [SessionMessage(role="user", content="x")])

        with pytest.raises(SessionStoreError):
            await store.load_recent("not-a-key")

    @pytest.mark.asyncio
    async def test_closed_store_rejects_calls(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = SessionKey(channel="cli", scope="session", conversation_id="conv", session_id="s5")

        await store.close()

        with pytest.raises(SessionStoreError):
            await store.load_recent(key)
        with pytest.raises(SessionStoreError):
            await store.get_meta(key)
        with pytest.raises(SessionStoreError):
            await store.list_sessions()

    @pytest.mark.asyncio
    async def test_invalid_header_key_falls_back(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = SessionKey(channel="cli", scope="session", conversation_id="conv", session_id="s6")
        await store.append_turn(key, [SessionMessage(role="user", content="x")])

        path = store.sessions_dir / f"{key.storage_id}.jsonl"
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        header = json.loads(lines[0])
        header["session_key"] = "not-a-valid-key"
        path.write_text(
            json.dumps(header, ensure_ascii=False) + "\n" + lines[1] + "\n",
            encoding="utf-8",
        )

        meta = await store.get_meta(key)

        assert meta is not None
        assert meta.session_key == key  # 回退到请求键

    def test_short_ids_extend_on_prefix_collision(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        first = "abcdefgh" + "x" * 18
        second = "abcdefgh" + "y" * 18

        short_ids = store._short_ids([first, second])  # noqa: SLF001 - 直接验证生成规则

        assert short_ids[first] == "abcdefghx"
        assert short_ids[second] == "abcdefghy"
        assert short_ids[first] != short_ids[second]

    @pytest.mark.asyncio
    async def test_list_sessions_include_archived_flag(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = SessionKey(channel="qq", scope="private", conversation_id="10001", session_id="s7")
        await store.append_turn(key, [SessionMessage(role="user", content="x")])
        await store.archive(key)

        assert await store.list_sessions(include_archived=False) == []
        archived = await store.list_sessions(include_archived=True)
        assert len(archived) == 1
        assert archived[0].archived is True

    @pytest.mark.asyncio
    async def test_unknown_fields_are_ignored(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = SessionKey(channel="cli", scope="session", conversation_id="conv", session_id="s8")
        await store.append_turn(key, [SessionMessage(role="user", content="x")])

        path = store.sessions_dir / f"{key.storage_id}.jsonl"
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        turn = json.loads(lines[1])
        turn["future_field"] = "ignored"
        path.write_text(lines[0] + "\n" + json.dumps(turn, ensure_ascii=False) + "\n", encoding="utf-8")

        loaded = await store.load_recent(key)

        assert [message.content for message in loaded] == ["x"]


class TestJsonlDuplicatePrecedence:
    @pytest.mark.asyncio
    async def test_active_preferred_over_archived_on_duplicate(self, tmp_path):
        import shutil

        store = JsonlSessionStore(tmp_path / "memory")
        key = SessionKey(channel="cli", scope="session", conversation_id="conv", session_id="dup")
        await store.append_turn(key, [SessionMessage(role="user", content="x")])

        active = store.sessions_dir / f"{key.storage_id}.jsonl"
        archived = store.archive_dir / f"{key.storage_id}.jsonl"
        shutil.copyfile(active, archived)  # 人为制造重复, 验证活跃优先

        summaries = await store.list_sessions(include_archived=True)

        assert len(summaries) == 1
        assert summaries[0].archived is False


# ------------------------------------------------- 并发与崩溃恢复(M7)


class TestConcurrencyAndCrashRecovery:
    @pytest.mark.asyncio
    async def test_two_store_instances_serialize_appends(self, tmp_path):
        root = tmp_path / "memory"
        key = SessionKey(channel="cli", scope="session", conversation_id="conv", session_id="s9")
        store_a = JsonlSessionStore(root)
        store_b = JsonlSessionStore(root)

        await asyncio.gather(
            store_a.append_turn(key, [SessionMessage(role="user", content="A")]),
            store_b.append_turn(key, [SessionMessage(role="user", content="B")]),
        )

        meta = await store_a.get_meta(key)
        assert meta is not None
        assert meta.turn_count == 2
        loaded = await store_a.load_recent(key)
        assert {message.content for message in loaded} == {"A", "B"}

        path = store_a.sessions_dir / f"{key.storage_id}.jsonl"
        turns = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("type") == "turn"
        ]
        assert [turn["seq"] for turn in turns] == [1, 2]

    @pytest.mark.asyncio
    async def test_partial_last_line_is_skipped_and_newline_restored(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = SessionKey(channel="cli", scope="session", conversation_id="conv", session_id="s10")
        await store.append_turn(key, [SessionMessage(role="user", content="第一条")])
        path = store.sessions_dir / f"{key.storage_id}.jsonl"

        # 模拟崩溃: 末尾留下一个没有换行的半行 JSON
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"type":"turn","seq":2,"ts_ms":')

        loaded = await store.load_recent(key)
        meta = await store.get_meta(key)
        assert [message.content for message in loaded] == ["第一条"]
        assert meta is not None
        assert meta.turn_count == 1

        # 下一次写入应先补换行, 且 seq 从有效记录继续
        await store.append_turn(key, [SessionMessage(role="user", content="第二条")])

        loaded = await store.load_recent(key)
        meta = await store.get_meta(key)
        assert [message.content for message in loaded] == ["第一条", "第二条"]
        assert meta is not None
        assert meta.turn_count == 2

        valid_turns = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") == "turn":
                valid_turns.append(record)
        assert [turn["seq"] for turn in valid_turns] == [1, 2]
