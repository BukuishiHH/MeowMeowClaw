"""meowmeowclaw/memory/models.py 的单元测试."""

import copy

import pytest

from meowmeowclaw.memory import (
    InvalidSessionKeyError,
    SessionKey,
    SessionMessage,
    SessionMeta,
    SessionSummary,
    ms_to_iso,
    utc_now_ms,
)


class TestSessionKey:
    def test_cli_canonical(self):
        key = SessionKey(channel="cli", scope="session", conversation_id="abc123")

        assert key.canonical == "v1:cli:session:abc123"
        assert str(key) == key.canonical

    def test_qq_contact_and_instance_keys(self):
        contact = SessionKey(channel="qq", scope="private", conversation_id="10001")
        instance = contact.with_session("sess-9")

        assert contact.canonical == "v1:qq:private:10001"
        assert instance.canonical == "v1:qq:private:10001:sess-9"
        assert instance.contact_key() == contact
        assert contact.contact_key() is contact or contact.contact_key() == contact

    def test_storage_id_is_stable_and_safe(self):
        key = SessionKey(channel="cli", scope="session", conversation_id="abc123")

        first = key.storage_id
        second = SessionKey(channel="cli", scope="session", conversation_id="abc123").storage_id

        assert first == second
        assert len(first) == 26
        assert first.isalnum() and first.islower()
        assert first != key.with_session("other").storage_id

    def test_user_id_does_not_change_canonical(self):
        key = SessionKey(channel="qq", scope="private", conversation_id="10001", user_id="10001")

        assert key.canonical == "v1:qq:private:10001"
        assert key.storage_id == SessionKey(
            channel="qq", scope="private", conversation_id="10001"
        ).storage_id

    def test_from_canonical_roundtrip(self):
        for canonical in ("v1:cli:session:abc", "v1:qq:private:10001:sess-9"):
            key = SessionKey.from_canonical(canonical)
            assert key.canonical == canonical

    @pytest.mark.parametrize(
        "canonical",
        [
            "",
            "cli:session:abc",            # 缺少版本前缀
            "v0:cli:session:abc",         # 版本非法
            "v1:cli:session",             # 段数不足
            "v1:cli:session:abc:def:ghi", # 段数过多
        ],
    )
    def test_from_canonical_rejects_invalid(self, canonical):
        with pytest.raises(InvalidSessionKeyError):
            SessionKey.from_canonical(canonical)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("channel", ""),
            ("scope", "   "),
            ("conversation_id", "a:b"),
            ("session_id", "bad:id"),
        ],
    )
    def test_components_are_validated(self, field, value):
        params = {"channel": "cli", "scope": "session", "conversation_id": "abc"}
        params[field] = value

        with pytest.raises(InvalidSessionKeyError):
            SessionKey(**params)

    def test_version_must_be_positive(self):
        with pytest.raises(InvalidSessionKeyError):
            SessionKey(channel="cli", scope="session", conversation_id="abc", version=0)


class TestSessionMessage:
    def test_to_llm_message_strips_internal_fields(self):
        message = SessionMessage(
            role="assistant",
            content="hi",
            tool_calls=[{"id": "call_1", "type": "function"}],
            ts_ms=123,
        )

        assert message.to_llm_message() == {
            "role": "assistant",
            "content": "hi",
            "tool_calls": [{"id": "call_1", "type": "function"}],
        }

    def test_to_llm_message_omits_none_fields(self):
        message = SessionMessage(role="assistant", tool_calls=[{"id": "call_1"}], ts_ms=1)

        assert message.to_llm_message() == {
            "role": "assistant",
            "tool_calls": [{"id": "call_1"}],
        }

    def test_from_llm_message_roundtrip(self):
        original = {
            "role": "user",
            "content": "你好",
            "name": "tester",
            "tool_calls": [{"id": "call_1"}],
        }

        message = SessionMessage.from_llm_message(original, ts_ms=42)

        assert message.ts_ms == 42
        assert message.to_llm_message() == original

    def test_storage_roundtrip(self):
        message = SessionMessage(
            role="tool",
            content="结果",
            tool_call_id="call_1",
            ts_ms=7,
        )

        restored = SessionMessage.from_storage_dict(message.to_storage_dict())

        assert restored.role == "tool"
        assert restored.content == "结果"
        assert restored.tool_call_id == "call_1"
        assert restored.ts_ms == 7

    def test_tool_calls_are_deep_copied(self):
        tool_calls = [{"id": "call_1", "function": {"name": "read_file"}}]
        message = SessionMessage(role="assistant", tool_calls=tool_calls)

        exported = message.to_llm_message()
        exported["tool_calls"][0]["id"] = "changed"

        assert tool_calls[0]["id"] == "call_1"
        assert message.tool_calls is not None
        assert message.tool_calls[0]["id"] == "call_1"

    def test_char_size_counts_content_and_tool_calls(self):
        message = SessionMessage(
            role="assistant",
            content="abc",
            tool_calls=[{"id": "call_1"}],
        )

        assert message.char_size() >= 3 + len('{"id": "call_1"}'.replace(" ", ""))

    def test_invalid_role(self):
        with pytest.raises(ValueError):
            SessionMessage(role="")

    def test_invalid_tool_calls_type(self):
        with pytest.raises(ValueError):
            SessionMessage(role="assistant", tool_calls={"not": "list"})


class TestMetaAndSummary:
    def test_iso_properties(self):
        meta = SessionMeta(
            storage_id="abc",
            session_key=SessionKey(channel="cli", scope="session", conversation_id="c"),
            created_at_ms=0,
            updated_at_ms=1000,
            turn_count=1,
            message_count=2,
        )

        assert meta.created_at_iso == "1970-01-01T00:00:00.000Z"
        assert meta.updated_at_iso == "1970-01-01T00:00:01.000Z"
        assert meta.channel == "cli"
        assert meta.scope == "session"
        assert meta.conversation_id == "c"

    def test_summary_properties(self):
        summary = SessionSummary(
            short_id="abcdefgh",
            storage_id="abcdefgh1234567890123456",
            session_key=SessionKey(channel="qq", scope="private", conversation_id="10001"),
            created_at_ms=0,
            updated_at_ms=1000,
            turn_count=3,
            message_count=6,
            archived=True,
        )

        assert summary.channel == "qq"
        assert summary.archived is True
        assert summary.updated_at_iso.endswith("Z")


def test_ms_to_iso_utc():
    assert ms_to_iso(0) == "1970-01-01T00:00:00.000Z"


def test_utc_now_ms_is_reasonable():
    now = utc_now_ms()

    assert now > 1_700_000_000_000


def test_deepcopy_message_does_not_share_tool_calls():
    message = SessionMessage(role="assistant", tool_calls=[{"id": "call_1"}])

    clone = copy.deepcopy(message)
    clone.tool_calls[0]["id"] = "changed"

    assert message.tool_calls[0]["id"] == "call_1"
