"""A+ A1: ``JsonlSessionStore.stats()`` 契约与计时钩子(纯观测, 不改行为)."""

import pytest

from meowmeowclaw.memory import JsonlSessionStore, SessionKey, SessionMessage
from meowmeowclaw.memory.errors import SessionStoreError


def make_key(conversation_id: str = "stats-1") -> SessionKey:
    return SessionKey(channel="cli", scope="session", conversation_id=conversation_id)


def user_message(text: str = "你好") -> SessionMessage:
    return SessionMessage(role="user", content=text)


class TestJsonlSessionStoreStats:
    @pytest.mark.asyncio
    async def test_stats_shape_on_empty_store(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        try:
            snapshot = store.stats()
        finally:
            await store.close()

        assert set(snapshot) == {"metrics", "active_file_count", "last_file_size_bytes"}
        assert snapshot["active_file_count"] == 0
        assert snapshot["last_file_size_bytes"] is None
        assert set(snapshot["metrics"]) == {"load_recent", "get_meta", "append_turn"}

    @pytest.mark.asyncio
    async def test_counters_and_file_size_after_operations(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        try:
            key = make_key()
            await store.append_turn(key, [user_message()])
            await store.load_recent(key, max_turns=10, max_chars=1000)
            await store.get_meta(key)

            snapshot = store.stats()
        finally:
            await store.close()

        assert snapshot["metrics"]["append_turn"]["count"] == 1
        assert snapshot["metrics"]["load_recent"]["count"] == 1
        assert snapshot["metrics"]["get_meta"]["count"] == 1
        assert snapshot["metrics"]["load_recent"]["p95"] is not None
        assert snapshot["metrics"]["append_turn"]["avg"] is not None
        assert snapshot["active_file_count"] == 1
        assert snapshot["last_file_size_bytes"] and snapshot["last_file_size_bytes"] > 0

    @pytest.mark.asyncio
    async def test_failed_call_is_still_counted(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        try:
            with pytest.raises(SessionStoreError):
                await store.load_recent("not-a-key")  # type: ignore[arg-type]

            snapshot = store.stats()
        finally:
            await store.close()

        assert snapshot["metrics"]["load_recent"]["count"] == 1
        assert snapshot["last_file_size_bytes"] is None

    @pytest.mark.asyncio
    async def test_missing_session_keeps_file_size_none(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        try:
            assert await store.get_meta(make_key("missing")) is None
            snapshot = store.stats()
        finally:
            await store.close()

        assert snapshot["metrics"]["get_meta"]["count"] == 1
        assert snapshot["last_file_size_bytes"] is None

    @pytest.mark.asyncio
    async def test_archived_session_size_is_reported(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        try:
            key = make_key()
            await store.append_turn(key, [user_message()])
            await store.archive(key)
            await store.get_meta(key)  # 走 archive 回退

            snapshot = store.stats()
        finally:
            await store.close()

        assert snapshot["last_file_size_bytes"] and snapshot["last_file_size_bytes"] > 0

    @pytest.mark.asyncio
    async def test_active_file_count_tracks_multiple_sessions(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        try:
            for index in range(3):
                await store.append_turn(make_key(f"c-{index}"), [user_message()])

            snapshot = store.stats()
        finally:
            await store.close()

        assert snapshot["active_file_count"] == 3
