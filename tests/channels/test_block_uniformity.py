"""S5.1 回归: 用用户实测原文验证 block 分块均匀性(不再"22 字小头 + 700 字巨块").

背景(docs/STREAMING_DESIGN.md §12.4):
- 旧实现按"优先级里最靠后的边界"切, 且没有任何单块上限 → 用户实测 722 字原文被切成 [22, 700];
- S5.1 改为"窗口内取最贴近 target 的边界" + "收尾按剩余预算均分" → 期望 [~145 × 5]。
"""

import asyncio
import json
import socket
from contextlib import suppress
from typing import Any, Optional

import pytest
from websockets.asyncio.client import connect

from meowmeowclaw.channels.onebot11 import OneBot11Adapter, OneBot11Transport
from meowmeowclaw.gateway import (
    STREAM_PHASE_DELTA,
    STREAM_PHASE_END,
    STREAM_PHASE_START,
    make_inbound,
    make_reply,
    make_stream_event,
)

TOKEN = "meowclaw-test"
PATH = "/onebot/v11/ws"
OWNER = "2328428288"

# 用户第一次实测的原文(模型输出 722 字, 含标题与空行)
TEXT_722 = """## 灯

海边有座灯塔，守塔的人姓林。

他每天傍晚做的事都一样：先上塔顶擦一遍玻璃，再检查灯芯，然后把灯点起来。灯转得很慢，一圈大约十二秒。十二秒里，光扫过海面、礁石，还有远处那条看不见的航线。他说，灯不是在照路，灯是在说——这里有人。

林师傅在这座塔上守了二十六年。

前十年他有个同伴，姓周。周师傅爱喝酒，喝多了就坐在塔下唱歌，唱得跑调，海浪都笑。后来有一年冬天，周师傅下塔去买酒，在结冰的台阶上滑了一跤，就没再上来。

那之后林师傅改了规矩。他把酒都倒了，把自己一天的时间切得很碎：早上扫院子，中午补网，下午念报纸，晚上点灯。他说，人要是把一天切成很多块，就没工夫去想一个整块的难过。

有一年，台风。风把海吹得翻过来，浪打到塔身，玻璃碎了两块。他一个人爬上塔顶，用身子挡住缺口，一手护着火，一手拉着栏杆，站了四个小时。灯没灭。

第二天早上有人来看，说林师傅，你这是何苦。

他说，昨晚有两条船在外面，其中一条，船上有个刚满月的孩子。他不认识那孩子，他只是想，灯在，就有人敢往前走。

再后来，有人给他写信来。

信里说，二十年前那个台风夜，他就在那条船上，怀里抱着刚满月的儿子，看见灯一直在。现在儿子长大了，考上了海洋学校，第一趟实习就跟着船路过这里。信的最后写着：谢谢那盏灯，它让我父亲没有放手。

林师傅把信折好，压在灯芯盒底下。

那天晚上他照旧去点灯。灯转起来，一圈十二秒。海面很平静，星星很低。他站在塔顶，忽然想起周师傅——如果周师傅在，大概会又唱那首跑调的歌。

他也忽然明白，二十六年，他守的从来不是灯。

他守的，是很多年前的一个晚上，有个孩子，正等着父亲回家。

灯又转了一圈。海面上没有船。

但灯还是亮着。"""

