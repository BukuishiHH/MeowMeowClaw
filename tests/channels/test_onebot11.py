"""OneBot 11(NapCat)传输层单元测试.

覆盖: 消息段/CQ 码 -> 纯文本、事件 -> IncomingMessage、超长分片、
反向 WS 服务端(token 鉴权/事件分发/动作发送)、正向 WS 客户端(重连语义)、
``OneBot11Adapter`` 生命周期(stop_inbound 不关连接, 保证在途回复可送达)。
"""

import asyncio
import json
import socket
from contextlib import suppress
from typing import Any, Optional

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
from meowmeowclaw.gateway import (
    STREAM_PHASE_DELTA,
    STREAM_PHASE_END,
    STREAM_PHASE_START,
    AsyncioQueueBus,
    Envelope,
    make_inbound,
    make_reply,
    make_stream_event,
    outbound_queue,
)

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


# ------------------------------------------------------------------ 输入状态(S4)


class TestSetInputStatus:
    @pytest.mark.asyncio
    async def test_sends_action_with_expected_params(self):
        port = free_port()
        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
        await transport.start()
        try:
            async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token={TOKEN}") as websocket:
                assert await wait_until(lambda: transport.connected)

                task = asyncio.create_task(transport.set_input_status("2328428288"))
                payload = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                assert payload["action"] == "set_input_status"
                assert payload["params"] == {"user_id": 2328428288, "event_type": 1}
                await websocket.send(
                    json.dumps({"status": "ok", "retcode": 0, "echo": payload["echo"]})
                )

                assert await asyncio.wait_for(task, timeout=5) is True
        finally:
            await transport.stop()

    @pytest.mark.asyncio
    async def test_custom_event_type_and_string_user_id(self):
        port = free_port()
        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
        await transport.start()
        try:
            async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token={TOKEN}") as websocket:
                assert await wait_until(lambda: transport.connected)

                task = asyncio.create_task(
                    transport.set_input_status("not-a-number", event_type=2)
                )
                payload = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                assert payload["params"] == {"user_id": "not-a-number", "event_type": 2}
                await websocket.send(
                    json.dumps({"status": "ok", "retcode": 0, "echo": payload["echo"]})
                )
                assert await asyncio.wait_for(task, timeout=5) is True
        finally:
            await transport.stop()

    @pytest.mark.asyncio
    async def test_without_connection_returns_false(self):
        transport = OneBot11Transport(mode="reverse", port=free_port(), path=PATH, token=TOKEN)

        assert await transport.set_input_status("2328428288") is False

    @pytest.mark.asyncio
    async def test_napcat_failure_returns_false_without_raising(self):
        port = free_port()
        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
        await transport.start()
        try:
            async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token={TOKEN}") as websocket:
                assert await wait_until(lambda: transport.connected)

                task = asyncio.create_task(transport.set_input_status("2328428288"))
                payload = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                # NapCat 返回失败(例如旧版本不支持该扩展接口)
                await websocket.send(
                    json.dumps(
                        {
                            "status": "failed",
                            "retcode": 1404,
                            "message": "unknown action",
                            "echo": payload["echo"],
                        }
                    )
                )

                assert await asyncio.wait_for(task, timeout=5) is False
        finally:
            await transport.stop()


