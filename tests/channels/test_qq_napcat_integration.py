"""QQ 渠道(OneBot 11 / NapCat)走网关的端到端测试.

真实 ``ConversationService`` / ``JsonlSessionStore`` + ``QqPolicy`` +
``OneBot11Adapter``(反向 WS) + ``Gateway``; Provider 为脚本替身。
测试进程扮演 NapCat 客户端, 覆盖: 私聊往返、非主人/群聊忽略、指令短路、
6h 轮换提示顺序、断线重连后会话续接。
"""

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import pytest
from websockets.asyncio.client import connect

from meowmeowclaw.channels.onebot11 import OneBot11Adapter, OneBot11Transport
from meowmeowclaw.llm.base import (
    FINISH_REASON_STOP,
    LLMProvider,
    LLMResponse,
    StreamFinal,
    StreamTextDelta,
)
from meowmeowclaw.channels.qq_policy import QqPolicy
from meowmeowclaw.channels.qq_private import QqPrivateActiveStore
from meowmeowclaw.gateway import Gateway

from _qq_helpers import OWNER, Clock, ScriptedProvider, build_conversation, contact_key

TOKEN = "meowclaw-test"
PATH = "/onebot/v11/ws"
GROUP_ID = 700001


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def private_event(text: str, *, user_id: str = OWNER, message_id: int = 1, time_ms: int = 1_000_000) -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "user_id": int(user_id),
        "message_id": message_id,
        "time": int(time_ms / 1000),
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
    }


def group_event(text: str, *, user_id: str = OWNER, message_id: int = 1, time_ms: int = 1_000_000) -> dict[str, Any]:
    payload = private_event(text, user_id=user_id, message_id=message_id, time_ms=time_ms)
    payload["message_type"] = "group"
    payload["group_id"] = GROUP_ID
    return payload


class StreamingPartsProvider(LLMProvider):
    """逐条吐增量(每条之间可插入延迟), 最终回答为 parts 拼接."""

    def __init__(self, parts: list[str], *, delay: float = 0.0) -> None:
        self.parts = list(parts)
        self.delay = float(delay)
        self.stream_calls = 0
        self.chat_calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=None) -> LLMResponse:
        self.chat_calls += 1
        raise AssertionError("流式场景不应调用 chat()")

    async def chat_stream(self, messages, tools=None, model=None, max_tokens=None):
        self.stream_calls += 1
        for part in self.parts:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield StreamTextDelta(part)
        yield StreamFinal(
            LLMResponse(content="".join(self.parts), finish_reason=FINISH_REASON_STOP)
        )


@asynccontextmanager
async def napcat_client(port: int, token: str = TOKEN) -> AsyncIterator[Any]:
    """扮演 NapCat: 以客户端身份连接 Agent 的反向 WS 服务端."""
    uri = f"ws://127.0.0.1:{port}{PATH}?access_token={token}"
    async with connect(uri, ping_interval=None, open_timeout=5) as websocket:
        yield websocket


async def send_event(websocket, payload: dict[str, Any]) -> None:
    await websocket.send(json.dumps(payload, ensure_ascii=False))


async def next_action(websocket, timeout: float = 5.0) -> dict[str, Any]:
    return json.loads(await asyncio.wait_for(websocket.recv(), timeout=timeout))


async def ack(websocket, action: dict[str, Any]) -> None:
    await websocket.send(json.dumps({"status": "ok", "retcode": 0, "echo": action.get("echo")}))


