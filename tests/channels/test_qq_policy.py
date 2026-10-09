"""QqPolicy 策略层测试(等价于原 QqPrivateService 行为的网关侧拆解)."""

import pytest

from meowmeowclaw.channels.base import IncomingMessage
from meowmeowclaw.channels.bridge import incoming_to_envelope
from meowmeowclaw.gateway import ACTION_AGENT, ACTION_IGNORE, ACTION_REPLY

from _qq_helpers import (
    OWNER,
    Clock,
    ScriptedProvider,
    build_policy_stack,
    cli_key,
    contact_key,
    qq_message,
    run_agent,
)


def _envelope(message: IncomingMessage):
    return incoming_to_envelope(message)


class TestIdentity:
    @pytest.mark.asyncio
    async def test_non_owner_group_or_empty_is_ignored(self, tmp_path):
        stack = build_policy_stack(tmp_path, ScriptedProvider(), Clock())

        other = await stack.policy.resolve(_envelope(qq_message("hi", owner="other")))
        group = await stack.policy.resolve(_envelope(qq_message("hi", scope="group")))
        empty = await stack.policy.resolve(_envelope(qq_message("   ")))

        assert other.action == ACTION_IGNORE
        assert group.action == ACTION_IGNORE
        assert empty.action == ACTION_IGNORE


class TestSessionLifecycle:
    @pytest.mark.asyncio
    async def test_first_message_creates_active_session(self, tmp_path):
        clock = Clock()
        stack = build_policy_stack(tmp_path, ScriptedProvider(["你好"]), clock)

        decision = await stack.policy.resolve(
            _envelope(qq_message("hi", received_at_ms=clock.now))
        )

        assert decision.action == ACTION_AGENT
        assert decision.pre_replies == ()
        active = await stack.active_store.load(contact_key())
        assert active is not None
        session_key = contact_key().with_session(active.session_id)
        assert decision.session_key == session_key
        assert decision.text == "hi"
        assert decision.meta == {"channel": "qq", "scope": "private", "sender_id": OWNER}

    @pytest.mark.asyncio
    async def test_within_timeout_reuses_and_touches(self, tmp_path):
        clock = Clock()
        stack = build_policy_stack(tmp_path, ScriptedProvider(["答1", "答2"]), clock)
        first = await stack.policy.resolve(_envelope(qq_message("第一问", received_at_ms=clock.now)))
        await run_agent(stack.conversation, first)
        first_active = await stack.active_store.load(contact_key())
        assert first_active is not None

        clock.advance(60 * 60 * 1000)
        second = await stack.policy.resolve(_envelope(qq_message("第二问", received_at_ms=clock.now)))

        assert second.action == ACTION_AGENT
        assert second.pre_replies == ()
        assert second.session_key == first.session_key
        second_active = await stack.active_store.load(contact_key())
        assert second_active is not None
        assert second_active.at_ms == clock.now

    @pytest.mark.asyncio
    async def test_after_timeout_rotates_with_pre_reply_and_archives(self, tmp_path):
        clock = Clock()
        stack = build_policy_stack(tmp_path, ScriptedProvider(["答1", "答2"]), clock)
        first = await stack.policy.resolve(_envelope(qq_message("第一问", received_at_ms=clock.now)))
        await run_agent(stack.conversation, first)
        first_active = await stack.active_store.load(contact_key())
        assert first_active is not None

        clock.advance(6 * 60 * 60 * 1000 + 1)
        second = await stack.policy.resolve(_envelope(qq_message("第二问", received_at_ms=clock.now)))

        assert [reply.text for reply in second.pre_replies] == ["已开始新对话"]
        assert second.pre_replies[0].target_channel == "qq"
        assert second.pre_replies[0].correlation_id == second.pre_replies[0].reply_to
        second_active = await stack.active_store.load(contact_key())
        assert second_active is not None
        assert second_active.session_id != first_active.session_id
        old_meta = await stack.conversation.get_meta(
            contact_key().with_session(first_active.session_id)
        )
        assert old_meta is not None and old_meta.archived is True

    @pytest.mark.asyncio
    async def test_deleted_active_session_triggers_rotation(self, tmp_path):
        clock = Clock()
        stack = build_policy_stack(tmp_path, ScriptedProvider(["答1", "答2"]), clock)
        first = await stack.policy.resolve(_envelope(qq_message("第一问", received_at_ms=clock.now)))
        await run_agent(stack.conversation, first)
        first_active = await stack.active_store.load(contact_key())
        assert first_active is not None

        await stack.conversation.purge_session(
            contact_key().with_session(first_active.session_id)
        )
        second = await stack.policy.resolve(_envelope(qq_message("第二问", received_at_ms=clock.now)))

        assert [reply.text for reply in second.pre_replies] == ["已开始新对话"]
        second_active = await stack.active_store.load(contact_key())
        assert second_active is not None
        assert second_active.session_id != first_active.session_id

    @pytest.mark.asyncio
    async def test_idle_anchor_uses_platform_arrival_time(self, tmp_path):
        clock = Clock()
        first_arrival = clock.now
        stack = build_policy_stack(tmp_path, ScriptedProvider(["答1", "答2"]), clock)
        first = await stack.policy.resolve(
            _envelope(qq_message("第一问", received_at_ms=first_arrival))
        )
        await run_agent(stack.conversation, first)

        clock.advance(10 * 60 * 60 * 1000)  # 本地时钟前进 10 小时
        second = await stack.policy.resolve(
            _envelope(qq_message("第二问", received_at_ms=first_arrival + 60 * 60 * 1000))
        )

        assert second.pre_replies == ()
        assert second.session_key == first.session_key


