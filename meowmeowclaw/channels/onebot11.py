"""OneBot 11(NapCat)传输层: 反向 WebSocket 服务端 + 正向 WebSocket 客户端.

职责边界(与 ``QqAdapter`` 组合使用)::

    NapCat ──WS 事件(JSON)──► OneBot11Transport ──IncomingMessage──► QqAdapter.feed
    NapCat ◄──WS 动作(JSON)── OneBot11Transport ◄──Envelope── QqAdapter.send

约定:

- 只有 ``post_type=message`` + ``message_type=private`` 的事件进入渠道层,
  心跳/生命周期/群聊事件静默忽略(debug 日志);
- 出站固定使用 ``send_private_msg``, 文本以 **消息段数组** 发送,
  避免模型输出被 NapCat 当作 CQ 码执行; 超长文本按 ``max_text_chars`` 分片;
- ``echo`` 与动作响应配对, 单次动作超时/失败只记 warning, 不杀出站循环;
- ``stop_inbound()`` 只拒绝新连接/新事件, **不关闭既有连接**, 保证优雅关闭时
  在途回复仍能送达; 真正的连接关闭在 ``stop()`` 里完成;
- ``mode=reverse``: 本进程监听 ``ws://host:port+path``, NapCat 作为客户端接入并自动重连;
- ``mode=forward``: 本进程作为客户端连接 NapCat 的 WS 服务端, 内置指数退避重连。
"""

import asyncio
import hmac
import json
import logging
import math
import re
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import parse_qs, quote, urlsplit

from websockets.asyncio.client import connect
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from meowmeowclaw.gateway import (
    KIND_REPLY,
    KIND_STREAM,
    STREAM_PHASE_DELTA,
    STREAM_PHASE_END,
    STREAM_PHASE_START,
    Envelope,
    MessageBus,
)

from .base import IncomingMessage
from .qq_adapter import QqAdapter
from .streaming import (
    DEFAULT_MAX_TEXT_CHARS,
    BlockSegmenter,
    StreamAssembler,
    TypingKeepAlive,
    chunk_text,
    split_evenly,
)

logger = logging.getLogger(__name__)

POST_TYPE_MESSAGE = "message"
MESSAGE_TYPE_PRIVATE = "private"
# DEFAULT_MAX_TEXT_CHARS 由 channels/streaming.py 唯一定义并在此再导出
DEFAULT_ACTION_TIMEOUT = 10.0
DEFAULT_RECONNECT_MIN = 1.0
DEFAULT_RECONNECT_MAX = 30.0

# block 分段呈现默认参数(见 docs/STREAMING_DESIGN.md §6, S5.1 修订)
DEFAULT_BLOCK_FIRST_CHARS = 150     # 首块触发下限(默认=target; 调小可更早看到首块, 但均匀性略降)
DEFAULT_BLOCK_MIN_CHARS = 80        # 单块下限
DEFAULT_BLOCK_TARGET_CHARS = 150    # 理想块大小(同级边界里取最贴近它的)
DEFAULT_BLOCK_MAX_CHARS = 260       # 单块硬上限(无边界时也在此处硬切)
DEFAULT_BLOCK_MIN_INTERVAL = 2.5    # 中间气泡最小间隔(秒)
DEFAULT_BLOCK_MAX_MESSAGES = 5      # **单轮总气泡上限**(含收尾补发)
DEFAULT_BLOCK_FLUSH_INTERVAL = 0.3  # 收尾补发块之间的间隔(秒, 防风控)
# 单个 correlation 的分段状态上限(防回复丢失时泄漏)
MAX_BLOCK_STATES = 64


@dataclass
class _BlockState:
    """单个 correlation 的分段呈现状态(仅 block 模式)."""

    segmenter: BlockSegmenter
    messages: int = 0
    last_sent_at: Optional[float] = None