async def wait_connected(transport: OneBot11Transport, timeout: float = 5.0) -> bool:
    """等服务端把新连接登记为 active, 避免测试竞态."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if transport.connected:
            return True
        await asyncio.sleep(0.005)
    return transport.connected


def action_texts(action: dict[str, Any]) -> list[str]:
    return [segment["data"]["text"] for segment in action["params"]["message"]]


async def build_gateway(
    tmp_path,
    provider,
    *,
    idle_timeout_ms: int = 6 * 60 * 60 * 1000,
    stream_enabled: bool = False,
    adapter_factory=None,
    conversation_kwargs=None,
):
    conversation = build_conversation(
        tmp_path / "memory", provider, **(conversation_kwargs or {})
    )
    active_store = QqPrivateActiveStore(tmp_path / "memory")
    clock = Clock()
    policy = QqPolicy(conversation, active_store, OWNER, idle_timeout_ms=idle_timeout_ms, clock=clock)
    port = free_port()
    transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
    adapter = adapter_factory(transport) if adapter_factory is not None else OneBot11Adapter(transport)
    gateway = Gateway(
        conversation=conversation, policies={"qq": policy}, stream_enabled=stream_enabled
    )
    await gateway.start(adapters=[adapter])
    return gateway, transport, conversation, active_store, clock


class TestNapCatPrivateRoundTrip:
    @pytest.mark.asyncio
    async def test_private_message_gets_agent_reply(self, tmp_path):
        provider = ScriptedProvider(["你好喵"])
        gateway, transport, conversation, active_store, clock = await build_gateway(tmp_path, provider)
        try:
            async with napcat_client(transport.port) as websocket:
                assert await wait_connected(transport)
                await send_event(websocket, private_event("在吗", time_ms=clock.now))
                action = await next_action(websocket)

                assert action["action"] == "send_private_msg"
                assert action["params"]["user_id"] == int(OWNER)
                assert action_texts(action) == ["你好喵"]
                await ack(websocket, action)

                active = await active_store.load(contact_key())
                assert active is not None
                meta = await conversation.get_meta(contact_key().with_session(active.session_id))
                assert meta is not None and meta.turn_count == 1
        finally:
            await gateway.stop()

    @pytest.mark.asyncio
    async def test_non_owner_is_ignored(self, tmp_path):
        provider = ScriptedProvider(["不应调用"])
        gateway, transport, _, active_store, _ = await build_gateway(tmp_path, provider)
        try:
            async with napcat_client(transport.port) as websocket:
                assert await wait_connected(transport)
                await send_event(websocket, private_event("hi", user_id="999999"))
                with pytest.raises(asyncio.TimeoutError):
                    await next_action(websocket, timeout=0.5)

                assert provider.calls == []
                assert await active_store.load(contact_key()) is None
        finally:
            await gateway.stop()

    @pytest.mark.asyncio
    async def test_group_message_is_ignored(self, tmp_path):
        provider = ScriptedProvider(["不应调用"])
        gateway, transport, _, _, _ = await build_gateway(tmp_path, provider)
        try:
            async with napcat_client(transport.port) as websocket:
                assert await wait_connected(transport)
                await send_event(websocket, group_event("大家好"))
                with pytest.raises(asyncio.TimeoutError):
                    await next_action(websocket, timeout=0.5)

                assert provider.calls == []
        finally:
            await gateway.stop()

    @pytest.mark.asyncio
    async def test_command_short_circuits_agent(self, tmp_path):
        provider = ScriptedProvider(["不应调用"])
        gateway, transport, _, _, _ = await build_gateway(tmp_path, provider)
        try:
            async with napcat_client(transport.port) as websocket:
                assert await wait_connected(transport)
                await send_event(websocket, private_event("/help"))
                action = await next_action(websocket)

                assert action["action"] == "send_private_msg"
                assert "/new" in "".join(action_texts(action))
                assert provider.calls == []
                await ack(websocket, action)
        finally:
            await gateway.stop()

    @pytest.mark.asyncio
    async def test_idle_rotation_notice_precedes_answer(self, tmp_path):
        provider = ScriptedProvider(["答1", "答2"])
        gateway, transport, _, active_store, clock = await build_gateway(tmp_path, provider)
        try:
            async with napcat_client(transport.port) as websocket:
                assert await wait_connected(transport)
                await send_event(websocket, private_event("第一问", message_id=1, time_ms=clock.now))
                first = await next_action(websocket)
                await ack(websocket, first)
                first_active = await active_store.load(contact_key())

                clock.advance(6 * 60 * 60 * 1000 + 5_000)  # +5s: OneBot time 字段只有秒级精度
                await send_event(websocket, private_event("第二问", message_id=2, time_ms=clock.now))

                # 逐条 ack: transport.send 等待动作响应后才继续下一条出站消息
                notice = await next_action(websocket)
                assert action_texts(notice) == ["已开始新对话"]
                await ack(websocket, notice)

                answer = await next_action(websocket)
                assert action_texts(answer) == ["答2"]
                await ack(websocket, answer)

                second_active = await active_store.load(contact_key())
                assert second_active is not None and first_active is not None
                assert second_active.session_id != first_active.session_id
        finally:
            await gateway.stop()

    @pytest.mark.asyncio
    async def test_reconnect_continues_same_session(self, tmp_path):
        provider = ScriptedProvider(["第一次", "第二次"])
        gateway, transport, conversation, active_store, clock = await build_gateway(tmp_path, provider)
        try:
            async with napcat_client(transport.port) as websocket:
                assert await wait_connected(transport)
                await send_event(websocket, private_event("第一问", message_id=1, time_ms=clock.now))
                action = await next_action(websocket)
                await ack(websocket, action)
                first_active = await active_store.load(contact_key())

            # 断开重连(NapCat 自动重连语义): 会话指针不变, 上下文续接
            async with napcat_client(transport.port) as websocket:
                assert await wait_connected(transport)
                await send_event(websocket, private_event("第二问", message_id=2, time_ms=clock.now))
                action = await next_action(websocket)
                assert action_texts(action) == ["第二次"]
                await ack(websocket, action)
                second_active = await active_store.load(contact_key())

            assert first_active is not None and second_active is not None
            assert first_active.session_id == second_active.session_id
            meta = await conversation.get_meta(contact_key().with_session(second_active.session_id))
            assert meta is not None and meta.turn_count == 2
        finally:
            await gateway.stop()


class TestNapCatTransportErrors:
    @pytest.mark.asyncio
    async def test_action_failure_is_logged_not_raised(self, tmp_path):
        provider = ScriptedProvider(["回答"])
        gateway, transport, _, _, clock = await build_gateway(tmp_path, provider)
        try:
            async with napcat_client(transport.port) as websocket:
                assert await wait_connected(transport)
                await send_event(websocket, private_event("你好", time_ms=clock.now))
                action = await next_action(websocket)
                # NapCat 返回失败: 出站循环不应崩溃
                await websocket.send(
                    json.dumps({"status": "failed", "retcode": 100, "message": "发送失败", "echo": action["echo"]})
                )
                await asyncio.sleep(0.1)
                # 再次发消息仍能正常工作
                await send_event(websocket, private_event("再来", message_id=2, time_ms=clock.now))
                action2 = await next_action(websocket)
                assert action2["action"] == "send_private_msg"
        finally:
            await gateway.stop()


# --------------------------------------------------- QQ 流式: typing 指示器(S4)


class TestNapCatTypingIndicator:
    @pytest.mark.asyncio
    async def test_typing_sent_before_reply_and_deltas_do_not_bubble(self, tmp_path):
        """真实链路: start → set_input_status(1); 最终回复 1 条; delta 不产生气泡."""
        provider = ScriptedProvider(["你好呀"], delay=0.2)
        gateway, transport, _, _, clock = await build_gateway(
            tmp_path,
            provider,
            stream_enabled=True,
            adapter_factory=lambda item: OneBot11Adapter(
                item, stream_mode="typing", typing_refresh_seconds=30
            ),
        )
        try:
            async with napcat_client(transport.port) as websocket:
                assert await wait_connected(transport)
                await send_event(websocket, private_event("在吗", time_ms=clock.now))

                typing_frame = await next_action(websocket)
                assert typing_frame["action"] == "set_input_status"
                assert typing_frame["params"] == {"user_id": int(OWNER), "event_type": 1}
                await ack(websocket, typing_frame)

                reply_frame = await next_action(websocket)
                assert reply_frame["action"] == "send_private_msg"
                assert action_texts(reply_frame) == ["你好呀"]  # 单条完整气泡
                await ack(websocket, reply_frame)

                # delta(整段被当作一个增量)不产生额外气泡, typing 也已停止
                with pytest.raises(asyncio.TimeoutError):
                    await next_action(websocket, timeout=0.4)
        finally:
            await gateway.stop()

    @pytest.mark.asyncio
    async def test_typing_disabled_by_config_sends_no_typing(self, tmp_path):
        provider = ScriptedProvider(["回答"], delay=0.1)
        gateway, transport, _, _, clock = await build_gateway(
            tmp_path,
            provider,
            stream_enabled=True,
            adapter_factory=lambda item: OneBot11Adapter(
                item, stream_mode="typing", typing_enabled=False
            ),
        )
        try:
            async with napcat_client(transport.port) as websocket:
                assert await wait_connected(transport)
                await send_event(websocket, private_event("在吗", time_ms=clock.now))

                frame = await next_action(websocket)
                assert frame["action"] == "send_private_msg"  # 直接就是最终回复
                await ack(websocket, frame)
        finally:
            await gateway.stop()

    @pytest.mark.asyncio
    async def test_off_mode_streams_nothing(self, tmp_path):
        provider = ScriptedProvider(["回答"], delay=0.05)
        gateway, transport, _, _, clock = await build_gateway(
            tmp_path,
            provider,
            stream_enabled=True,
            adapter_factory=lambda item: OneBot11Adapter(item, stream_mode="off"),
        )
        try:
            async with napcat_client(transport.port) as websocket:
                assert await wait_connected(transport)
                await send_event(websocket, private_event("在吗", time_ms=clock.now))

                frame = await next_action(websocket)
                assert frame["action"] == "send_private_msg"
                await ack(websocket, frame)
        finally:
            await gateway.stop()


# --------------------------------------------------- QQ 流式: block 分段(S5)


class TestNapCatBlockStreaming:
    @pytest.mark.asyncio
    async def test_block_bubbles_join_to_full_answer_within_cap(self, tmp_path):
        """端到端: 中间气泡 + 最终补发 的拼接必须等于完整回答, 且条数受封顶约束."""
        parts = ["第一句话。", "第二句话。", "第三句话。", "第四句话。", "第五句话。"]
        provider = StreamingPartsProvider(parts, delay=0.05)
        gateway, transport, _, _, _ = await build_gateway(
            tmp_path,
            provider,
            stream_enabled=True,
            # 关掉 Agent 侧的前导宽限, 让每个增量立刻下发(否则短回答会被合并成一次)
            conversation_kwargs={"stream_preamble_grace_chars": 1, "stream_preamble_grace_ms": 1},
            adapter_factory=lambda item: OneBot11Adapter(
                item,
                stream_mode="block",
                typing_enabled=False,
                block_first_chars=6,
                block_min_chars=6,
                block_min_interval=0.01,
                block_max_messages=3,
                block_hard_chars=200,
            ),
        )
        try:
            async with napcat_client(transport.port) as websocket:
                assert await wait_connected(transport)
                await send_event(websocket, private_event("讲点什么", time_ms=1_000_000))

                bubbles: list[str] = []
                while True:
                    try:
                        frame = await next_action(websocket, timeout=1.0)
                    except asyncio.TimeoutError:
                        break
                    if frame["action"] == "send_private_msg":
                        bubbles.append(action_texts(frame)[0])
                    await ack(websocket, frame)
                assert provider.stream_calls == 1  # 确实走了流式
                assert bubbles, "至少应发出 1 条消息"
                full = "".join(parts)
                assert "".join(bubbles) == full  # 不重复、不缺失
                assert len(bubbles) >= 2  # 至少发生一次中间气泡(而不是整段兜底)
                assert len(bubbles) <= 4  # block_max_messages(3) + 最终 1 条
        finally:
            await gateway.stop()

    @pytest.mark.asyncio
    async def test_short_answer_block_mode_single_bubble(self, tmp_path):
        provider = StreamingPartsProvider(["短答。"])
        gateway, transport, _, _, _ = await build_gateway(
            tmp_path,
            provider,
            stream_enabled=True,
            adapter_factory=lambda item: OneBot11Adapter(
                item, stream_mode="block", typing_enabled=False, block_first_chars=50
            ),
        )
        try:
            async with napcat_client(transport.port) as websocket:
                assert await wait_connected(transport)
                await send_event(websocket, private_event("在吗", time_ms=1_000_000))

                frame = await next_action(websocket, timeout=3.0)
                assert frame["action"] == "send_private_msg"
                assert action_texts(frame) == ["短答。"]
                await ack(websocket, frame)
                with pytest.raises(asyncio.TimeoutError):
                    await next_action(websocket, timeout=0.4)
        finally:
            await gateway.stop()
