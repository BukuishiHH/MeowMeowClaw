"""QQ 渠道走网关的端到端对等测试(W2).

使用真实 ConversationService/JsonlSessionStore + QqPolicy + QqAdapter + Gateway,
Provider 替换为脚本替身; 覆盖收发、轮换提示、指令短路与忽略路径。
"""

import pytest

from meowmeowclaw.channels.qq_adapter import QqAdapter
from meowmeowclaw.channels.qq_private import QqPrivateActiveStore
from meowmeowclaw.channels.qq_policy import QqPolicy
from meowmeowclaw.gateway import Gateway

from _qq_helpers import (
    OWNER,
    Clock,
    ScriptedProvider,
    build_conversation,
    contact_key,
    qq_message,
)

CHANNEL = "qq"


async def build_gateway(tmp_path, provider, clock, *, idle_timeout_ms=6 * 60 * 60 * 1000):
    conversation = build_conversation(tmp_path / "memory", provider)
    active_store = QqPrivateActiveStore(tmp_path / "memory")
    policy = QqPolicy(
        conversation,
        active_store,
        OWNER,
        idle_timeout_ms=idle_timeout_ms,
        clock=clock,
    )
    adapter = QqAdapter()
    gateway = Gateway(conversation=conversation, policies={CHANNEL: policy})
    await gateway.start(adapters=[adapter])
    return gateway, adapter, conversation, active_store


class TestQqGatewayParity:
    @pytest.mark.asyncio
    async def test_first_message_round_trip(self, tmp_path):
        clock = Clock()
        gateway, adapter, conversation, active_store = await build_gateway(
            tmp_path, ScriptedProvider(["你好"]), clock
        )

        assert await adapter.feed(qq_message("hi", received_at_ms=clock.now)) is True
        sent = await adapter.wait_sent(1, timeout=2)

        assert [item.text for item in sent] == ["你好"]
        assert sent[0].target_channel == "qq"
        assert sent[0].correlation_id  # reply 与请求配对
        active = await active_store.load(contact_key())
        assert active is not None
        meta = await conversation.get_meta(contact_key().with_session(active.session_id))
        assert meta is not None and meta.turn_count == 1
        await gateway.stop()

    @pytest.mark.asyncio
    async def test_timeout_rotation_notice_then_answer(self, tmp_path):
        clock = Clock()
        provider = ScriptedProvider(["答1", "答2"])
        gateway, adapter, _, active_store = await build_gateway(
            tmp_path, provider, clock
        )

        await adapter.feed(qq_message("第一问", received_at_ms=clock.now))
        await adapter.wait_sent(1, timeout=2)
        first_active = await active_store.load(contact_key())
        assert first_active is not None

        clock.advance(6 * 60 * 60 * 1000 + 1)
        await adapter.feed(qq_message("第二问", received_at_ms=clock.now))
        sent = await adapter.wait_sent(3, timeout=2)

        assert [item.text for item in sent] == ["答1", "已开始新对话", "答2"]
        second_active = await active_store.load(contact_key())
        assert second_active is not None
        assert second_active.session_id != first_active.session_id
        await gateway.stop()

    @pytest.mark.asyncio
    async def test_command_short_circuits_agent(self, tmp_path):
        clock = Clock()
        provider = ScriptedProvider(["不应调用"])
        gateway, adapter, _, _ = await build_gateway(tmp_path, provider, clock)

        await adapter.feed(qq_message("/help"))
        sent = await adapter.wait_sent(1, timeout=2)

        assert "/new" in sent[0].text
        assert provider.calls == []
        await gateway.stop()

    @pytest.mark.asyncio
    async def test_non_owner_is_ignored(self, tmp_path):
        clock = Clock()
        provider = ScriptedProvider(["不应调用"])
        gateway, adapter, _, active_store = await build_gateway(tmp_path, provider, clock)

        await adapter.feed(qq_message("hi", owner="other"))
        await adapter.wait_sent(1, timeout=0.1)

        assert adapter.sent == []
        assert provider.calls == []
        assert await active_store.load(contact_key()) is None
        await gateway.stop()