# 入站消息处理器: 收到一条平台无关消息, 返回是否成功入队
Handler = Callable[[IncomingMessage], Awaitable[bool]]

# CQ 码与消息段的占位转换(仅文本进入 Agent, 非文本转可读占位符)
_CQ_PATTERN = re.compile(r"\[CQ:([A-Za-z0-9_]+)((?:,[^\[\]]*)?)\]")
_SEGMENT_PLACEHOLDER = {
    "image": "[图片]",
    "face": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "markdown": "[卡片]",
    "poke": "[戳一戳]",
    "forward": "[合并转发]",
    "reply": "",
}
_NON_TEXT_PLACEHOLDER = "[非文本消息]"


def _parse_cq_params(raw: str) -> dict[str, str]:
    """解析 CQ 码参数串 ``,k=v,k2=v2``(值里的逗号按原样保留)."""
    params: dict[str, str] = {}
    for item in raw.lstrip(",").split(","):
        key, _, value = item.partition("=")
        if key:
            params[key.strip()] = value
    return params


def _segment_placeholder(seg_type: str, data: dict[str, Any]) -> str:
    """非文本消息段 -> 可读占位符."""
    if seg_type == "at":
        target = str(data.get("qq", ""))
        return "@全体成员" if target == "all" else f"@{target}"
    return _SEGMENT_PLACEHOLDER.get(seg_type, _NON_TEXT_PLACEHOLDER)


def strip_cq_codes(text: str) -> str:
    """把消息字符串里的 CQ 码替换成可读占位符(纯文本部分原样保留)."""
    return _CQ_PATTERN.sub(
        lambda match: _segment_placeholder(match.group(1), _parse_cq_params(match.group(2))),
        text,
    )


def extract_text(message: Any) -> str:
    """OneBot ``message`` 字段 -> 纯文本.

    同时兼容数组消息段与 CQ 码字符串两种形态; 非文本段转占位符。
    例: ``[{"type":"text","data":{"text":"hi"}},{"type":"image","data":{}}]`` -> ``"hi[图片]"``。
    """
    if message is None:
        return ""
    if isinstance(message, str):
        return strip_cq_codes(message)
    if isinstance(message, (list, tuple)):
        parts: list[str] = []
        for segment in message:
            if not isinstance(segment, dict):
                parts.append(str(segment))
                continue
            seg_type = str(segment.get("type", ""))
            data = segment.get("data")
            if not isinstance(data, dict):
                data = {}
            if seg_type == "text":
                parts.append(str(data.get("text", "")))
            else:
                parts.append(_segment_placeholder(seg_type, data))
        return "".join(parts)
    # 非字符串/非消息段的取值一律忽略, 避免把任意对象拼进 Prompt
    return ""


