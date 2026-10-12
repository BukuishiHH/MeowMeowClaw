"""GatewayDispatcher: 单任务消费 inbound, 去重 + 策略 + 派发(见 docs/GATEWAY_DESIGN.md §7).

- 单读取任务保证到达顺序; 每条 agent 请求一个 task, 跨会话并发;
- 同会话串行由 ``ConversationService`` 的 ``storage_id`` 锁保证(本层不做锁);
- ``Semaphore(max_inflight)`` 限制在途请求数; worker 异常只日志, 不打断循环.
"""

import asyncio
import contextlib
import logging
from typing import Awaitable, Callable, Mapping, Optional

from .bus import INBOUND, GatewayClosedError, MessageBus, outbound_queue
from .dedup import DedupCache
from .envelope import (
    STREAM_PHASE_DELTA,
    STREAM_PHASE_END,
    STREAM_PHASE_START,
    Envelope,
    make_stream_event,
)
from .policy import ACTION_AGENT, ACTION_IGNORE, ACTION_REPLY, ChannelPolicy, PolicyDecision
from .worker import AgentWorker

logger = logging.getLogger(__name__)

GATEWAY_MAX_INFLIGHT = 64


class GatewayDispatcher:
    """入站消息调度器(每个 Gateway 一个)."""

    def __init__(
        self,
        *,
        bus: MessageBus,
        worker: AgentWorker,
        policies: Optional[Mapping[str, ChannelPolicy]] = None,
        dedup: Optional[DedupCache] = None,
        max_inflight: int = GATEWAY_MAX_INFLIGHT,
        shutdown_timeout: float = 10.0,
        stream_enabled: bool = False,
    ) -> None:
        if not isinstance(max_inflight, int) or isinstance(max_inflight, bool) or max_inflight <= 0:
            raise ValueError(f"max_inflight 必须是正整数: {max_inflight!r}")
        if not isinstance(shutdown_timeout, (int, float)) or isinstance(shutdown_timeout, bool):
            raise ValueError(f"shutdown_timeout 必须是正数: {shutdown_timeout!r}")
        if shutdown_timeout <= 0:
            raise ValueError(f"shutdown_timeout 必须是正数: {shutdown_timeout!r}")

        self.bus = bus
        self.worker = worker
        self.policies: dict[str, ChannelPolicy] = dict(policies or {})
        self.dedup = dedup or DedupCache()
        self.max_inflight = max_inflight
        self.shutdown_timeout = float(shutdown_timeout)
        # 流式总开关: false 时不产生任何 KIND_STREAM 信封, 也不给 worker 传 on_delta(零回归)
        self.stream_enabled = bool(stream_enabled)
        self._semaphore = asyncio.Semaphore(max_inflight)
        self._inflight: set[asyncio.Task] = set()
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    def __repr__(self) -> str:
        return (
            f"<GatewayDispatcher policies={sorted(self.policies)} "
            f"inflight={len(self._inflight)} running={self._task is not None} "
            f"stream={self.stream_enabled}>"
        )

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)

    def add_policy(self, policy: ChannelPolicy) -> None:
        channel = str(policy.channel)
        if not channel:
            raise ValueError("policy.channel 不能为空")
        if channel in self.policies:
            raise ValueError(f"渠道策略已注册: {channel}")
        self.policies[channel] = policy

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self.running:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="gateway-dispatcher")
        logger.info("网关 dispatcher 已启动")

    async def stop(self, timeout: Optional[float] = None) -> None:
        """排空 inbound 与 in-flight 后取消读取任务; 幂等."""
        task = self._task
        if task is None:
            return
        self._stopping = True
        budget = self.shutdown_timeout if timeout is None else float(timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, budget)

        try:
            await asyncio.wait_for(self.bus.join(INBOUND), timeout=max(0.0, deadline - loop.time()))
        except asyncio.TimeoutError:
            logger.warning("优雅关闭: inbound 排空超时, 仍有未派发消息")

        remaining = max(0.0, deadline - loop.time())
        pending = set(self._inflight)
        if pending:
            done, still_pending = await asyncio.wait(pending, timeout=remaining)
            for inflight_task in still_pending:
                inflight_task.cancel()
            if still_pending:
                logger.warning("优雅关闭: %d 个在途请求超时取消", len(still_pending))
                await asyncio.gather(*still_pending, return_exceptions=True)

        self._task = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._stopping = False
        logger.info("网关 dispatcher 已停止")

    # ------------------------------------------------------------------ 主循环

    async def _run(self) -> None:
        while not self._stopping:
            try:
                envelope = await self.bus.get(INBOUND)
            except GatewayClosedError:
                return
            try:
                await self._dispatch(envelope)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 单条派发失败不打断循环
                logger.exception(
                    "网关派发失败(单条跳过): channel=%s id=%s",
                    envelope.channel,
                    envelope.message_id,
                )
            finally:
                self.bus.task_done(INBOUND)

    async def _dispatch(self, envelope: Envelope) -> None:
        key = DedupCache.key_for(envelope)
        if key and self.dedup.seen(key):
            logger.info("重复消息已忽略: %s", key)
            return

        policy = self.policies.get(envelope.channel)
        if policy is None:
            logger.warning(
                "无渠道策略, 忽略消息: channel=%s id=%s", envelope.channel, envelope.message_id
            )
            return

        decision = await policy.resolve(envelope)
        if decision.action == ACTION_IGNORE:
            return
        if decision.action == ACTION_REPLY:
            for reply in decision.replies:
                await self._publish_outbound(reply)
            return
        if decision.action == ACTION_AGENT:
            for notice in decision.pre_replies:
                await self._publish_outbound(notice)
            await self._spawn_agent(envelope, decision)
            return
        logger.warning("未知策略裁决: %s", decision.action)  # pragma: no cover - 构造时已校验

    async def _publish_outbound(self, envelope: Envelope) -> None:
        target = envelope.target_channel or envelope.channel
        await self.bus.publish(outbound_queue(target), envelope)

    # ------------------------------------------------------------------ 流式(S2)

    async def _spawn_agent(self, request: Envelope, decision: PolicyDecision) -> None:
        await self._semaphore.acquire()
        on_delta: Optional[Callable[[str], Awaitable[None]]] = None
        if self.stream_enabled:
            # start 只是"即将开始生成"的提示(如 QQ 正在输入), 失败不影响本轮
            await self._publish_stream_safe(request, STREAM_PHASE_START)
            on_delta = self._make_delta_sink(request)
        task = asyncio.create_task(
            self._handle_agent(request, decision, on_delta=on_delta),
            name=f"gateway-agent-{request.channel}-{request.message_id[:8]}",
        )
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    def _make_delta_sink(self, request: Envelope) -> Callable[[str], Awaitable[None]]:
        """构造 per-request 的增量回调(带单调 seq, 便于排障与乱序断言)."""
        seq = 0

        async def on_delta(text: str) -> None:
            nonlocal seq
            seq += 1
            await self._publish_delta(request, text, seq)

        return on_delta

    async def _publish_delta(self, request: Envelope, text: str, seq: int) -> None:
        """delta 走 best-effort: 队列满/总线关闭直接丢弃, 不写死信, 不影响本轮."""
        envelope = make_stream_event(request, phase=STREAM_PHASE_DELTA, text=text, seq=seq)
        target = outbound_queue(envelope.target_channel or envelope.channel)
        try:
            publish_best_effort = getattr(self.bus, "publish_best_effort", None)
            if publish_best_effort is not None:
                await publish_best_effort(target, envelope)
            else:  # 自定义总线未实现 best-effort 时退化为普通发布
                await self.bus.publish(target, envelope)
        except GatewayClosedError:
            logger.debug("总线已关闭, 丢弃流式增量: correlation=%s", envelope.correlation_id)

    async def _publish_stream(self, request: Envelope, phase: str) -> None:
        """start/end 相位: 与 reply 同级别的强语义."""
        await self._publish_outbound(make_stream_event(request, phase=phase))

    async def _publish_stream_safe(self, request: Envelope, phase: str) -> None:
        try:
            await self._publish_stream(request, phase)
        except Exception:  # noqa: BLE001 控制相位失败只告警, 不能影响 reply
            logger.exception("流式 %s 相位发布失败(已忽略): id=%s", phase, request.message_id)

    async def _handle_agent(
        self,
        request: Envelope,
        decision: PolicyDecision,
        *,
        on_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        try:
            reply: Optional[Envelope] = None
            try:
                reply = await self.worker.handle(request, decision, on_delta=on_delta)
            except asyncio.CancelledError:
                raise
            # G10: 内部异常只日志, 不回错误文案
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Agent 处理失败(仅日志, 不回错误文案): channel=%s id=%s",
                    request.channel,
                    request.message_id,
                )
            if self.stream_enabled:
                # end 必须在 reply 之前: 渠道据此停止"正在输入"等状态
                await self._publish_stream_safe(request, STREAM_PHASE_END)
            if reply is not None:
                await self._publish_outbound(reply)
        finally:
            self._semaphore.release()
