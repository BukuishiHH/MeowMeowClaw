"""Envelope 契约测试."""

import dataclasses

import pytest

from meowmeowclaw.gateway import (
    KIND_REPLY,
    KIND_REQUEST,
    Envelope,
    make_inbound,
    make_reply,
    new_message_id,
)


class TestMakeInbound:
    def test_fields_and_defaults(self):
        env = make_inbound(channel="qq", text="你好", conversation_id="10001", sender_id="10001")

        assert env.kind == KIND_REQUEST
        assert env.channel == "qq"
        assert env.text == "你好"
        assert env.conversation_id == "10001"
        assert env.sender_id == "10001"
        assert env.message_id
        assert env.received_at_ms > 0
        assert env.created_at_ms > 0
        assert env.target_channel is None

    def test_platform_message_id_and_timestamp_kept(self):
        env = make_inbound(
            channel="qq", text="hi", message_id="m-1", received_at_ms=123456789
        )

        assert env.message_id == "m-1"
        assert env.received_at_ms == 123456789

    def test_metadata_is_copied(self):
        source = {"a": 1}
        env = make_inbound(channel="x", text="t", metadata=source)

        assert env.metadata == {"a": 1}
        assert env.metadata is not source


class TestMakeReply:
    def test_routing_and_correlation(self):
        request = make_inbound(
            channel="qq", text="问", conversation_id="10001", sender_id="10001",
            message_id="m-1", received_at_ms=111,
        )

        reply = make_reply(request, "答", session_id="s-1")

        assert reply.kind == KIND_REPLY
        assert reply.channel == "qq"
        assert reply.target_channel == "qq"
        assert reply.conversation_id == "10001"
        assert reply.session_id == "s-1"
        assert reply.correlation_id == "m-1"
        assert reply.reply_to == "m-1"
        assert reply.received_at_ms == 111
        assert reply.text == "答"
        assert reply.metadata["source"] == "agent"

    def test_metadata_merge(self):
        request = make_inbound(channel="x", text="q")
        reply = make_reply(request, "a", metadata={"completed": False})

        assert reply.metadata["source"] == "agent"
        assert reply.metadata["completed"] is False


class TestValidation:
    def test_invalid_kind(self):
        with pytest.raises(ValueError, match="kind"):
            Envelope(kind="whatever", channel="x")

    def test_empty_channel(self):
        with pytest.raises(ValueError, match="channel"):
            Envelope(kind=KIND_REQUEST, channel="")

    def test_metadata_must_be_dict(self):
        with pytest.raises(ValueError, match="metadata"):
            Envelope(kind=KIND_REQUEST, channel="x", metadata="bad")  # type: ignore[arg-type]

    def test_reply_can_use_channel_as_route(self):
        env = Envelope(kind=KIND_REPLY, channel="x", text="a")
        assert env.target_channel is None

    def test_message_id_none_rejected(self):
        with pytest.raises(ValueError):
            Envelope(kind=KIND_REQUEST, channel="x", message_id=None)  # type: ignore[arg-type]

    def test_envelope_is_frozen(self):
        env = make_inbound(channel="x", text="t")
        with pytest.raises(dataclasses.FrozenInstanceError):
            env.text = "changed"  # type: ignore[misc]

    def test_new_message_id_unique(self):
        assert new_message_id() != new_message_id()