class TestCommands:
    @pytest.mark.asyncio
    async def test_help_and_unknown_command(self, tmp_path):
        stack = build_policy_stack(tmp_path, ScriptedProvider(), Clock())
        help_decision = await stack.policy.resolve(_envelope(qq_message("/help")))
        unknown = await stack.policy.resolve(_envelope(qq_message("/nope")))

        assert help_decision.action == ACTION_REPLY
        assert "/new" in help_decision.replies[0].text
        assert unknown.action == ACTION_REPLY
        assert "未知命令" in unknown.replies[0].text

    @pytest.mark.asyncio
    async def test_new_command_keeps_old_session(self, tmp_path):
        clock = Clock()
        stack = build_policy_stack(tmp_path, ScriptedProvider(["答1"]), clock)
        first = await stack.policy.resolve(_envelope(qq_message("第一问", received_at_ms=clock.now)))
        await run_agent(stack.conversation, first)
        first_active = await stack.active_store.load(contact_key())
        assert first_active is not None

        decision = await stack.policy.resolve(_envelope(qq_message("/new")))

        assert decision.action == ACTION_REPLY
        assert decision.replies[0].text == "已开始新对话"
        second_active = await stack.active_store.load(contact_key())
        assert second_active is not None
        assert second_active.session_id != first_active.session_id
        old_meta = await stack.conversation.get_meta(
            contact_key().with_session(first_active.session_id)
        )
        assert old_meta is not None and old_meta.archived is False

    @pytest.mark.asyncio
    async def test_clear_current_archives_and_rotates(self, tmp_path):
        clock = Clock()
        stack = build_policy_stack(tmp_path, ScriptedProvider(["答1"]), clock)
        first = await stack.policy.resolve(_envelope(qq_message("第一问", received_at_ms=clock.now)))
        await run_agent(stack.conversation, first)
        first_active = await stack.active_store.load(contact_key())
        assert first_active is not None

        decision = await stack.policy.resolve(_envelope(qq_message("/clear")))

        assert decision.action == ACTION_REPLY
        assert "已归档当前会话" in decision.replies[0].text
        assert "已开始新对话" in decision.replies[0].text
        old_meta = await stack.conversation.get_meta(
            contact_key().with_session(first_active.session_id)
        )
        assert old_meta is not None and old_meta.archived is True

    @pytest.mark.asyncio
    async def test_clear_cross_channel_and_purge(self, tmp_path):
        stack = build_policy_stack(tmp_path, ScriptedProvider(["CLI 答"]), Clock())
        target = cli_key()
        await stack.conversation.handle_message(target, "cli 消息")

        archived = await stack.policy.resolve(
            _envelope(qq_message(f"/clear {target.storage_id[:8]}"))
        )
        assert "已归档会话" in archived.replies[0].text

        target_meta = await stack.conversation.get_meta(target)
        assert target_meta is not None and target_meta.archived is True

        purged = await stack.policy.resolve(
            _envelope(qq_message(f"/clear {target.storage_id[:8]} --purge"))
        )
        assert "已永久删除会话" in purged.replies[0].text
        assert await stack.conversation.get_meta(target) is None

    @pytest.mark.asyncio
    async def test_sessions_command_marks_current(self, tmp_path):
        clock = Clock()
        stack = build_policy_stack(tmp_path, ScriptedProvider(["答1"]), clock)
        first = await stack.policy.resolve(_envelope(qq_message("第一问", received_at_ms=clock.now)))
        await run_agent(stack.conversation, first)
        active = await stack.active_store.load(contact_key())
        assert active is not None

        decision = await stack.policy.resolve(_envelope(qq_message("/sessions")))

        text = decision.replies[0].text
        session_key = contact_key().with_session(active.session_id)
        assert session_key.storage_id[:8] in text
        assert "[active]" in text
        assert "<- 当前" in text
