"""M6: MemoryRecord / LongTermStore 抽象 / NoopLongTermStore 测试."""

import pytest

from meowmeowclaw.memory import (
    LongTermStore,
    MemoryRecord,
    NoopLongTermStore,
    new_memory_record,
)


class TestMemoryRecord:
    def test_factory_fills_id_and_timestamps(self):
        record = new_memory_record("user:default", "用户偏好 Python", tags=("preference",))

        assert record.id
        assert record.namespace == "user:default"
        assert record.kind == "fact"
        assert record.content == "用户偏好 Python"
        assert record.tags == ("preference",)
        assert record.created_at_ms == record.updated_at_ms
        assert record.created_at_iso.endswith("Z")

    def test_timestamps_can_be_injected(self):
        record = new_memory_record("user:default", "x", now_ms=1234)

        assert record.created_at_ms == 1234
        assert record.updated_at_ms == 1234

    def test_tags_are_stripped(self):
        record = new_memory_record("user:default", "x", tags=("  a  ", "", "b"))

        assert record.tags == ("a", "b")

    def test_validation_rejects_empty_fields(self):
        for field in ("id", "namespace", "kind", "content"):
            with pytest.raises(ValueError):
                MemoryRecord(
                    **{
                        "id": "id-1",
                        "namespace": "user:default",
                        "kind": "fact",
                        "content": "x",
                        field: "",
                    }
                )

    def test_validation_rejects_bad_confidence(self):
        with pytest.raises(ValueError):
            new_memory_record("user:default", "x", confidence=1.5)

    def test_validation_rejects_time_order(self):
        with pytest.raises(ValueError):
            MemoryRecord(
                id="id-1",
                namespace="user:default",
                kind="fact",
                content="x",
                created_at_ms=100,
                updated_at_ms=50,
            )

    def test_to_dict_from_dict_roundtrip(self):
        record = new_memory_record(
            "user:default",
            "用户喜欢猫",
            kind="preference",
            tags=("pet",),
            source_session="v1:cli:session:abc",
        )

        restored = MemoryRecord.from_dict(record.to_dict())

        assert restored == record

    def test_from_dict_ignores_unknown_fields(self):
        restored = MemoryRecord.from_dict(
            {
                "id": "id-1",
                "namespace": "user:default",
                "kind": "fact",
                "content": "x",
                "unknown": "ignored",
            }
        )

        assert restored.id == "id-1"
        assert restored.content == "x"


class TestNoopLongTermStore:
    def test_implements_protocol(self):
        assert isinstance(NoopLongTermStore(), LongTermStore)

    def test_repr(self):
        assert "NoopLongTermStore" in repr(NoopLongTermStore())

    @pytest.mark.asyncio
    async def test_recall_returns_empty(self):
        store = NoopLongTermStore()

        assert await store.recall("user:default") == []
        assert await store.recall("user:default", query="python", limit=5) == []

    @pytest.mark.asyncio
    async def test_remember_echoes_record_without_persisting(self):
        store = NoopLongTermStore()
        record = new_memory_record("user:default", "x")

        returned = await store.remember("user:default", record)

        assert returned is record
        assert await store.recall("user:default") == []

    @pytest.mark.asyncio
    async def test_forget_list_and_close_are_noop(self):
        store = NoopLongTermStore()

        assert await store.forget("user:default", "id-1") is None
        assert await store.list_namespaces() == []
        assert await store.close() is None