class TestAdapterIgnoresDeltas:
    @pytest.mark.asyncio
    async def test_delta_produces_no_message_but_start_sends_typing(self):
        """核心防线: delta 绝不逐条发消息(否则就是"一字一气泡")."""
        port = free_port()
        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
        adapter = OneBot11Adapter(transport, stream_mode="typing", typing_refresh_seconds=30)
        await transport.start()
        try:
            async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token={TOKEN}") as websocket:
                assert await wait_until(lambda: transport.connected)
                request = Envelope(
                    kind="request",
                    channel="qq",
                    target_channel="qq",
                    conversation_id="2328428288",
                    sender_id="2328428288",
                    message_id="m-1",
                )

                await adapter.send(make_stream_event(request, phase=STREAM_PHASE_START))
                typing_frame = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                assert typing_frame["action"] == "set_input_status"
                await websocket.send(
                    json.dumps({"status": "ok", "retcode": 0, "echo": typing_frame["echo"]})
                )

                await adapter.send(
                    make_stream_event(request, phase=STREAM_PHASE_DELTA, text="你", seq=1)
                )
                await adapter.send(
                    make_stream_event(request, phase=STREAM_PHASE_DELTA, text="好", seq=2)
                )
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(websocket.recv(), timeout=0.3)

                await adapter.send(make_stream_event(request, phase=STREAM_PHASE_END))
                assert adapter._typing == {}
        finally:
            await adapter.stop()
            await transport.stop()

    @pytest.mark.asyncio
    async def test_typing_disabled_sends_nothing(self):
        port = free_port()
        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
        adapter = OneBot11Adapter(
            transport, stream_mode="typing", typing_enabled=False, typing_refresh_seconds=30
        )
        await transport.start()
        try:
            async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token={TOKEN}") as websocket:
                assert await wait_until(lambda: transport.connected)
                request = Envelope(
                    kind="request",
                    channel="qq",
                    target_channel="qq",
                    conversation_id="2328428288",
                    message_id="m-1",
                )

                await adapter.send(make_stream_event(request, phase=STREAM_PHASE_START))
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(websocket.recv(), timeout=0.3)
                assert adapter._typing == {}
        finally:
            await adapter.stop()
            await transport.stop()

    @pytest.mark.asyncio
    async def test_off_mode_never_starts_typing(self):
        port = free_port()
        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
        adapter = OneBot11Adapter(transport, stream_mode="off")
        await transport.start()
        try:
            async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token={TOKEN}") as websocket:
                assert await wait_until(lambda: transport.connected)
                request = Envelope(
                    kind="request",
                    channel="qq",
                    target_channel="qq",
                    conversation_id="2328428288",
                    message_id="m-1",
                )

                await adapter.send(make_stream_event(request, phase=STREAM_PHASE_START))
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(websocket.recv(), timeout=0.3)
        finally:
            await adapter.stop()
            await transport.stop()

    def test_invalid_stream_mode_rejected(self):
        transport = OneBot11Transport(mode="reverse", port=free_port(), path=PATH, token=TOKEN)

        with pytest.raises(ValueError):
            OneBot11Adapter(transport, stream_mode="draft")


# ------------------------------------------------------- block 分段呈现(S5)


