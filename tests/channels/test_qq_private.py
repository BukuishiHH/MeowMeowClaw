"""QQ 私聊渠道服务测试.

覆盖 M4 关键契约:
- 首次消息创建 active 指针并落盘;
- 6 小时内复用会话, 超过 6 小时归档旧会话 + 新建 + 提示"已开始新对话";
- active 指针跨实例/重启恢复; 会话文件被外部删除时自动轮换;
- 仅本人私聊生效;
- /help /new /clear [id] [--purge] /sessions 命令, 以及跨渠道删除。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

from meowmeowclaw.agent.context import ContextBuilder
from meowmeowclaw.agent.loop import AgentLoop
from meowmeowclaw.channels.base import IncomingMessage
from meowmeowclaw.channels.qq_private import (
    DEFAULT_IDLE_TIMEOUT_MS,
    NEW_CONVERSATION_NOTICE,
    QqPrivateActiveStore,
    QqPrivateService,
)
from meowmeowclaw.conversation import ConversationService
from meowmeowclaw.llm.base import (
    FINISH_REASON_STOP,
    LLMProvider,
    LLMResponse,
)
from meowmeowclaw.memory import JsonlSessionStore, SessionKey
from meowmeowclaw.tools.registry import ToolRegistry

OWNER = "10001"


# --------------------------------------------------------------------- 测试替身


class ScriptedProvider(LLMProvider):
    def __init__(self, answers: Optional[list[str]] = None) -> None:
        self._answers = list(answers or [])
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None, model=None) -> LLMResponse:
        self.calls.append([dict(message) for message in messages])
        if not self._answers:
            return LLMResponse(content="默认回答", finish_reason=FINISH_REASON_STOP)
        return LLMResponse(content=self._answers.pop(0), finish_reason=FINISH_REASON_STOP)


class Clock:
    """可推进的注入时钟(毫秒)."""

    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, delta_ms: int) -> None:
        self.now += delta_ms


@dataclass
class Stack:
    session_store: JsonlSessionStore
    active_store: QqPrivateActiveStore
    conversation: ConversationService
    service: QqPrivateService


def build_stack(
    tmp_path,
    provider: LLMProvider,
    clock: Clock,
    *,
    memory_dir: Optional[Path] = None,
    owner: str = OWNER,
    idle_timeout_ms: int = DEFAULT_IDLE_TIMEOUT_MS,
) -> Stack:
    memory_dir = memory_dir or tmp_path / "memory"
    registry = MagicMock(spec=ToolRegistry)
    registry.get_definitions.return_value = []
    registry.list_tools.return_value = []
    context = MagicMock(spec=ContextBuilder)
    context.build_messages.side_effect = lambda history=None, current_message="": (
        [{"role": "system", "content": "SYS"}]
        + (list(history) if history else [])
        + ([{"role": "user", "content": current_message}] if current_message else [])
    )

    def agent_factory(session_key: SessionKey) -> AgentLoop:
        return AgentLoop(provider=provider, tools=registry, context=context)

    session_store = JsonlSessionStore(memory_dir)
    conversation = ConversationService(session_store, agent_factory)
    active_store = QqPrivateActiveStore(memory_dir)
    service = QqPrivateService(
        conversation,
        active_store,
        owner,
        idle_timeout_ms=idle_timeout_ms,
        clock=clock,
    )
    return Stack(
        session_store=session_store,
        active_store=active_store,
        conversation=conversation,
        service=service,
    )


def qq_message(
    text: str,
    *,
    owner: str = OWNER,
    received_at_ms: Optional[int] = None,
    scope: str = "private",
    message_id: Optional[str] = None,
) -> IncomingMessage:
    return IncomingMessage(
        channel="qq",
        scope=scope,
        conversation_id=owner,
        sender_id=owner,
        text=text,
        message_id=message_id,
        received_at_ms=received_at_ms,
    )


def contact_key(owner: str = OWNER) -> SessionKey:
    return SessionKey(channel="qq", scope="private", conversation_id=owner)


def cli_key(conversation_id: str = "cli-1") -> SessionKey:
    return SessionKey(channel="cli", scope="session", conversation_id=conversation_id)


# ------------------------------------------------------------------ 会话生命周期


class TestSessionLifecycle:
    @pytest.mark.asyncio
    async def test_first_message_creates_active_session(self, tmp_path):
        clock = Clock()
        stack = build_stack(tmp_path, ScriptedProvider(["你好"]), clock)

        replies = await stack.service.handle_incoming(
            qq_message("hi", received_at_ms=clock.now)
        )

        assert [reply.text for reply in replies] == ["你好"]
        active = await stack.active_store.load(contact_key())
        assert active is not None
        session_key = contact_key().with_session(active.session_id)
        assert session_key.canonical.startswith(f"v1:qq:private:{OWNER}:")
        meta = await stack.conversation.get_meta(session_key)
        assert meta is not None
        assert meta.turn_count == 1

    @pytest.mark.asyncio
    async def test_within_timeout_reuses_session(self, tmp_path):
        clock = Clock()
        provider = ScriptedProvider(["答1", "答2"])
        stack = build_stack(tmp_path, provider, clock)

        await stack.service.handle_incoming(qq_message("第一问", received_at_ms=clock.now))
        first_active = await stack.active_store.load(contact_key())
        assert first_active is not None

        clock.advance(60 * 60 * 1000)  # 1 小时
        replies = await stack.service.handle_incoming(
            qq_message("第二问", received_at_ms=clock.now)
        )

        assert [reply.text for reply in replies] == ["答2"]  # 无轮换提示
        second_active = await stack.active_store.load(contact_key())
        assert second_active is not None
        assert second_active.session_id == first_active.session_id

        session_key = contact_key().with_session(first_active.session_id)
        meta = await stack.conversation.get_meta(session_key)
        assert meta is not None
        assert meta.turn_count == 2

        # 第二轮能看到第一轮历史
        second_call = provider.calls[1]
        contents = [
            message.get("content") for message in second_call if message["role"] != "system"
        ]
        assert contents == ["第一问", "答1", "第二问"]

    @pytest.mark.asyncio
    async def test_after_timeout_rotates_with_notice(self, tmp_path):
        clock = Clock()
        stack = build_stack(tmp_path, ScriptedProvider(["答1", "答2"]), clock)

        await stack.service.handle_incoming(qq_message("第一问", received_at_ms=clock.now))
        first_active = await stack.active_store.load(contact_key())
        assert first_active is not None

        clock.advance(DEFAULT_IDLE_TIMEOUT_MS + 1)
        replies = await stack.service.handle_incoming(
            qq_message("第二问", received_at_ms=clock.now)
        )

        assert [reply.text for reply in replies] == [NEW_CONVERSATION_NOTICE, "答2"]
        second_active = await stack.active_store.load(contact_key())
        assert second_active is not None
        assert second_active.session_id != first_active.session_id

        old_meta = await stack.conversation.get_meta(
            contact_key().with_session(first_active.session_id)
        )
        assert old_meta is not None
        assert old_meta.archived is True

    @pytest.mark.asyncio
    async def test_restart_recovers_active_pointer(self, tmp_path):
        clock = Clock()
        memory_dir = tmp_path / "memory"
        first_stack = build_stack(
            tmp_path, ScriptedProvider(["答1"]), clock, memory_dir=memory_dir
        )
        await first_stack.service.handle_incoming(
            qq_message("第一问", received_at_ms=clock.now)
        )
        first_active = await first_stack.active_store.load(contact_key())
        assert first_active is not None

        clock.advance(60 * 60 * 1000)
        provider = ScriptedProvider(["答2"])
        second_stack = build_stack(
            tmp_path, provider, clock, memory_dir=memory_dir
        )

        replies = await second_stack.service.handle_incoming(
            qq_message("第二问", received_at_ms=clock.now)
        )

        assert [reply.text for reply in replies] == ["答2"]  # 未超时 -> 复用
        second_active = await second_stack.active_store.load(contact_key())
        assert second_active is not None
        assert second_active.session_id == first_active.session_id
        contents = [
            message.get("content") for message in provider.calls[0] if message["role"] != "system"
        ]
        assert contents == ["第一问", "答1", "第二问"]

    @pytest.mark.asyncio
    async def test_deleted_active_session_triggers_rotation(self, tmp_path):
        clock = Clock()
        stack = build_stack(tmp_path, ScriptedProvider(["答1", "答2"]), clock)
        await stack.service.handle_incoming(qq_message("第一问", received_at_ms=clock.now))
        first_active = await stack.active_store.load(contact_key())
        assert first_active is not None

        await stack.conversation.purge_session(
            contact_key().with_session(first_active.session_id)
        )
        replies = await stack.service.handle_incoming(
            qq_message("第二问", received_at_ms=clock.now)
        )

        assert [reply.text for reply in replies] == [NEW_CONVERSATION_NOTICE, "答2"]
        second_active = await stack.active_store.load(contact_key())
        assert second_active is not None
        assert second_active.session_id != first_active.session_id

    @pytest.mark.asyncio
    async def test_idle_anchor_uses_user_arrival_time(self, tmp_path):
        clock = Clock()
        stack = build_stack(tmp_path, ScriptedProvider(["答1", "答2"]), clock)
        first_arrival = clock.now

        await stack.service.handle_incoming(qq_message("第一问", received_at_ms=first_arrival))

        clock.advance(10 * 60 * 60 * 1000)  # 本地时钟前进 10 小时
        # 平台时间戳只过了 1 小时 -> 仍复用会话
        replies = await stack.service.handle_incoming(
            qq_message("第二问", received_at_ms=first_arrival + 60 * 60 * 1000)
        )

        assert [reply.text for reply in replies] == ["答2"]

    @pytest.mark.asyncio
    async def test_non_owner_or_group_is_ignored(self, tmp_path):
        stack = build_stack(tmp_path, ScriptedProvider(["答"]), Clock())

        assert await stack.service.handle_incoming(qq_message("hi", owner="other")) == []
        assert await stack.service.handle_incoming(
            qq_message("hi", scope="group")
        ) == []
        assert await stack.service.handle_incoming(qq_message("   ")) == []


# ------------------------------------------------------------------ 命令


class TestCommands:
    @pytest.mark.asyncio
    async def test_help_and_unknown_command(self, tmp_path):
        stack = build_stack(tmp_path, ScriptedProvider(), Clock())

        help_replies = await stack.service.handle_incoming(qq_message("/help"))
        assert len(help_replies) == 1
        assert "/new" in help_replies[0].text

        unknown = await stack.service.handle_incoming(qq_message("/nope"))
        assert "未知命令" in unknown[0].text

    @pytest.mark.asyncio
    async def test_new_command_keeps_old_session(self, tmp_path):
        clock = Clock()
        stack = build_stack(tmp_path, ScriptedProvider(["答1"]), clock)
        await stack.service.handle_incoming(qq_message("第一问", received_at_ms=clock.now))
        first_active = await stack.active_store.load(contact_key())
        assert first_active is not None

        replies = await stack.service.handle_incoming(qq_message("/new"))
        assert [reply.text for reply in replies] == [NEW_CONVERSATION_NOTICE]

        second_active = await stack.active_store.load(contact_key())
        assert second_active is not None
        assert second_active.session_id != first_active.session_id

        old_meta = await stack.conversation.get_meta(
            contact_key().with_session(first_active.session_id)
        )
        assert old_meta is not None
        assert old_meta.archived is False  # /new 不归档旧会话

        summaries = await stack.conversation.list_sessions()
        assert len(summaries) == 1  # 新会话尚未写入消息, 只有旧会话有文件

    @pytest.mark.asyncio
    async def test_clear_archives_current_and_rotates(self, tmp_path):
        clock = Clock()
        stack = build_stack(tmp_path, ScriptedProvider(["答1"]), clock)
        await stack.service.handle_incoming(qq_message("第一问", received_at_ms=clock.now))
        first_active = await stack.active_store.load(contact_key())
        assert first_active is not None

        replies = await stack.service.handle_incoming(qq_message("/clear"))

        assert "已归档当前会话" in replies[0].text
        assert NEW_CONVERSATION_NOTICE in replies[0].text
        old_meta = await stack.conversation.get_meta(
            contact_key().with_session(first_active.session_id)
        )
        assert old_meta is not None
        assert old_meta.archived is True

    @pytest.mark.asyncio
    async def test_clear_specific_session_cross_channel(self, tmp_path):
        clock = Clock()
        provider = ScriptedProvider(["CLI 答", "QQ 答"])
        stack = build_stack(tmp_path, provider, clock)
        target = cli_key()
        await stack.conversation.handle_message(target, "cli 消息")

        replies = await stack.service.handle_incoming(
            qq_message(f"/clear {target.storage_id[:8]}")
        )

        assert "已归档会话" in replies[0].text
        target_meta = await stack.conversation.get_meta(target)
        assert target_meta is not None
        assert target_meta.archived is True

    @pytest.mark.asyncio
    async def test_clear_purge_specific_session(self, tmp_path):
        clock = Clock()
        provider = ScriptedProvider(["CLI 答"])
        stack = build_stack(tmp_path, provider, clock)
        target = cli_key()
        await stack.conversation.handle_message(target, "cli 消息")

        replies = await stack.service.handle_incoming(
            qq_message(f"/clear {target.storage_id[:8]} --purge")
        )

        assert "已永久删除会话" in replies[0].text
        assert await stack.conversation.get_meta(target) is None

    @pytest.mark.asyncio
    async def test_sessions_command_lists_current(self, tmp_path):
        clock = Clock()
        stack = build_stack(tmp_path, ScriptedProvider(["答1"]), clock)
        await stack.service.handle_incoming(qq_message("第一问", received_at_ms=clock.now))
        active = await stack.active_store.load(contact_key())
        assert active is not None

        replies = await stack.service.handle_incoming(qq_message("/sessions"))

        text = replies[0].text
        session_key = contact_key().with_session(active.session_id)
        assert session_key.storage_id[:8] in text
        assert "active" in text
        assert "<- 当前" in text


# ------------------------------------------------------------------ active 存储


class TestActiveStore:
    @pytest.mark.asyncio
    async def test_latest_event_wins_and_clear_resets(self, tmp_path):
        store = QqPrivateActiveStore(tmp_path / "memory")
        contact = contact_key()

        await store.activate(contact, "s1", 100)
        await store.touch(contact, "s1", 200)
        await store.activate(contact, "s2", 300)
        active = await store.load(contact)
        assert active is not None
        assert active.session_id == "s2"
        assert active.at_ms == 300

        await store.clear(contact, 400)
        assert await store.load(contact) is None

        await store.activate(contact, "s3", 500)
        active = await store.load(contact)
        assert active is not None
        assert active.session_id == "s3"

        # 新实例读取同一文件 -> 持久化恢复
        reopened = QqPrivateActiveStore(tmp_path / "memory")
        active = await reopened.load(contact)
        assert active is not None
        assert active.session_id == "s3"

    @pytest.mark.asyncio
    async def test_corrupt_lines_are_skipped(self, tmp_path):
        memory_dir = tmp_path / "memory"
        store = QqPrivateActiveStore(memory_dir)
        contact = contact_key()
        await store.activate(contact, "s1", 100)

        with store.path.open("a", encoding="utf-8") as handle:
            handle.write("not-json\n")

        active = await store.load(contact)
        assert active is not None
        assert active.session_id == "s1"
