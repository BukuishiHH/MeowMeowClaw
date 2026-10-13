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
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from meowmeowclaw.agent.loop import AgentLoop, AgentTurn
from meowmeowclaw.memory import (
    MemoryStoreError,
    SessionKey,
    SessionMessage,
    SessionMeta,
    SessionStore,
    SessionSummary,
)
from meowmeowclaw.subagent.models import SubagentStats

logger = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 50
DEFAULT_MAX_CHARS = 120_000
DEFAULT_MAX_CACHED_SESSIONS = 256
DEFAULT_CACHE_IDLE_SECONDS = 0.0


def current_rss_mb() -> Optional[float]:
    """当前进程常驻内存峰值(MB); 平台不支持(如 Windows 无 ``resource``)时返回 None.

    - Linux: ``ru_maxrss`` 单位 KB; macOS: 单位字节(见 ``sys.platform`` 分支);
    - 只用于观测, 失败一律不影响主流程。
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows 无 resource 模块
        return None
    try:
        value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except Exception:  # noqa: BLE001 观测失败不影响主流程
        return None
    if sys.platform == "darwin":
        return round(value / (1024 * 1024), 2)
    return round(value / 1024, 2)


@dataclass(frozen=True)
class ConversationResult:
    """一次 ``handle_message`` 的结果."""

    answer: str
    completed: bool
    persisted: bool
    turn: AgentTurn


@dataclass
class _SessionRuntime:
    """会话运行时(A4): AgentLoop + 串行锁 + 引用计数/淘汰状态.

    - ``agent``/``lock`` 同生共死, 不会出现"锁被替换而 agent 还在用"的并发窗口;
    - ``refcount``: ``_acquire`` 同步 +1, ``handle_message`` finally ``_release`` -1,
      排队等待锁的任务也被计数, 因此 ``refcount > 0`` 时绝不淘汰;
    - ``last_used_ms``: 最近一次 ``_acquire`` 的单调时钟毫秒(供 TTL / LRU);
    - ``evict_pending``: ``close_session`` 命中在途运行时时置位, 由最后的 ``_release`` 完成淘汰。
    """

    agent: AgentLoop
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    refcount: int = 0
    last_used_ms: int = 0
    evict_pending: bool = False


class ConversationService:
    """按会话编排短期记忆的读写.

    Args:
        store: ``SessionStore`` 实现(契约见 memory/store.py)
        agent_factory: 按 SessionKey 创建 AgentLoop 的工厂; 每会话只调用一次并缓存
        max_turns / max_chars: 装载历史时的窗口上限
        max_cached_sessions: 运行时缓存容量上限(<=0 视为不限制); 超限时按 LRU 淘汰空闲运行时
        cache_idle_seconds: 空闲 TTL 秒数(<=0 关闭自动 TTL); 显式 ``evict_idle()`` 不受此限制
        subagent_stats: 子代理观测计数器(SA5); None 时使用 enabled=False 空快照
    """

    def __init__(
        self,
        store: SessionStore,
        agent_factory: Callable[[SessionKey], AgentLoop],
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_cached_sessions: int = DEFAULT_MAX_CACHED_SESSIONS,
        cache_idle_seconds: float = DEFAULT_CACHE_IDLE_SECONDS,
        subagent_stats: Optional[SubagentStats] = None,
    ) -> None:
        if max_turns <= 0:
            raise ValueError("max_turns 必须为正整数")
        if max_chars <= 0:
            raise ValueError("max_chars 必须为正整数")
        self.store = store
        self.agent_factory = agent_factory
        self.max_turns = max_turns
        self.max_chars = max_chars
        self.max_cached_sessions = int(max_cached_sessions)
        self.cache_idle_seconds = float(cache_idle_seconds)
        # SA5: None = 子代理未启用, stats 时返回 enabled=False 空快照(不提前构造对象)
        self._subagent_stats: Optional[SubagentStats] = subagent_stats
        # storage_id -> _SessionRuntime(agent + lock 一起管理, 按 last_used 排序供 LRU)
        self._runtimes: "OrderedDict[str, _SessionRuntime]" = OrderedDict()
        # A1 观测: 会话运行时被淘汰的次数(A4 的生命周期 API 会递增)
        self._evictions = 0

    def __repr__(self) -> str:
        return (
            f"<ConversationService sessions={len(self._runtimes)} "
            f"max_turns={self.max_turns} max_chars={self.max_chars}>"
        )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _now_ms() -> int:
        """单调时钟毫秒; 只用于 TTL/LRU 相对比较, 不受系统时间回拨影响."""
        return time.monotonic_ns() // 1_000_000

    def _is_expired(self, runtime: _SessionRuntime, ttl_seconds: float) -> bool:
        return (
            ttl_seconds > 0
            and self._now_ms() - runtime.last_used_ms > ttl_seconds * 1000.0
        )

    def _acquire(self, key: SessionKey) -> _SessionRuntime:
        """取/建会话运行时并同步 +1; 排队与执行期都计数, 淘汰只见 refcount==0."""
        runtime = self._runtimes.get(key.storage_id)
        if (
            runtime is not None
            and runtime.refcount == 0
            and self._is_expired(runtime, self.cache_idle_seconds)
        ):
            # 请求命中的运行时已过 TTL: 先淘汰再重建, 保证自动 TTL 语义
            self._evict_runtime(key.storage_id, runtime)
            runtime = None
        if runtime is None:
            runtime = _SessionRuntime(agent=self.agent_factory(key))
            self._runtimes[key.storage_id] = runtime
        else:
            self._runtimes.move_to_end(key.storage_id)  # LRU: 最近使用挪到队尾
        runtime.refcount += 1
        runtime.last_used_ms = self._now_ms()
        if self.cache_idle_seconds > 0:
            # 顺带清理其他已过期的空闲运行时(在途的不动)
            self._sweep_expired(self.cache_idle_seconds)
        # 先把新运行时计入 refcount, 再按容量回收空闲的旧运行时(绝不淘汰在途)
        self._enforce_capacity()
        return runtime

    def _release(self, key: SessionKey, runtime: _SessionRuntime) -> None:
        runtime.refcount = max(0, runtime.refcount - 1)
        if runtime.refcount == 0 and runtime.evict_pending:
            self._evict_runtime(key.storage_id, runtime)
        self._enforce_capacity()
        if self.cache_idle_seconds > 0:
            self._sweep_expired(self.cache_idle_seconds)

    def _sweep_expired(self, ttl_seconds: float) -> int:
        """淘汰空闲超过 TTL 的运行时; 只处理 ``refcount == 0``, 返回数量."""
        if ttl_seconds <= 0:
            return 0
        victims = [
            storage_id
            for storage_id, runtime in list(self._runtimes.items())
            if runtime.refcount == 0 and self._is_expired(runtime, ttl_seconds)
        ]
        for storage_id in victims:
            self._evict_runtime(storage_id)
        return len(victims)

    def _evict_runtime(
        self, storage_id: str, runtime: Optional[_SessionRuntime] = None
    ) -> bool:
        """淘汰指定运行时; ``runtime`` 非空时校验身份, 防止误淘汰新建的同名运行时."""
        current = self._runtimes.get(storage_id)
        if current is None or (runtime is not None and current is not runtime):
            return False
        self._runtimes.pop(storage_id, None)
        self._evictions += 1
        logger.debug("淘汰会话运行时: %s", storage_id)
        return True

    def _enforce_capacity(self) -> int:
        """容量超限时按 LRU 淘汰空闲运行时; 全部在途则本轮不淘汰."""
        if self.max_cached_sessions <= 0:
            return 0
        evicted = 0
        while len(self._runtimes) > self.max_cached_sessions:
            victim = next(
                (storage_id for storage_id, rt in self._runtimes.items() if rt.refcount == 0),
                None,
            )
            if victim is None:
                break
            self._evict_runtime(victim)
            evicted += 1
        return evicted

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
        on_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> ConversationResult:
        """
        处理一条用户消息: 装载历史 → 跑一轮 → 完整则回写.

        :param key: 已解析好的会话键
        :param user_message: 用户输入(非空字符串)
        :param meta: 追加到 turn 的元数据(如 channel/sender_id)
        :param on_delta: 可选的流式增量回调, 透传给 ``AgentLoop.run_turn``;
            None(默认)时完全走非流式路径, 行为与现状一致
        :return: ConversationResult; 记忆故障时 ``persisted=False`` 但仍返回回答
        """
        if not isinstance(key, SessionKey):
            raise TypeError("key 必须是 SessionKey")
        if not isinstance(user_message, str) or not user_message.strip():
            raise ValueError("user_message 不能为空")

        runtime = self._acquire(key)
        try:
            async with runtime.lock:
                history = await self._load_history(key)
                turn = await runtime.agent.run_turn(
                    user_message, history=history, on_delta=on_delta
                )

                persisted = False
                if turn.completed and turn.messages:
                    try:
                        await self.store.append_turn(
                            key,
                            [
                                SessionMessage.from_llm_message(message)
                                for message in turn.messages
                            ],
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
        finally:
            self._release(key, runtime)

    async def get_meta(self, key: SessionKey) -> Optional[SessionMeta]:
        return await self.store.get_meta(key)

    async def list_sessions(self, *, include_archived: bool = True) -> list[SessionSummary]:
        return await self.store.list_sessions(include_archived=include_archived)

    async def archive_session(self, key: SessionKey) -> None:
        """``/clear`` 对应的归档; 成功后释放该会话运行时."""
        await self.store.archive(key)
        await self.close_session(key)

    async def purge_session(self, key: SessionKey) -> None:
        """``/clear --purge`` 对应的永久删除; 成功后释放该会话运行时."""
        await self.store.purge(key)
        await self.close_session(key)

    # ---------------------------------------------------------- 生命周期(A4)

    async def close_session(self, key: SessionKey) -> bool:
        """释放指定会话运行时(A4, 见 docs/A_PLUS_OPTIMIZATION.md §3.4).

        - 空闲: 立即淘汰, 返回 ``True``;
        - 在途(持锁/排队/执行中): 标记 ``evict_pending``, 由最后一次 ``_release`` 完成淘汰,
          返回 ``False``; 淘汰永远不会替换正在被引用的锁/agent;
        - 运行时不存在: 返回 ``False``(幂等).
        """
        if not isinstance(key, SessionKey):
            raise TypeError("key 必须是 SessionKey")
        runtime = self._runtimes.get(key.storage_id)
        if runtime is None:
            return False
        if runtime.refcount > 0:
            runtime.evict_pending = True
            return False
        return self._evict_runtime(key.storage_id, runtime)

    async def evict_idle(self, ttl_seconds: Optional[float] = None) -> int:
        """按 ``last_used_ms`` 淘汰空闲运行时, 返回淘汰数量(A4).

        - ``ttl_seconds`` 为 ``None`` 时使用构造参数 ``cache_idle_seconds``;
        - ``<= 0``(含默认 0)表示显式清理: 淘汰全部空闲运行时;
        - 只淘汰 ``refcount == 0`` 的运行时, 在途会话不受影响。
        """
        ttl = self.cache_idle_seconds if ttl_seconds is None else float(ttl_seconds)
        if ttl <= 0:
            # 显式清理: ttl<=0(默认 0)表示立即淘汰全部空闲运行时
            victims = [
                storage_id
                for storage_id, runtime in list(self._runtimes.items())
                if runtime.refcount == 0
            ]
            for storage_id in victims:
                self._evict_runtime(storage_id)
            return len(victims)
        return self._sweep_expired(ttl)

    def stats(self) -> dict[str, Any]:
        """运行时观测快照(A1/A4/SA5, 见 docs/A_PLUS_OPTIMIZATION.md §3.1/§3.4, docs/SUBAGENT_DESIGN.md §8.1).

        ``{"cached_agents": int, "locks": int, "evictions": int,
        "store": <SessionStore.stats() 或 {}>, "rss_mb": float | None,
        "subagents": <SubagentStats.snapshot()>}``

        - ``cached_agents`` / ``locks``: 当前 ``_SessionRuntime`` 数量(agent 与 lock 一一对应);
        - ``evictions``: 生命周期 API 累计淘汰的运行时数;
        - ``subagents``: 子代理观测(未启用时为 enabled=False 的全零快照);
        - 纯观测: 任何取值失败都只降级为 ``{}`` / ``None`` / 空快照, 不影响会话主流程。
        """
        store_stats: dict[str, Any] = {}
        store_stats_fn = getattr(self.store, "stats", None)
        if callable(store_stats_fn):
            try:
                store_stats = dict(store_stats_fn())
            except Exception:  # noqa: BLE001 观测失败不影响主流程
                logger.warning("读取 store.stats() 失败(已忽略)", exc_info=True)

        try:
            if self._subagent_stats is None:
                subagent_stats = SubagentStats(enabled=False).snapshot()
            else:
                subagent_stats = self._subagent_stats.snapshot()
        except Exception:  # noqa: BLE001 观测失败不影响主流程
            logger.warning("读取 SubagentStats.snapshot() 失败(已忽略)", exc_info=True)
            subagent_stats = SubagentStats(enabled=False).snapshot()

        return {
            "cached_agents": len(self._runtimes),
            "locks": len(self._runtimes),
            "evictions": self._evictions,
            "store": store_stats,
            "subagents": subagent_stats,
            "rss_mb": current_rss_mb(),
        }

    async def close(self) -> None:
        """释放全部会话运行时; store 的生命周期由调用方(组合根)负责."""
        self._runtimes.clear()
