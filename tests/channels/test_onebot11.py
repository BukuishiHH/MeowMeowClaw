"""OneBot 11(NapCat)传输层单元测试.

覆盖: 消息段/CQ 码 -> 纯文本、事件 -> IncomingMessage、超长分片、
反向 WS 服务端(token 鉴权/事件分发/动作发送)、正向 WS 客户端(重连语义)、
``OneBot11Adapter`` 生命周期(stop_inbound 不关连接, 保证在途回复可送达)。
"""

import asyncio
import json
import socket
from typing import Any

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from meowmeowclaw.channels.base import IncomingMessage
from meowmeowclaw.channels.onebot11 import (
    OneBot11Adapter,
    OneBot11Transport,
    chunk_text,
    event_to_incoming,
    extract_text,
    strip_cq_codes,
)
from meowmeowclaw.gateway import AsyncioQueueBus, Envelope, outbound_queue

TOKEN = "meowclaw-test"
PATH = "/onebot/v11/ws"


def free_port() -> int:
    """取一个空闲的本地端口(测试用; 存在极小竞态, 可接受)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def private_event(
    text: str = "你好",
    *,
    user_id: int = 2328428288,
    message_id: int = 1,
    time: int = 1_700_000_000,
    message: Any = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "user_id": user_id,
        "message_id": message_id,
        "time": time,
        "message": [{"type": "text", "data": {"text": text}}] if message is None else message,
        "raw_message": text,
    }
    return payload


async def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# ------------------------------------------------------------------ 纯函数


class TestExtractText:
    def test_array_segments_join_text_and_placeholder(self):
        message = [
            {"type": "text", "data": {"text": "看图 "}},
            {"type": "image", "data": {"file": "x.png"}},
            {"type": "text", "data": {"text": " 谢谢"}},
        ]

        assert extract_text(message) == "看图 [图片] 谢谢"

    def test_cq_string_is_converted(self):
        text = "hi[CQ:face,id=1]there[CQ:at,qq=123]"

        assert extract_text(text) == "hi[表情]there@123"

    def test_cq_at_all(self):
        assert strip_cq_codes("[CQ:at,qq=all]") == "@全体成员"

    @pytest.mark.parametrize("value", [None, "", [], {}])
    def test_empty_inputs(self, value):
        assert extract_text(value) == ""

    def test_unknown_segment_placeholder(self):
        assert extract_text([{"type": "magic", "data": {}}]) == "[非文本消息]"

    def test_plain_string_without_cq(self):
        assert extract_text("纯文本") == "纯文本"


class TestEventToIncoming:
    def test_private_message_round_trip_fields(self):
        incoming = event_to_incoming(private_event("你好", message_id=42, time=1_700_000_000))

        assert isinstance(incoming, IncomingMessage)
        assert incoming.channel == "qq"
        assert incoming.scope == "private"
        assert incoming.conversation_id == "2328428288"
        assert incoming.sender_id == "2328428288"
        assert incoming.text == "你好"
        assert incoming.message_id == "42"
        assert incoming.received_at_ms == 1_700_000_000_000

    def test_raw_message_fallback_when_message_missing(self):
        payload = private_event("hi")
        payload.pop("message")

        assert event_to_incoming(payload).text == "hi"

    def test_group_message_ignored(self):
        payload = private_event("hi")
        payload["message_type"] = "group"

        assert event_to_incoming(payload) is None

    def test_meta_event_ignored(self):
        assert event_to_incoming({"post_type": "meta_event", "meta_event_type": "heartbeat"}) is None

    def test_empty_text_ignored(self):
        assert event_to_incoming(private_event(message=[])) is None

    def test_missing_time_yields_none(self):
        payload = private_event("hi")
        payload.pop("time")

        assert event_to_incoming(payload).received_at_ms is None

    @pytest.mark.parametrize("value", [None, "x", [], 1])
    def test_non_dict_payload_ignored(self, value):
        assert event_to_incoming(value) is None


class TestChunkText:
    def test_split_long_text(self):
        assert chunk_text("abcdefg", 3) == ["abc", "def", "g"]

    def test_short_text_kept(self):
        assert chunk_text("abc", 3) == ["abc"]

    def test_empty_text(self):
        assert chunk_text("", 3) == []

    def test_non_positive_limit_disables_split(self):
        assert chunk_text("abcdefg", 0) == ["abcdefg"]


# ------------------------------------------------------------------ 反向模式


class TestReverseTransport:
    @pytest.mark.asyncio
    async def test_rejects_wrong_token(self):
        port = free_port()
        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
        await transport.start()
        try:
            with pytest.raises(Exception):
                async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token=wrong") as websocket:
                    await asyncio.wait_for(websocket.recv(), timeout=3)
            assert transport.connected is False
        finally:
            await transport.stop()

    @pytest.mark.asyncio
    async def test_accepts_token_and_dispatches_event(self):
        port = free_port()
        received: list[IncomingMessage] = []

        async def handler(message: IncomingMessage) -> bool:
            received.append(message)
            return True

        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN, handler=handler)
        await transport.start()
        try:
            async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token={TOKEN}") as websocket:
                assert await wait_until(lambda: transport.connected)
                await websocket.send(json.dumps(private_event("你好", message_id=7), ensure_ascii=False))
                assert await wait_until(lambda: len(received) == 1)
                assert received[0].text == "你好"
                assert received[0].message_id == "7"
        finally:
            await transport.stop()

    @pytest.mark.asyncio
    async def test_send_private_msg_action_with_segments(self):
        port = free_port()
        transport = OneBot11Transport(
            mode="reverse", port=port, path=PATH, token=TOKEN, max_text_chars=4
        )
        await transport.start()
        try:
            async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token={TOKEN}") as websocket:
                assert await wait_until(lambda: transport.connected)
                envelope = Envelope(
                    kind="reply",
                    channel="qq",
                    target_channel="qq",
                    scope="private",
                    conversation_id="2328428288",
                    text="abcdefgh",
                )
                send_task = asyncio.create_task(transport.send(envelope))

                first = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                assert first["action"] == "send_private_msg"
                assert first["params"]["user_id"] == 2328428288
                assert [item["data"]["text"] for item in first["params"]["message"]] == ["abcd"]
                await websocket.send(json.dumps({"status": "ok", "retcode": 0, "echo": first["echo"]}))

                second = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                assert [item["data"]["text"] for item in second["params"]["message"]] == ["efgh"]
                await websocket.send(json.dumps({"status": "ok", "retcode": 0, "echo": second["echo"]}))
                await asyncio.wait_for(send_task, timeout=5)
        finally:
            await transport.stop()

    @pytest.mark.asyncio
    async def test_send_without_connection_is_soft_failure(self):
        transport = OneBot11Transport(mode="reverse", port=free_port(), path=PATH, token=TOKEN)
        await transport.start()
        try:
            envelope = Envelope(kind="reply", channel="qq", target_channel="qq", text="hi")
            await transport.send(envelope)  # 不应抛异常
        finally:
            await transport.stop()

    @pytest.mark.asyncio
    async def test_stop_inbound_rejects_new_connection(self):
        port = free_port()
        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
        await transport.start()
        try:
            transport.stop_inbound()
            with pytest.raises(Exception):
                async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token={TOKEN}") as websocket:
                    await asyncio.wait_for(websocket.recv(), timeout=3)
        finally:
            await transport.stop()


# ------------------------------------------------------------------ 正向模式


class TestForwardTransport:
    @pytest.mark.asyncio
    async def test_connects_receives_event_and_sends_action(self):
        events: asyncio.Queue = asyncio.Queue()
        actions: list[dict[str, Any]] = []
        received: list[IncomingMessage] = []

        async def handler(websocket) -> None:
            async def pump() -> None:
                while True:
                    await websocket.send(json.dumps(await events.get(), ensure_ascii=False))

            pump_task = asyncio.create_task(pump())
            try:
                async for raw in websocket:
                    payload = json.loads(raw)
                    actions.append(payload)
                    await websocket.send(
                        json.dumps({"status": "ok", "retcode": 0, "echo": payload.get("echo")})
                    )
            finally:
                pump_task.cancel()

        server = await serve(handler, "127.0.0.1", 0, ping_interval=None)
        port = server.sockets[0].getsockname()[1]

        async def on_message(message: IncomingMessage) -> bool:
            received.append(message)
            return True

        transport = OneBot11Transport(
            mode="forward",
            host="127.0.0.1",
            port=port,
            path=PATH,
            token=TOKEN,
            handler=on_message,
            reconnect_min=0.1,
            reconnect_max=0.2,
        )
        await transport.start()
        try:
            assert await wait_until(lambda: transport.connected, timeout=5)
            await events.put(private_event("正向", message_id=9))
            assert await wait_until(lambda: len(received) == 1, timeout=5)
            assert received[0].text == "正向"

            await transport.send(
                Envelope(kind="reply", channel="qq", target_channel="qq", conversation_id="2328428288", text="回复")
            )
            assert await wait_until(lambda: len(actions) == 1, timeout=5)
            assert actions[0]["action"] == "send_private_msg"
            assert actions[0]["params"]["message"][0]["data"]["text"] == "回复"
        finally:
            await transport.stop()
            server.close()
            await server.wait_closed()


# ------------------------------------------------------------------ 适配器生命周期


class TestOneBot11Adapter:
    @pytest.mark.asyncio
    async def test_adapter_delivers_reply_after_stop_inbound(self):
        """优雅关闭契约: stop_inbound 后连接保留, 在途回复仍可送达."""
        port = free_port()
        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
        adapter = OneBot11Adapter(transport)
        bus = AsyncioQueueBus()
        await adapter.start(bus)
        try:
            async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token={TOKEN}") as websocket:
                assert await wait_until(lambda: transport.connected)
                await adapter.stop_inbound()

                envelope = Envelope(
                    kind="reply",
                    channel="qq",
                    target_channel="qq",
                    conversation_id="2328428288",
                    text="在途回复",
                )
                await bus.publish(outbound_queue("qq"), envelope)
                payload = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                assert payload["params"]["message"][0]["data"]["text"] == "在途回复"
                await websocket.send(
                    json.dumps({"status": "ok", "retcode": 0, "echo": payload["echo"]})
                )
        finally:
            await adapter.stop()
            await bus.close()
        assert transport.connected is False

    @pytest.mark.asyncio
    async def test_adapter_start_is_idempotent(self):
        port = free_port()
        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
        adapter = OneBot11Adapter(transport)
        bus = AsyncioQueueBus()
        try:
            await adapter.start(bus)
            await adapter.start(bus)
            assert transport.accepting is True
        finally:
            await adapter.stop()
            await bus.close()

    @pytest.mark.asyncio
    async def test_adapter_start_failure_releases_outbound_loop(self):
        """端口被占用时 start 抛错, 但不留悬挂的出站消费任务."""

        async def noop(websocket) -> None:  # pragma: no cover - 测试不会连它
            return None

        port = free_port()
        blocker = await serve(noop, "127.0.0.1", port, ping_interval=None)
        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
        adapter = OneBot11Adapter(transport)
        bus = AsyncioQueueBus()
        try:
            with pytest.raises(OSError):
                await adapter.start(bus)
        finally:
            blocker.close()
            await blocker.wait_closed()
            await bus.close()
