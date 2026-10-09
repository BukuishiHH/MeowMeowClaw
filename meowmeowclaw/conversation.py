"""ConversationService: 短期记忆与 AgentLoop 之间的编排层.

职责:
- 同一会话串行: 装载历史 → ``AgentLoop.run_turn`` → 仅把**完整轮次**回写 ``SessionStore``;
- 每会话缓存一个 AgentLoop(同会话的工具调用护栏跨轮生效, 不同会话互不干扰);
- fail-soft: 记忆读取/写入失败只告警, 不阻断用户回复(D22)。

不负责: 会话解析/超时轮换/指令(/sessions、/clear 等), 那些属于 M3/M4 的交付层或
SessionManager; 本服务只接受已经解析好的 ``SessionKey``。
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from meowmeowclaw.agent.loop import AgentLoop, AgentTurn
from meowmeowclaw.memory import (
    MemoryStoreError,
    SessionKey,
    SessionMessage,
    SessionMeta,
    SessionStore,
    SessionSummary,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 50
DEFAULT_MAX_CHARS = 120_000


@dataclass(frozen=True)
class ConversationResult:
    """一次 ``handle_message`` 的结果."""

    answer: str
    completed: bool
    persisted: bool
    turn: AgentTurn


class ConversationService:
    """按会话编排短期记忆的读写.

    Args:
        store: ``SessionStore`` 实现(契约见 memory/store.py)
        agent_factory: 按 SessionKey 创建 AgentLoop 的工厂; 每会话只调用一次并缓存
        max_turns / max_chars: 装载历史时的窗口上限
    """

    def __init__(
        self,
        store: SessionStore,
        agent_factory: Callable[[SessionKey], AgentLoop],
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        if max_turns <= 0:
            raise ValueError("max_turns 必须为正整数")
        if max_chars <= 0:
            raise ValueError("max_chars 必须为正整数")
        self.store = store
        self.agent_factory = agent_factory
        self.max_turns = max_turns
        self.max_chars = max_chars
        # storage_id -> AgentLoop(护栏状态按会话隔离)
        self._agents: dict[str, AgentLoop] = {}
        # storage_id -> 串行锁(装历史/跑模型/回写整段互斥)
        self._locks: dict[str, asyncio.Lock] = {}

    def __repr__(self) -> str:
        return (
            f"<ConversationService sessions={len(self._agents)} "
            f"max_turns={self.max_turns} max_chars={self.max_chars}>"
        )

    # ------------------------------------------------------------------ 内部

    def _lock_for(self, storage_id: str) -> asyncio.Lock:
        lock = self._locks.get(storage_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[storage_id] = lock
        return lock

    def _agent_for(self, key: SessionKey) -> AgentLoop:
        agent = self._agents.get(key.storage_id)
        if agent is None:
            agent = self.agent_factory(key)
            self._agents[key.storage_id] = agent
        return agent

    async def _load_history(self, key: SessionKey) -> list[dict]:
        """从 store 装载最近窗口并投影为 OpenAI messages; 失败降级为空历史."""
        try:
            messages = await self.store.load_recent(
                key, max_turns=self.max_turns, max_chars=self.max_chars
            )
        except MemoryStoreError as exc:
            logger.warning("记忆读取失败, 本次以空历史继续(fail-soft): %s (%r)", key.canonical, exc)
            return []
        return [message.to_llm_message() for message in messages]

    # ------------------------------------------------------------------ 对外

    async def handle_message(
        self,
        key: SessionKey,
        user_message: str,
        *,
        meta: Optional[dict] = None,
    ) -> ConversationResult:
        """
        处理一条用户消息: 装载历史 → 跑一轮 → 完整则回写.

        :param key: 已解析好的会话键
        :param user_message: 用户输入(非空字符串)
        :param meta: 追加到 turn 的元数据(如 channel/sender_id)
        :return: ConversationResult; 记忆故障时 ``persisted=False`` 但仍返回回答
        """
        if not isinstance(key, SessionKey):
            raise TypeError("key 必须是 SessionKey")
        if not isinstance(user_message, str) or not user_message.strip():
            raise ValueError("user_message 不能为空")

        async with self._lock_for(key.storage_id):
            history = await self._load_history(key)
            agent = self._agent_for(key)
            turn = await agent.run_turn(user_message, history=history)

            persisted = False
            if turn.completed and turn.messages:
                try:
                    await self.store.append_turn(
                        key,
                        [SessionMessage.from_llm_message(message) for message in turn.messages],
                        meta=meta,
                    )
                    persisted = True
                except MemoryStoreError as exc:
                    logger.warning(
                        "记忆写入失败, 本轮不落盘但正常回复(fail-soft): %s (%r)",
                        key.canonical,
                        exc,
                    )
            else:
                logger.warning(
                    "本轮未完整结束, 不写入历史: session=%s reason=%s",
                    key.canonical,
                    turn.finish_reason,
                )

            return ConversationResult(
                answer=turn.answer,
                completed=turn.completed,
                persisted=persisted,
                turn=turn,
            )

    async def get_meta(self, key: SessionKey) -> Optional[SessionMeta]:
        return await self.store.get_meta(key)

    async def list_sessions(self, *, include_archived: bool = True) -> list[SessionSummary]:
        return await self.store.list_sessions(include_archived=include_archived)

    async def archive_session(self, key: SessionKey) -> None:
        """``/clear`` 对应的归档."""
        await self.store.archive(key)

    async def purge_session(self, key: SessionKey) -> None:
        """``/clear --purge`` 对应的永久删除."""
        await self.store.purge(key)

    async def close(self) -> None:
        """释放会话缓存; store 的生命周期由调用方(组合根)负责."""
        self._agents.clear()
        self._locks.clear()