class FakeClock:
    """可注入的单调时钟(秒), 用于稳定验证节流."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ActionCollector:
    """后台读帧并 ack: 记录 send_private_msg 的文本, 同时应答 typing 等动作."""

    def __init__(self, websocket) -> None:
        self.websocket = websocket
        self.messages: list[str] = []
        self.actions: list[str] = []
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                raw = await self.websocket.recv()
                payload = json.loads(raw)
                action = payload.get("action", "")
                self.actions.append(action)
                if action == "send_private_msg":
                    self.messages.append(payload["params"]["message"][0]["data"]["text"])
                await self.websocket.send(
                    json.dumps({"status": "ok", "retcode": 0, "echo": payload.get("echo")})
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 收尾阶段连接关闭属正常
            return

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def block_adapter(transport, **kwargs):
    params = {
        "stream_mode": "block",
        "typing_enabled": False,
        "block_first_chars": 5,
        "block_min_chars": 5,
        "block_target_chars": 10,
        "block_max_chars": 20,
        "block_min_interval": 1.0,
        "block_max_messages": 3,
        "block_flush_interval": 0.0,
    }
    params.update(kwargs)
    return OneBot11Adapter(transport, **params)


def sentences_request():
    return make_inbound(
        channel="qq",
        text="问题",
        conversation_id="2328428288",
        sender_id="2328428288",
        message_id="m-1",
    )


class TestAdapterBlockMode:
    @pytest.mark.asyncio
    async def test_bounded_bubbles_and_final_suffix_join_to_full_answer(self):
        port = free_port()
        clock = FakeClock()
        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
        adapter = block_adapter(transport, clock=clock)
        await transport.start()
        try:
            async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token={TOKEN}") as websocket:
                assert await wait_until(lambda: transport.connected)
                collector = ActionCollector(websocket)
                collector.start()
                request = sentences_request()
                await adapter.send(make_stream_event(request, phase=STREAM_PHASE_START))

                parts = ["第一句。", "第二句。", "第三句。", "第四句。", "第五句。"]
                full = ""
                for index, part in enumerate(parts, start=1):
                    full += part
                    clock.advance(2.0)
                    await adapter.send(
                        make_stream_event(request, phase=STREAM_PHASE_DELTA, text=part, seq=index)
                    )
                await adapter.send(make_stream_event(request, phase=STREAM_PHASE_END))
                await adapter.send(make_reply(request, full))
                await collector.stop()

                # S5.1: 首块达到 max(first,min) 即切; 后续块等到 target 再切(块大小均匀)
                assert collector.messages == ["第一句。第二句。", "第三句。第四句。第五句。"]
                assert "".join(collector.messages) == full  # 不重复、不缺失
                assert len(collector.messages) <= adapter.block_max_messages
                assert "set_input_status" not in collector.actions  # typing 已关闭
        finally:
            await adapter.stop()
            await transport.stop()

    @pytest.mark.asyncio
    async def test_throttle_limits_bubbles_within_interval(self):
        port = free_port()
        clock = FakeClock()  # 不推进 → 只允许第一条
        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
        adapter = block_adapter(transport, clock=clock, block_min_interval=10.0)
        await transport.start()
        try:
            async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token={TOKEN}") as websocket:
                assert await wait_until(lambda: transport.connected)
                collector = ActionCollector(websocket)
                collector.start()
                request = sentences_request()
                for index, part in enumerate(["第一句。", "第二句。", "第三句。"], start=1):
                    await adapter.send(
                        make_stream_event(request, phase=STREAM_PHASE_DELTA, text=part, seq=index)
                    )
                await adapter.send(make_reply(request, "第一句。第二句。第三句。"))
                await collector.stop()

                assert collector.messages == ["第一句。第二句。", "第三句。"]
        finally:
            await adapter.stop()
            await transport.stop()

    @pytest.mark.asyncio
    async def test_max_messages_cap_moves_rest_to_final_reply(self):
        port = free_port()
        clock = FakeClock()
        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
        adapter = block_adapter(transport, clock=clock, block_max_messages=1)
        await transport.start()
        try:
            async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token={TOKEN}") as websocket:
                assert await wait_until(lambda: transport.connected)
                collector = ActionCollector(websocket)
                collector.start()
                request = sentences_request()
                full = ""
                for index, part in enumerate(["一。", "二。", "三。", "四。"], start=1):
                    full += part
                    clock.advance(2.0)
                    await adapter.send(
                        make_stream_event(request, phase=STREAM_PHASE_DELTA, text=part, seq=index)
                    )
                await adapter.send(make_reply(request, full))
                await collector.stop()

                # 总上限=1: 不产生中间气泡, 整段作为唯一一条发出
                assert collector.messages == [full]
                assert "".join(collector.messages) == full
        finally:
            await adapter.stop()
            await transport.stop()

    @pytest.mark.asyncio
    async def test_short_answer_produces_single_final_message(self):
        port = free_port()
        clock = FakeClock()
        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
        adapter = block_adapter(transport, clock=clock, block_first_chars=50)
        await transport.start()
        try:
            async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token={TOKEN}") as websocket:
                assert await wait_until(lambda: transport.connected)
                collector = ActionCollector(websocket)
                collector.start()
                request = sentences_request()
                await adapter.send(
                    make_stream_event(request, phase=STREAM_PHASE_DELTA, text="短回答。", seq=1)
                )
                await adapter.send(make_reply(request, "短回答。"))
                await collector.stop()

                assert collector.messages == ["短回答。"]
        finally:
            await adapter.stop()
            await transport.stop()

    @pytest.mark.asyncio
    async def test_no_duplicate_when_all_content_already_bubbled(self):
        port = free_port()
        clock = FakeClock()
        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
        adapter = block_adapter(transport, clock=clock)
        await transport.start()
        try:
            async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token={TOKEN}") as websocket:
                assert await wait_until(lambda: transport.connected)
                collector = ActionCollector(websocket)
                collector.start()
                request = sentences_request()
                await adapter.send(make_stream_event(request, phase=STREAM_PHASE_START))
                clock.advance(2.0)
                await adapter.send(
                    make_stream_event(request, phase=STREAM_PHASE_DELTA, text="完整一句。", seq=1)
                )
                await adapter.send(make_stream_event(request, phase=STREAM_PHASE_END))
                await adapter.send(make_reply(request, "完整一句。"))
                await collector.stop()

                assert collector.messages == ["完整一句。"]  # 不再补发重复内容
        finally:
            await adapter.stop()
            await transport.stop()

    @pytest.mark.asyncio
    async def test_typing_stops_after_first_bubble(self):
        port = free_port()
        clock = FakeClock()
        transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
        adapter = block_adapter(
            transport, clock=clock, typing_enabled=True, typing_refresh_seconds=30.0
        )
        await transport.start()
        try:
            async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token={TOKEN}") as websocket:
                assert await wait_until(lambda: transport.connected)
                collector = ActionCollector(websocket)
                collector.start()
                request = sentences_request()
                await adapter.send(make_stream_event(request, phase=STREAM_PHASE_START))
                clock.advance(2.0)
                await adapter.send(
                    make_stream_event(request, phase=STREAM_PHASE_DELTA, text="第一句说得长。", seq=1)
                )
                await asyncio.sleep(0.05)

                assert "set_input_status" in collector.actions  # 开始时有"正在输入"
                assert adapter._typing == {}  # 第一条气泡后已停止续期
                await adapter.send(make_reply(request, "第一句说得长。"))
                await collector.stop()
        finally:
            await adapter.stop()
            await transport.stop()
