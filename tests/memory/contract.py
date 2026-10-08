"""SessionStore 契约测试基类.

任何 SessionStore 后端(JSONL/SQLite/MySQL...)继承本类并覆盖 ``store`` fixture,
即可复用同一套行为规格; 基类名不以 Test 开头, 不会被 pytest 直接收集。
"""

import asyncio

import pytest

from meowmeowclaw.memory import (
    DEFAULT_MAX_TOOL_RESULT_CHARS,
    TOOL_RESULT_TRUNCATE_NOTICE,
    SessionKey,
    SessionMessage,
    SessionStore,
    SessionStoreError,
)


class SessionStoreContract:
    """短期记忆仓储行为契约."""

    @pytest.fixture
    def store(self):
        raise NotImplementedError("子类必须提供 store fixture")

    @pytest.fixture
    def key(self) -> SessionKey:
        return SessionKey(
            channel="cli",
            scope="session",
            conversation_id="conv-1",
            session_id="sess-1",
        )

    @staticmethod
    def turn(text: str, reply: str | None = None) -> list[SessionMessage]:
        messages = [SessionMessage(role="user", content=text)]
        if reply is not None:
            messages.append(SessionMessage(role="assistant", content=reply))
        return messages

    # ------------------------------------------------------------- 协议与空状态

    def test_implements_session_store_protocol(self, store):
        assert isinstance(store, SessionStore)

    @pytest.mark.asyncio
    async def test_missing_session_is_empty_and_mutations_are_idempotent(self, store, key):
        assert await store.load_recent(key) == []
        assert await store.get_meta(key) is None
        assert await store.list_sessions() == []

        await store.archive(key)  # 幂等: 不存在也不报错
        await store.purge(key)    # 幂等

        assert await store.load_recent(key) == []
        assert await store.get_meta(key) is None

    # ------------------------------------------------------------- 写入与读取

    @pytest.mark.asyncio
    async def test_append_then_load_roundtrip(self, store, key):
        meta = await store.append_turn(key, self.turn("你好", "喵"))

        assert meta.storage_id == key.storage_id
        assert meta.session_key == key
        assert meta.turn_count == 1
        assert meta.message_count == 2
        assert meta.archived is False
        assert meta.created_at_ms <= meta.updated_at_ms

        loaded = await store.load_recent(key)
        assert [message.role for message in loaded] == ["user", "assistant"]
        assert loaded[0].to_llm_message() == {"role": "user", "content": "你好"}
        assert loaded[1].content == "喵"

        fetched = await store.get_meta(key)
        assert fetched is not None
        assert fetched.turn_count == 1
        assert fetched.message_count == 2
        assert fetched.session_key == key

    @pytest.mark.asyncio
    async def test_multiple_turns_keep_order_and_counts(self, store, key):
        for index in range(1, 4):
            await store.append_turn(key, self.turn(f"t{index}", f"a{index}"))

        loaded = await store.load_recent(key)
        assert [message.content for message in loaded] == [
            "t1", "a1", "t2", "a2", "t3", "a3",
        ]

        meta = await store.get_meta(key)
        assert meta.turn_count == 3
        assert meta.message_count == 6

    @pytest.mark.asyncio
    async def test_dict_messages_are_accepted(self, store, key):
        await store.append_turn(
            key,
            [
                {"role": "user", "content": "dict 用户"},
                {"role": "assistant", "content": "dict 助手"},
            ],
        )

        loaded = await store.load_recent(key)
        assert [message.content for message in loaded] == ["dict 用户", "dict 助手"]

    @pytest.mark.asyncio
    async def test_message_timestamps_are_filled(self, store, key):
        meta = await store.append_turn(key, self.turn("hi", "ok"))

        loaded = await store.load_recent(key)
        assert all(message.ts_ms is not None for message in loaded)
        assert loaded[0].ts_ms >= meta.created_at_ms

    @pytest.mark.asyncio
    async def test_invalid_inputs_raise_session_store_error(self, store, key):
        with pytest.raises(SessionStoreError):
            await store.append_turn(key, [])

        with pytest.raises(SessionStoreError):
            await store.load_recent(key, max_turns=-1)

        with pytest.raises(SessionStoreError):
            await store.load_recent(key, max_chars=-1)

    # ------------------------------------------------------------- 窗口裁剪

    @pytest.mark.asyncio
    async def test_max_turns_keeps_latest_whole_turns(self, store, key):
        for index in range(1, 4):
            await store.append_turn(key, self.turn(f"t{index}", f"a{index}"))

        loaded = await store.load_recent(key, max_turns=2)
        assert [message.content for message in loaded] == ["t2", "a2", "t3", "a3"]

        assert await store.load_recent(key, max_turns=0) == []

    @pytest.mark.asyncio
    async def test_max_chars_keeps_latest_fitting_turns(self, store, key):
        await store.append_turn(key, self.turn("a" * 100, "ra"))
        await store.append_turn(key, self.turn("b" * 10, "rb"))
        await store.append_turn(key, self.turn("c" * 10, "rc"))

        loaded = await store.load_recent(key, max_chars=25)

        contents = [message.content for message in loaded]
        assert "a" * 100 not in contents
        assert contents == ["b" * 10, "rb", "c" * 10, "rc"]

    @pytest.mark.asyncio
    async def test_max_chars_always_keeps_newest_turn(self, store, key):
        await store.append_turn(key, self.turn("x" * 100, "rx"))

        loaded = await store.load_recent(key, max_chars=5)

        assert [message.content for message in loaded] == ["x" * 100, "rx"]

    @pytest.mark.asyncio
    async def test_tool_result_is_truncated(self, store, key):
        await store.append_turn(
            key,
            [
                SessionMessage(role="assistant", tool_calls=[{"id": "call_1", "type": "function"}]),
                SessionMessage(
                    role="tool",
                    tool_call_id="call_1",
                    content="x" * (DEFAULT_MAX_TOOL_RESULT_CHARS + 100),
                ),
            ],
        )

        loaded = await store.load_recent(key)
        tool_message = loaded[1]
        assert tool_message.content is not None
        assert len(tool_message.content) == DEFAULT_MAX_TOOL_RESULT_CHARS + len(
            TOOL_RESULT_TRUNCATE_NOTICE
        )
        assert tool_message.content.endswith(TOOL_RESULT_TRUNCATE_NOTICE)

    # ------------------------------------------------------------- 归档与删除

    @pytest.mark.asyncio
    async def test_archive_hides_from_active_but_keeps_meta(self, store, key):
        await store.append_turn(key, self.turn("旧会话", "好的"))

        await store.archive(key)

        assert await store.load_recent(key) == []
        meta = await store.get_meta(key)
        assert meta is not None
        assert meta.archived is True
        assert meta.turn_count == 1

        assert [summary.storage_id for summary in await store.list_sessions()] == [key.storage_id]
        assert await store.list_sessions(include_archived=False) == []

    @pytest.mark.asyncio
    async def test_purge_removes_active_and_archived(self, store, key):
        await store.append_turn(key, self.turn("待删除", "好"))
        await store.archive(key)

        await store.purge(key)

        assert await store.get_meta(key) is None
        assert await store.load_recent(key) == []
        assert await store.list_sessions() == []

    # ------------------------------------------------------------- 列表与并发

    @pytest.mark.asyncio
    async def test_list_sessions_sorted_with_unique_short_ids(self, store, key):
        other = SessionKey(
            channel="qq",
            scope="private",
            conversation_id="10001",
            session_id="sess-2",
        )
        await store.append_turn(key, self.turn("第一条", "好"))
        await asyncio.sleep(0.005)
        await store.append_turn(other, self.turn("第二条", "好"))

        summaries = await store.list_sessions()

        assert [summary.storage_id for summary in summaries] == [
            other.storage_id,
            key.storage_id,
        ]
        assert len({summary.short_id for summary in summaries}) == 2
        assert all(len(summary.short_id) >= 8 for summary in summaries)
        assert all(
            summary.storage_id.startswith(summary.short_id) for summary in summaries
        )

    @pytest.mark.asyncio
    async def test_concurrent_appends_are_serialized(self, store, key):
        await asyncio.gather(
            *(
                store.append_turn(key, self.turn(f"m{index}", f"r{index}"))
                for index in range(5)
            )
        )

        meta = await store.get_meta(key)
        assert meta is not None
        assert meta.turn_count == 5
        assert meta.message_count == 10

        loaded = await store.load_recent(key)
        contents = {message.content for message in loaded}
        assert {f"m{index}" for index in range(5)} <= contents
        assert {f"r{index}" for index in range(5)} <= contents
