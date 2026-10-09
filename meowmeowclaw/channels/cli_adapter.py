"""CLI 渠道适配器(见 docs/GATEWAY_DESIGN.md §5.1).

CLI 与 QQ/飞书一样经总线, 但保留 REPL 语义:

- 出站: ``BaseChannelAdapter`` 消费 ``outbound:cli``, ``send()`` 打印并唤醒等待中的 ``ask()``;
- 入站: ``run_repl()`` 用同步 ``input()`` 读取(单用户单在途, 阻塞事件循环是安全的),
  ``ask()`` 发布信封并 await 回复 Future 后再显示下一个提示符;
- ``/exit``、``/quit``、``/q`` 是 REPL 本地命令, 不发布到总线;
- Ctrl+C 不在适配器内吞掉, 由 ``cli.main`` 统一触发 ``gateway.stop()`` 优雅关闭。
"""

import asyncio
import builtins
from typing import Callable, Optional, Sequence

from meowmeowclaw.gateway import BaseChannelAdapter, Envelope, make_inbound

from .cli_policy import CLI_APP_NAME, CLI_EXIT_COMMANDS, CLI_PROMPT


class CliAdapter(BaseChannelAdapter):
    """进程内 CLI 适配器(同步 REPL + 异步总线)."""

    def __init__(
        self,
        *,
        channel: str = "cli",
        prompt: str = CLI_PROMPT,
        input_func: Optional[Callable[[str], str]] = None,
        print_func: Optional[Callable[[str], None]] = None,
        reply_timeout: Optional[float] = None,
    ) -> None:
        super().__init__(channel=channel)
        self.prompt = prompt
        self.reply_timeout = reply_timeout
        self._input = input_func
        self._print = print_func
        self.sent: list[Envelope] = []
        self._pending: dict[str, asyncio.Future] = {}

    # ------------------------------------------------------------------ 内部工具

    def _read_input(self, prompt: str) -> str:
        reader = self._input if self._input is not None else builtins.input
        return reader(prompt)

    def _output(self, text: str) -> None:
        (self._print if self._print is not None else builtins.print)(text)

    # ------------------------------------------------------------------ 出站

    async def send(self, envelope: Envelope) -> None:
        self.sent.append(envelope)
        self._output(f"\n{CLI_APP_NAME} > {envelope.text}")
        future = self._pending.get(envelope.correlation_id)
        if future is not None and not future.done():
            future.set_result(envelope)

    # ------------------------------------------------------------------ 入站

    async def ask(self, text: str) -> Optional[Envelope]:
        """发布一条用户输入并等待对应回复; 未启动/超时/关闭返回 None."""
        envelope = make_inbound(
            channel=self.channel,
            text=text,
            scope="session",
            conversation_id="cli",
            sender_id="cli",
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[envelope.message_id] = future
        try:
            if not await self.publish_inbound(envelope):
                return None
            if self.reply_timeout is not None:
                return await asyncio.wait_for(future, timeout=self.reply_timeout)
            return await future
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending.pop(envelope.message_id, None)

    async def run_repl(self) -> str:
        """同步 REPL 循环; 返回 ``"exit"`` 或 ``"eof"``, Ctrl+C 由调用方处理."""
        while True:
            try:
                raw = self._read_input(self.prompt)
            except EOFError:
                return "eof"
            text = (raw or "").strip()
            if not text:
                continue
            if text.lower() in CLI_EXIT_COMMANDS:
                return "exit"
            await self.ask(text)

    async def stop(self, timeout: float = 5.0) -> None:
        """先取消未完成等待, 再排空出站队列并停出站消费."""
        for future in list(self._pending.values()):
            if not future.done():
                future.cancel()
        self._pending.clear()
        await super().stop(timeout)

    async def wait_sent(self, count: int = 1, timeout: float = 5.0) -> Sequence[Envelope]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self.sent) < count and loop.time() < deadline:
            await asyncio.sleep(0.001)
        return list(self.sent)
