"""CliPolicy 策略层测试."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from meowmeowclaw.channels.base import IncomingMessage
from meowmeowclaw.channels.bridge import incoming_to_envelope
from meowmeowclaw.channels.cli_policy import CliPolicy, session_short_id
from meowmeowclaw.gateway import ACTION_AGENT, ACTION_IGNORE, ACTION_REPLY
from meowmeowclaw.memory import SessionKey
from meowmeowclaw.tools.registry import ToolRegistry

from _qq_helpers import ScriptedProvider, build_conversation


class FakeCatalog:
    def __init__(self, skills=()) -> None:
        self._skills = list(skills)

    def skills(self):
        return list(self._skills)

    def __len__(self) -> int:
        return len(self._skills)


def make_tools() -> MagicMock:
    registry = MagicMock(spec=ToolRegistry)
    registry.get_definitions.return_value = [
        {"type": "function", "function": {"name": "read_file", "description": "读取文件"}}
    ]
    return registry


def make_policy(tmp_path, provider, *, tools=None, catalog=None, session_ids=("cli-1", "cli-2", "cli-3")):
    conversation = build_conversation(tmp_path / "memory", provider)
    iterator = iter(session_ids)

    def factory() -> SessionKey:
        return SessionKey(channel="cli", scope="session", conversation_id=next(iterator))

    policy = CliPolicy(
        conversation,
        tools=tools,
        catalog=catalog,
        session_factory=factory,
    )
    return policy, conversation


def cli_message(text: str, message_id: str = "m-1") -> IncomingMessage:
    return IncomingMessage(
        channel="cli",
        scope="session",
        conversation_id="cli",
        sender_id="cli",
        text=text,
        message_id=message_id,
    )


def envelope(text: str, message_id: str = "m-1"):
    return incoming_to_envelope(cli_message(text, message_id))


class TestResolve:
    @pytest.mark.asyncio
    async def test_empty_text_ignored(self, tmp_path):
        policy, _ = make_policy(tmp_path, ScriptedProvider())
        decision = await policy.resolve(envelope("   "))
        assert decision.action == ACTION_IGNORE

    @pytest.mark.asyncio
    async def test_plain_text_uses_current_session(self, tmp_path):
        policy, _ = make_policy(tmp_path, ScriptedProvider(["答"]))
        decision = await policy.resolve(envelope("你好"))

        assert decision.action == ACTION_AGENT
        assert decision.session_key == policy.current_session
        assert decision.text == "你好"
        assert decision.meta == {"channel": "cli", "scope": "session"}
        assert decision.pre_replies == ()


class TestCommands:
    @pytest.mark.asyncio
    async def test_help_and_unknown(self, tmp_path):
        policy, _ = make_policy(tmp_path, ScriptedProvider())
        help_decision = await policy.resolve(envelope("/help"))
        unknown = await policy.resolve(envelope("/nope"))

        assert help_decision.action == ACTION_REPLY
        assert "/new" in help_decision.replies[0].text
        assert unknown.action == ACTION_REPLY
        assert "未知命令或参数" in unknown.replies[0].text

    @pytest.mark.asyncio
    async def test_tools_and_skills(self, tmp_path):
        catalog = FakeCatalog(
            [
                SimpleNamespace(
                    name="exec", dir_name="exec", description="执行命令"
                )
            ]
        )
        policy, _ = make_policy(
            tmp_path, ScriptedProvider(), tools=make_tools(), catalog=catalog
        )

        tools_decision = await policy.resolve(envelope("/tools"))
        skills_decision = await policy.resolve(envelope("/skills"))

        assert "已注册工具(1 个)" in tools_decision.replies[0].text
        assert "read_file" in tools_decision.replies[0].text
        assert "已发现技能(1 个)" in skills_decision.replies[0].text
        assert "exec" in skills_decision.replies[0].text

    @pytest.mark.asyncio
    async def test_new_switches_session(self, tmp_path):
        policy, _ = make_policy(tmp_path, ScriptedProvider(["答1"]))
        first = policy.current_session

        decision = await policy.resolve(envelope("/new"))

        assert decision.action == ACTION_REPLY
        assert "已开始新会话" in decision.replies[0].text
        assert policy.current_session != first
        assert session_short_id(policy.current_session) in decision.replies[0].text

    @pytest.mark.asyncio
    async def test_sessions_marks_current_after_chat(self, tmp_path):
        provider = ScriptedProvider(["答1"])
        policy, conversation = make_policy(tmp_path, provider)
        decision = await policy.resolve(envelope("你好"))
        await conversation.handle_message(decision.session_key, decision.text, meta=decision.meta)

        sessions_decision = await policy.resolve(envelope("/sessions"))

        text = sessions_decision.replies[0].text
        assert session_short_id(policy.current_session) in text
        assert "active" in text
        assert "*" in text

    @pytest.mark.asyncio
    async def test_clear_empty_session_just_switches(self, tmp_path):
        policy, _ = make_policy(tmp_path, ScriptedProvider())
        first = policy.current_session

        decision = await policy.resolve(envelope("/clear"))

        assert "当前会话为空, 已开始新会话" in decision.replies[0].text
        assert policy.current_session != first

    @pytest.mark.asyncio
    async def test_clear_current_archives_and_switches(self, tmp_path):
        provider = ScriptedProvider(["答1"])
        policy, conversation = make_policy(tmp_path, provider)
        decision = await policy.resolve(envelope("你好"))
        await conversation.handle_message(decision.session_key, decision.text, meta=decision.meta)
        first = policy.current_session

        cleared = await policy.resolve(envelope("/clear"))

        assert "已归档会话" in cleared.replies[0].text
        assert "已开始新会话" in cleared.replies[0].text
        meta = await conversation.get_meta(first)
        assert meta is not None and meta.archived is True
        assert policy.current_session != first

    @pytest.mark.asyncio
    async def test_clear_cross_channel_and_purge(self, tmp_path):
        provider = ScriptedProvider(["QQ 答"])
        policy, conversation = make_policy(tmp_path, provider)
        target = SessionKey(channel="qq", scope="private", conversation_id="10001")
        await conversation.handle_message(target, "qq 消息")

        archived = await policy.resolve(envelope(f"/clear {target.storage_id[:8]}"))
        assert "已归档会话" in archived.replies[0].text
        meta = await conversation.get_meta(target)
        assert meta is not None and meta.archived is True

        purged = await policy.resolve(envelope(f"/clear {target.storage_id[:8]} --purge"))
        assert "已永久删除会话" in purged.replies[0].text
        assert await conversation.get_meta(target) is None

    @pytest.mark.asyncio
    async def test_clear_usage_error(self, tmp_path):
        policy, _ = make_policy(tmp_path, ScriptedProvider())
        decision = await policy.resolve(envelope("/clear a b"))
        assert "未知命令或参数" in decision.replies[0].text
