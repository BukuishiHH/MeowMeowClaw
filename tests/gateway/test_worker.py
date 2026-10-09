"""AgentWorker 契约测试."""

import pytest

from meowmeowclaw.gateway import (
    AgentWorker,
    PolicyDecision,
    make_inbound,
)

from _helpers import StubConversation, session_key


class TestAgentWorker:
    @pytest.mark.asyncio
    async def test_reply_envelope_and_meta(self):
        conversation = StubConversation(answer="最终回答", persisted=True, completed=True)
        worker = AgentWorker(conversation)  # type: ignore[arg-type]
        request = make_inbound(
            channel="qq", text="问题", conversation_id="10001", sender_id="10001", message_id="m-1"
        )
        decision = PolicyDecision.agent(
            session_key(conversation_id="10001", channel="qq"),
            "问题",
            meta={"channel": "qq", "scope": "private", "sender_id": "10001"},
        )

        reply = await worker.handle(request, decision)

        assert reply.text == "最终回答"
        assert reply.target_channel == "qq"
        assert reply.correlation_id == "m-1"
        assert reply.session_id == decision.session_key.session_id
        assert reply.metadata["completed"] is True
        assert reply.metadata["persisted"] is True
        assert conversation.calls[0]["text"] == "问题"
        assert conversation.calls[0]["meta"] == {
            "channel": "qq",
            "scope": "private",
            "sender_id": "10001",
        }

    @pytest.mark.asyncio
    async def test_completed_false_still_returns_reply(self):
        conversation = StubConversation(answer="出错了", completed=False, persisted=False)
        worker = AgentWorker(conversation)  # type: ignore[arg-type]
        request = make_inbound(channel="cli", text="q")
        decision = PolicyDecision.agent(session_key(channel="cli"), "q")

        reply = await worker.handle(request, decision)

        assert reply.text == "出错了"
        assert reply.metadata["completed"] is False
        assert reply.metadata["persisted"] is False