def _to_ms(value: Any) -> Optional[int]:
    """OneBot ``time``(秒) -> 毫秒时间戳; 非法值返回 None(由渠道层取当前时间)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(float(value) * 1000)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip()) * 1000
    return None


def event_to_incoming(
    payload: Any,
    *,
    channel: str = "qq",
    scope: str = "private",
) -> Optional[IncomingMessage]:
    """OneBot 事件 -> ``IncomingMessage``; 非私聊消息 / 空文本返回 None."""
    if not isinstance(payload, dict):
        return None
    if payload.get("post_type") != POST_TYPE_MESSAGE:
        return None
    if payload.get("message_type") != MESSAGE_TYPE_PRIVATE:
        return None
    user_id = payload.get("user_id")
    if user_id is None or not str(user_id).strip():
        return None
    raw_message = payload.get("message")
    if raw_message is None:
        raw_message = payload.get("raw_message")
    text = extract_text(raw_message)
    if not text.strip():
        return None
    message_id = payload.get("message_id")
    return IncomingMessage(
        channel=channel,
        scope=scope,
        conversation_id=str(user_id).strip(),
        sender_id=str(user_id).strip(),
        text=text,
        message_id=None if message_id is None else str(message_id),
        received_at_ms=_to_ms(payload.get("time")),
    )


class OneBot11Transport:
    """OneBot 11 传输(反向服务端 / 正向客户端二选一).

    ``send(envelope)`` 满足 ``QqAdapter`` 的 transport 契约,
    入站事件通过 ``handler``(通常是 ``QqAdapter.feed``)注入网关。
    """

    def __init__(
        self,
        *,
        mode: str = "reverse",
        host: str = "127.0.0.1",
        port: int = 3100,
        path: str = "/onebot/v11/ws",
        token: str = "",
        action_timeout: float = DEFAULT_ACTION_TIMEOUT,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        reconnect_min: float = DEFAULT_RECONNECT_MIN,
        reconnect_max: float = DEFAULT_RECONNECT_MAX,
        handler: Optional[Handler] = None,
        channel: str = "qq",
    ) -> None:
        if mode not in ("reverse", "forward"):
            raise ValueError(f"mode 必须是 reverse/forward: {mode!r}")
        if not str(host).strip():
            raise ValueError("host 不能为空")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError(f"port 不合法: {port!r}")

        self.mode = mode
        self.host = str(host).strip()
        self.port = int(port)
        self.path = path if str(path).startswith("/") else f"/{path}"
        self.token = str(token or "")
        self.action_timeout = float(action_timeout)
        self.max_text_chars = int(max_text_chars)
        self.reconnect_min = max(0.1, float(reconnect_min))
        self.reconnect_max = max(self.reconnect_min, float(reconnect_max))
        self.channel = str(channel)
        self._handler = handler

        self._started = False
        self._stopped = True
        self._accepting = False
        self._server: Optional[Server] = None
        self._client_task: Optional[asyncio.Task] = None
        self._active: Optional[ServerConnection] = None
        self._pending: dict[str, asyncio.Future] = {}

    def __repr__(self) -> str:
        return (
            f"<OneBot11Transport mode={self.mode!r} endpoint={self.endpoint!r} "
            f"connected={self.connected}>"
        )

    # ------------------------------------------------------------------ 属性

    @property
    def endpoint(self) -> str:
        """可读的监听/连接地址(token 不展示)."""
        return f"ws://{self.host}:{self.port}{self.path}"

    @property
    def connected(self) -> bool:
        websocket = self._active
        return websocket is not None and websocket.state is State.OPEN

    @property
    def accepting(self) -> bool:
        return self._accepting

    def set_handler(self, handler: Optional[Handler]) -> None:
        self._handler = handler

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """启动传输; reverse 模式绑定端口失败会直接抛异常(由调用方报错退出)."""
        if self._started:
            return
        self._started = True
        self._stopped = False
        self._accepting = True
        if self.mode == "reverse":
            self._server = await serve(
                self._handle_reverse,
                self.host,
                self.port,
                ping_interval=20,
                ping_timeout=20,
                max_size=4 * 1024 * 1024,
            )
            logger.info("OneBot11 反向 WS 已监听 %s (等待 NapCat 连接)", self.endpoint)
        else:
            self._client_task = asyncio.create_task(self._run_forward(), name="onebot11-client")
            logger.info("OneBot11 正向 WS 已启动, 目标 %s", self.endpoint)

    def stop_inbound(self) -> None:
        """停止接收新入站(幂等): 拒绝新连接/忽略新事件, 但不关闭既有连接."""
        self._accepting = False

    async def stop(self, timeout: float = 5.0) -> None:
        """关闭连接与监听(幂等); 先关连接再关服务端, 避免关闭握手卡住监听."""
        if self._stopped:
            return
        self._stopped = True
        self._started = False
        self._accepting = False

        websocket, self._active = self._active, None
        if websocket is not None:
            with suppress(Exception):
                await asyncio.wait_for(websocket.close(), timeout=max(0.1, timeout / 2))

        for future in list(self._pending.values()):
            if not future.done():
                future.cancel()
        self._pending.clear()

        server, self._server = self._server, None
        if server is not None:
            server.close()
            with suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), timeout=max(0.1, timeout))

        task, self._client_task = self._client_task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        logger.info("OneBot11 传输已停止 (mode=%s)", self.mode)

    # ------------------------------------------------------------------ 入站

    async def _handle_reverse(self, websocket: ServerConnection) -> None:
        """反向模式: NapCat 每条连接一个 handler 协程."""
        if not self._authorized(websocket):
            logger.warning("拒绝未通过 token 校验的 NapCat 连接: %s", self._peer(websocket))
            with suppress(Exception):
                await websocket.close(code=1008, reason="unauthorized")
            return
        if not self._accepting:
            logger.info("已停止入站, 拒绝新连接: %s", self._peer(websocket))
            with suppress(Exception):
                await websocket.close(code=1001, reason="stopping")
            return

        previous, self._active = self._active, websocket
        if previous is not None and previous is not websocket:
            logger.info("NapCat 建立新连接, 关闭旧连接: %s", self._peer(previous))
            with suppress(Exception):
                await previous.close(code=1000, reason="replaced")

        logger.info("NapCat 已连接: %s", self._peer(websocket))
        try:
            await self._read_loop(websocket)
        except ConnectionClosed as exc:
            logger.info("NapCat 连接已关闭: %s", exc)
        except Exception:  # noqa: BLE001 单条连接异常不影响监听
            logger.exception("NapCat 连接处理异常")
        finally:
            if self._active is websocket:
                self._active = None
            logger.info("NapCat 已断开, 等待重连")

    async def _run_forward(self) -> None:
        """正向模式: 作为客户端连接 NapCat 的 WS 服务端, 指数退避重连."""
        delay = self.reconnect_min
        while not self._stopped:
            try:
                async with connect(
                    self._build_uri(),
                    additional_headers=self._auth_headers(),
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=4 * 1024 * 1024,
                ) as websocket:
                    self._active = websocket
                    logger.info("已连接 NapCat: %s", self.endpoint)
                    delay = self.reconnect_min
                    await self._read_loop(websocket)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 连接失败/断开都走重连
                if not self._stopped:
                    logger.warning("NapCat 连接失败或断开(%r), %.1fs 后重连", exc, delay)
            finally:
                self._active = None
            if self._stopped:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.reconnect_max)

    async def _read_loop(self, websocket: Any) -> None:
        async for raw in websocket:
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError:
                    logger.warning("收到非 UTF-8 字节帧, 已忽略")
                    continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                logger.warning("收到非法 JSON 帧, 已忽略: %.200s", raw)
                continue
            if not isinstance(payload, dict):
                logger.warning("收到非对象 JSON 帧, 已忽略: %.200s", raw)
                continue
            await self._dispatch(payload)

    async def _dispatch(self, payload: dict[str, Any]) -> None:
        echo = payload.get("echo")
        if echo is not None and ("retcode" in payload or "status" in payload):
            future = self._pending.pop(str(echo), None)
            if future is not None and not future.done():
                future.set_result(payload)
            return

        incoming = event_to_incoming(payload, channel=self.channel)
        if incoming is None:
            logger.debug(
                "忽略 OneBot 事件: post_type=%s message_type=%s",
                payload.get("post_type"),
                payload.get("message_type"),
            )
            return
        if not self._accepting:
            logger.debug("已停止入站, 丢弃消息: id=%s", incoming.message_id)
            return
        if self._handler is None:
            logger.warning("未注入入站处理器, 丢弃消息: id=%s", incoming.message_id)
            return
        try:
            await self._handler(incoming)
        except Exception:  # noqa: BLE001 处理器异常不杀读循环
            logger.exception("OneBot 入站分发失败: id=%s", incoming.message_id)

    # ------------------------------------------------------------------ 出站

    async def send(self, envelope: Envelope) -> None:
        """把出站信封发给主人私聊; 未连接时丢弃并记 warning(fail-soft)."""
        text = (envelope.text or "").strip()
        if not text:
            return

        websocket = self._active
        if websocket is None or websocket.state is not State.OPEN:
            logger.warning("NapCat 未连接, 丢弃出站消息: id=%s", envelope.message_id)
            return

        target = (envelope.conversation_id or envelope.sender_id or "").strip()
        if not target:
            logger.warning("出站消息缺少 conversation_id/sender_id, 丢弃: id=%s", envelope.message_id)
            return
        user_id: Any = int(target) if target.isdigit() else target

        chunks = chunk_text(text, self.max_text_chars)
        for index, chunk in enumerate(chunks, start=1):
            params = {"user_id": user_id, "message": [{"type": "text", "data": {"text": chunk}}]}
            result = await self._call_api(websocket, "send_private_msg", params)
            if result is None:
                # 连接已断或超时: 停止后续分片, 避免无谓等待
                return
            logger.debug(
                "已发送 QQ 私聊消息: id=%s chunk=%d/%d chars=%d",
                envelope.message_id,
                index,
                len(chunks),
                len(chunk),
            )

    async def _call_api(
        self,
        websocket: Any,
        action: str,
        params: dict[str, Any],
        *,
        timeout: Optional[float] = None,
        quiet: bool = False,
    ) -> Optional[dict[str, Any]]:
        """发送动作并等待 ``echo`` 响应; 超时/失败返回 None(已记日志).

        ``quiet=True`` 时失败降到 debug 级(输入状态等 best-effort 动作专用)。
        """
        budget = self.action_timeout if timeout is None else float(timeout)
        log = logger.debug if quiet else logger.warning
        echo = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[echo] = future
        try:
            await websocket.send(
                json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False)
            )
            response = await asyncio.wait_for(future, timeout=budget)
        except asyncio.TimeoutError:
            log("OneBot 动作超时(%.1fs): %s", budget, action)
            return None
        except ConnectionClosed as exc:
            log("OneBot 动作发送失败(连接已关闭): %s (%s)", action, exc)
            return None
        except Exception as exc:  # noqa: BLE001 发送失败只影响本条消息
            log("OneBot 动作发送失败: %s (%r)", action, exc)
            return None
        finally:
            self._pending.pop(echo, None)

        retcode = response.get("retcode")
        if response.get("status") != "ok" and retcode != 0:
            log(
                "OneBot 动作返回失败: %s retcode=%s message=%s",
                action,
                retcode,
                response.get("message") or response.get("wording") or "",
            )
            return None
        return response

    async def set_input_status(
        self, user_id: Any, event_type: int = 1, *, timeout: float = 3.0
    ) -> bool:
        """NapCat 扩展接口: 设置对某好友的"正在输入"状态(见 docs/STREAMING_DESIGN.md §4.7).

        - best-effort: 未连接/超时/不支持都只是返回 False 并记 debug, 不影响回复;
        - ``event_type`` 默认 1(=正在输入), 语义已由真机实测确认(见设计文档 §12)。
        """
        websocket = self._active
        if websocket is None or websocket.state is not State.OPEN:
            return False
        target: Any = int(user_id) if str(user_id).isdigit() else user_id
        result = await self._call_api(
            websocket,
            "set_input_status",
            {"user_id": target, "event_type": int(event_type)},
            timeout=timeout,
            quiet=True,
        )
        return result is not None

    # ------------------------------------------------------------------ 鉴权/工具

    def _authorized(self, websocket: ServerConnection) -> bool:
        """反向模式 token 校验: 支持 ``?access_token=`` 与 ``Authorization: Bearer``."""
        if not self.token:
            return True
        request = getattr(websocket, "request", None)
        if request is None:  # pragma: no cover - 新版本 websockets 一定带 request
            return False
        query = parse_qs(urlsplit(getattr(request, "path", "") or "").query)
        provided = (query.get("access_token") or [None])[0]
        if provided is None:
            headers = getattr(request, "headers", None) or {}
            authorization = headers.get("Authorization") or headers.get("authorization") or ""
            if isinstance(authorization, str) and authorization.lower().startswith("bearer "):
                provided = authorization[7:].strip()
        return hmac.compare_digest(str(provided or ""), self.token)

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _build_uri(self) -> str:
        query = f"?access_token={quote(self.token)}" if self.token else ""
        return f"ws://{self.host}:{self.port}{self.path}{query}"

    @staticmethod
    def _peer(websocket: ServerConnection) -> str:
        address = getattr(websocket, "remote_address", None)
        return f"{address[0]}:{address[1]}" if isinstance(address, tuple) and len(address) >= 2 else "unknown"


class OneBot11Adapter(QqAdapter):
    """``QqAdapter`` + ``OneBot11Transport`` 生命周期与流式呈现(实现网关适配器契约).

    ``BaseChannelAdapter`` 只管理出站消费循环, 平台连接由本类接管:
    先起出站消费再接收入站(契约要求), 关闭时先停入站(保留连接以送达在途回复),
    最后在 ``stop()`` 里断开连接。

    流式呈现(见 docs/STREAMING_DESIGN.md §4.7):

    - ``KIND_STREAM`` 的 ``start``/``end`` 相位驱动"对方正在输入"续期任务;
    - ``delta`` 在 S4 只用于维持 typing, **绝不逐条发消息**(否则就是"一字一气泡");
      ``block`` 分段呈现由 S5 实现。
    """

    def __init__(
        self,
        transport: OneBot11Transport,
        *,
        channel: str = "qq",
        stream_mode: str = "typing",
        typing_enabled: bool = True,
        typing_refresh_seconds: float = 5.0,
        typing_event_type: int = 1,
        block_first_chars: int = DEFAULT_BLOCK_FIRST_CHARS,
        block_min_chars: int = DEFAULT_BLOCK_MIN_CHARS,
        block_target_chars: int = DEFAULT_BLOCK_TARGET_CHARS,
        block_max_chars: int = DEFAULT_BLOCK_MAX_CHARS,
        block_min_interval: float = DEFAULT_BLOCK_MIN_INTERVAL,
        block_max_messages: int = DEFAULT_BLOCK_MAX_MESSAGES,
        block_flush_interval: float = DEFAULT_BLOCK_FLUSH_INTERVAL,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        super().__init__(channel=channel, transport=transport.send)
        if stream_mode not in ("off", "typing", "block"):
            raise ValueError(f"stream_mode 取值不支持: {stream_mode!r}")
        if block_max_messages <= 0:
            raise ValueError(f"block_max_messages 必须为正整数: {block_max_messages!r}")
        if block_min_interval <= 0:
            raise ValueError(f"block_min_interval 必须为正数: {block_min_interval!r}")
        if block_flush_interval < 0:
            raise ValueError(f"block_flush_interval 不能为负数: {block_flush_interval!r}")
        self.onebot = transport
        self.onebot.set_handler(self.feed)
        self.stream_mode = stream_mode
        self.typing_enabled = bool(typing_enabled)
        self.typing_refresh_seconds = float(typing_refresh_seconds)
        self.typing_event_type = int(typing_event_type)
        # block 模式参数(见 docs/STREAMING_DESIGN.md §6)
        self.block_first_chars = max(1, int(block_first_chars))
        self.block_min_chars = max(1, int(block_min_chars))
        self.block_target_chars = max(1, int(block_target_chars))
        self.block_max_chars = max(1, int(block_max_chars))
        self.block_min_interval = float(block_min_interval)
        self.block_max_messages = int(block_max_messages)
        self.block_flush_interval = float(block_flush_interval)
        # 水位线: 记录"已作为气泡送达"的内容, 最终 reply 只补发剩余后缀
        self.assembler = StreamAssembler(max_chars=self.onebot.max_text_chars)
        self._clock = clock or time.monotonic
        # correlation_id -> 续期任务(仅在 agent 生成期间存在)
        self._typing: dict[str, TypingKeepAlive] = {}
        # correlation_id -> 分段状态(仅 block 模式)
        self._blocks: dict[str, _BlockState] = {}

    async def start(self, bus: MessageBus) -> None:
        await super().start(bus)
        try:
            await self.onebot.start()
        except BaseException:
            await super().stop()
            raise

    async def stop_inbound(self) -> None:
        await super().stop_inbound()
        self.onebot.stop_inbound()

    async def stop(self, timeout: float = 5.0) -> None:
        await self._stop_all_typing()
        self._blocks.clear()
        self.assembler.clear()
        await self.onebot.stop(timeout)
        await super().stop(timeout)

    # ------------------------------------------------------------------ 流式呈现

    async def send(self, envelope: Envelope) -> None:
        """流式信封只在渠道内消化; 真正的回复仍走 ``QqAdapter.send`` → ``send_private_msg``."""
        if envelope.kind == KIND_STREAM:
            await self._handle_stream_envelope(envelope)
            return
        if self.stream_mode == "block":
            await self._finish_block_reply(envelope)
            return
        self.assembler.forget(envelope.correlation_id)
        await super().send(envelope)

    async def _finish_block_reply(self, envelope: Envelope) -> None:
        """block 模式的最终回答: 把**尚未送达**的剩余文本均分成若干块再发出.

        - 即使模型一口气吐完(一个中间气泡都没有), 也不会出现"最后一条塞进一大段";
        - 已送达部分由水位线扣除, 绝不重复;
        - 总气泡数受 ``block_max_messages`` 约束(含已发出的中间气泡)。
        """
        correlation = envelope.correlation_id
        state = self._blocks.pop(correlation, None)
        sent_count = state.messages if state is not None else 0
        remaining = self.assembler.remaining_text(correlation, envelope.text)
        self.assembler.forget(correlation)
        if not remaining:
            self.sent.append(envelope)  # 内容已全部作为气泡送达, 不再重发
            return

        budget = max(1, self.block_max_messages - sent_count)
        pieces = min(budget, max(1, math.ceil(len(remaining) / self.block_target_chars)))
        blocks = split_evenly(
            remaining,
            pieces=pieces,
            target_chars=self.block_target_chars,
            max_chars=self.block_max_chars,
            min_chars=min(self.block_min_chars, len(remaining)),
        )
        for index, block in enumerate(blocks):
            if index and self.block_flush_interval:
                await asyncio.sleep(self.block_flush_interval)
            await self._emit_bubble(envelope, block)

    async def _handle_stream_envelope(self, envelope: Envelope) -> None:
        phase = envelope.metadata.get("phase")
        if phase == STREAM_PHASE_START:
            self.assembler.register(envelope.correlation_id)
            self._start_typing(envelope.correlation_id, envelope)
        elif phase == STREAM_PHASE_DELTA:
            if self.stream_mode == "block":
                await self._handle_block_delta(envelope)
        elif phase == STREAM_PHASE_END:
            # 只停 typing; 分段状态保留到最终 reply 再清理(收尾需要已发块数)
            await self._stop_typing(envelope.correlation_id)

    # ------------------------------------------------------------------ block 分段呈现

    async def _handle_block_delta(self, envelope: Envelope) -> None:
        """按"语义边界 + 节流 + 条数封顶"发中间气泡(见 docs/STREAMING_DESIGN.md §4.6)."""
        text = envelope.text
        if not text:
            return
        correlation = envelope.correlation_id
        state = self._blocks.get(correlation)
        if state is None:
            state = _BlockState(
                segmenter=BlockSegmenter(
                    min_chars=self.block_min_chars,
                    target_chars=self.block_target_chars,
                    max_chars=self.block_max_chars,
                )
            )
            self._blocks[correlation] = state
            # 防泄漏: 极端情况下 reply 迟迟不来时按插入序淘汰旧状态
            while len(self._blocks) > MAX_BLOCK_STATES:
                self._blocks.pop(next(iter(self._blocks)), None)
        state.segmenter.push(text)

        # 中间气泡最多用掉"总上限 - 1"个槽位, 给收尾补发留一条(否则总条数会超上限)
        intermediate_cap = max(0, self.block_max_messages - 1)
        while True:
            if state.messages >= intermediate_cap:
                return
            # 首块允许早一点(>= max(first_chars, min_chars)); 后续块等到 target 再切,
            # 这样每块都贴近理想大小, 不会出现"一堆小块把预算吃光、尾巴一大坨"
            floor = (
                max(self.block_first_chars, self.block_min_chars)
                if state.messages == 0
                else self.block_target_chars
            )
            if state.segmenter.buffered_length < floor:
                return
            now = self._clock()
            if state.last_sent_at is not None and now - state.last_sent_at < self.block_min_interval:
                return  # 时间节流
            block = state.segmenter.take()
            if block is None:
                return  # 尚未形成自然边界

            await self._emit_bubble(envelope, block)
            self.assembler.mark_sent(correlation, block)
            state.messages += 1
            state.last_sent_at = self._clock()
            if state.messages == 1:
                # 第一条气泡已经把"有内容了"传递出去, 输入状态可以停掉
                await self._stop_typing(correlation)

    async def _emit_bubble(self, envelope: Envelope, text: str) -> None:
        """把一段中间内容作为独立气泡发出(复用最终回复通道)."""
        bubble = replace(envelope, kind=KIND_REPLY, text=text)
        self.sent.append(bubble)
        await self.onebot.send(bubble)

    def _typing_active(self) -> bool:
        return self.typing_enabled and self.stream_mode in ("typing", "block")

    def _start_typing(self, correlation_id: str, envelope: Envelope) -> None:
        if not self._typing_active() or correlation_id in self._typing:
            return
        user_id = (envelope.conversation_id or envelope.sender_id or "").strip()
        if not user_id:
            return
        keepalive = TypingKeepAlive(
            lambda: self.onebot.set_input_status(user_id, self.typing_event_type),
            label=f"qq:{user_id}",
            refresh_seconds=self.typing_refresh_seconds,
        )
        self._typing[correlation_id] = keepalive
        keepalive.start()

    async def _stop_typing(self, correlation_id: str) -> None:
        keepalive = self._typing.pop(correlation_id, None)
        if keepalive is not None:
            await keepalive.stop()

    async def _stop_all_typing(self) -> None:
        keepalives = list(self._typing.values())
        self._typing.clear()
        for keepalive in keepalives:
            await keepalive.stop()


__all__ = [
    "DEFAULT_ACTION_TIMEOUT",
    "DEFAULT_MAX_TEXT_CHARS",
    "MESSAGE_TYPE_PRIVATE",
    "POST_TYPE_MESSAGE",
    "OneBot11Adapter",
    "OneBot11Transport",
    "DEFAULT_BLOCK_FIRST_CHARS",
    "DEFAULT_BLOCK_FLUSH_INTERVAL",
    "DEFAULT_BLOCK_MAX_CHARS",
    "DEFAULT_BLOCK_MAX_MESSAGES",
    "DEFAULT_BLOCK_MIN_CHARS",
    "DEFAULT_BLOCK_MIN_INTERVAL",
    "DEFAULT_BLOCK_TARGET_CHARS",
    "TypingKeepAlive",
    "chunk_text",
    "event_to_incoming",
    "extract_text",
    "strip_cq_codes",
]