# 用户第二次实测的原文(288 字)
TEXT_288 = """## 石阶

山不高，台阶有三百级。老人每天清晨上来，坐到第一级，不看山顶，只看脚边。

有人问他看什么。

他说，看石头。这些台阶一百年了，被人踩过，被雨淋过，每一级的边角都不一样。有的圆了，有的裂了，有的中间陷下去一小块——那是很多人在这里停过脚。

他说，人多的地方，石头就软一点。

后来修路的工人想把台阶磨平，统一翻新。老人来劝，说别磨。工人问为什么。

他说，磨平了，就看不出来谁在这里歇过。

工人愣了一会儿，最后只补了裂缝，没动那些凹下去的地方。

如今老人不在了。每天清晨还是有人上来，也有人在第一级坐一会儿，看看脚边的石头。

石头还是老样子，软软地凹着。"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ActionCollector:
    """后台读帧并 ack; 记录 send_private_msg 的文本."""

    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket
        self.messages: list[str] = []
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                payload = json.loads(await self.websocket.recv())
                if payload.get("action") == "send_private_msg":
                    self.messages.append(payload["params"]["message"][0]["data"]["text"])
                await self.websocket.send(
                    json.dumps({"status": "ok", "retcode": 0, "echo": payload.get("echo")})
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 收尾关闭连接属正常
            return

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def drive_block(
    text: str,
    *,
    chunk: int,
    first_chars: int = 150,
    min_chars: int = 80,
    target_chars: int = 150,
    max_chars: int = 260,
    max_messages: int = 5,
) -> list[str]:
    """按 chunk 字/次的节奏喂 delta, 返回实际发出的气泡文本列表."""
    port = free_port()
    clock = FakeClock()
    transport = OneBot11Transport(mode="reverse", port=port, path=PATH, token=TOKEN)
    adapter = OneBot11Adapter(
        transport,
        stream_mode="block",
        typing_enabled=False,
        block_first_chars=first_chars,
        block_min_chars=min_chars,
        block_target_chars=target_chars,
        block_max_chars=max_chars,
        block_min_interval=0.001,
        block_max_messages=max_messages,
        block_flush_interval=0.0,
        clock=clock,
    )
    await transport.start()
    try:
        async with connect(f"ws://127.0.0.1:{port}{PATH}?access_token={TOKEN}") as websocket:
            for _ in range(100):
                if transport.connected:
                    break
                await asyncio.sleep(0.01)
            collector = ActionCollector(websocket)
            collector.start()
            request = make_inbound(
                channel="qq",
                text="写一份大概五百字左右的文本用于测试渐进式输出",
                conversation_id=OWNER,
                sender_id=OWNER,
                message_id="m-1",
            )
            await adapter.send(make_stream_event(request, phase=STREAM_PHASE_START))
            for index in range(0, len(text), chunk):
                clock.advance(1.0)  # 让节流不拦
                await adapter.send(
                    make_stream_event(
                        request,
                        phase=STREAM_PHASE_DELTA,
                        text=text[index : index + chunk],
                        seq=index,
                    )
                )
            await adapter.send(make_stream_event(request, phase=STREAM_PHASE_END))
            await adapter.send(make_reply(request, text))
            await collector.stop()
            return collector.messages
    finally:
        await adapter.stop()
        await transport.stop()


def normalize(text: str) -> str:
    """去掉空白后比较: QQ 发送时每条气泡的首尾空白会被 strip(纯排版, 不影响内容)."""
    return "".join(text.split())


class TestBlockUniformityRegression:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("chunk", [30, 60, 120])
    async def test_real_text_722_is_split_evenly(self, chunk):
        bubbles = await drive_block(TEXT_722, chunk=chunk)
        sizes = [len(bubble) for bubble in bubbles]

        assert normalize("".join(bubbles)) == normalize(TEXT_722)  # 不重复、不缺失
        assert len(bubbles) <= 5  # 单轮总气泡上限
        assert all(80 <= size <= 260 for size in sizes)  # 不再有 22 字小头/700 字巨块
        assert max(sizes) - min(sizes) <= 60  # 分布均匀

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chunk", [30, 60, 120])
    async def test_real_text_288_is_split_evenly(self, chunk):
        bubbles = await drive_block(TEXT_288, chunk=chunk)
        sizes = [len(bubble) for bubble in bubbles]

        assert normalize("".join(bubbles)) == normalize(TEXT_288)
        assert 2 <= len(bubbles) <= 3
        assert all(80 <= size <= 260 for size in sizes)
        assert max(sizes) - min(sizes) <= 60

    @pytest.mark.asyncio
    async def test_first_bubble_is_not_tiny_head(self):
        """旧缺陷: 首块被切在第一个段落边界(22 字)."""
        bubbles = await drive_block(TEXT_722, chunk=60)

        assert len(bubbles[0]) >= 80

    @pytest.mark.asyncio
    async def test_total_bubble_cap_is_respected(self):
        bubbles = await drive_block(TEXT_722, chunk=30, max_messages=3)
        sizes = [len(bubble) for bubble in bubbles]

        assert len(bubbles) <= 3  # 总上限(末块承载安全阀剩余)
        assert normalize("".join(bubbles)) == normalize(TEXT_722)
        assert all(size <= 260 for size in sizes[:-1])

    @pytest.mark.asyncio
    async def test_smaller_first_chars_trade_evenness_for_earlier_first_bubble(self):
        """把首块下限调小 → 首块更早更小(用户显式选择的权衡)."""
        early = await drive_block(TEXT_288, chunk=30, first_chars=80)
        even = await drive_block(TEXT_288, chunk=30, first_chars=150)

        assert len(early[0]) < len(even[0])  # 更早出现 → 更小
        assert max(len(b) for b in even) - min(len(b) for b in even) <= max(
            len(b) for b in early
        ) - min(len(b) for b in early)

    @pytest.mark.asyncio
    async def test_short_answer_stays_single_bubble(self):
        bubbles = await drive_block("这是一句很短的回答。", chunk=30)

        assert bubbles == ["这是一句很短的回答。"]
