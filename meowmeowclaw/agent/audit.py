"""HISTORY.md 审计日志: 记录每次上下文压缩/降级的原文与摘要, 供人工校验.

设计见 ``docs/CONTEXT_COMPRESSION_DESIGN.md`` §7:

- 追加式 Markdown, 一次压缩事件一个 section, 文件位于 ``<memory_dir>/HISTORY.md``;
- 时间戳用 ``ms_to_iso`` 产生 UTC ISO8601(毫秒精度), 与 JSONL 会话记录同一口径;
- 写前按大小轮转: ``>= max_bytes`` 时 ``HISTORY.md -> HISTORY.md.1``(单代覆盖);
- 跨进程文件锁(``async_file_lock``)覆盖"读大小 -> 轮转 -> 追加"全过程;
- 任何 I/O/权限/格式化错误只 warning, **绝不阻塞压缩与回复**(fail-soft);
- 只写不读: 不注入 Prompt、不被压缩逻辑读回, 也不是摘要缓存.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from meowmeowclaw.memory.filelock import async_file_lock
from meowmeowclaw.memory.models import ms_to_iso

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 2_097_152  # 2 MiB
DEFAULT_ORIGINAL_CHARS = 32_000
DEFAULT_LOCK_TIMEOUT = 5.0

_TRUNCATE_MARKER = "\n...(审计原文截断, 超长部分已省略)..."

# 不允许模型通过文件工具访问的运行时文件(与 tools/filesystem.py 保持一致)
RUNTIME_MEMORY_FILES = ("HISTORY.md", "HISTORY.md.1", "HISTORY.md.lock")


class HistoryAuditLog:
    """
    HISTORY.md 追加写入器(实现 ``ContextCompressor`` 的 ``AuditSink`` 协议).

    Args:
        path: ``HISTORY.md`` 路径; 父目录不存在时自动创建
        max_bytes: 轮转阈值(字节), 超过则当前文件滚动为 ``HISTORY.md.1``
        original_chars: 单条事件中原文 JSON 的字符上限, 超出按 head/tail 截断
        lock_timeout: 获取跨进程文件锁的超时(秒)
    """

    def __init__(
        self,
        path: Union[str, Path],
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        original_chars: int = DEFAULT_ORIGINAL_CHARS,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError(f"max_bytes 必须是正整数: {max_bytes!r}")
        if (
            not isinstance(original_chars, int)
            or isinstance(original_chars, bool)
            or original_chars <= 0
        ):
            raise ValueError(f"original_chars 必须是正整数: {original_chars!r}")
        if not isinstance(lock_timeout, (int, float)) or isinstance(lock_timeout, bool):
            raise ValueError(f"lock_timeout 必须是正数: {lock_timeout!r}")
        if lock_timeout <= 0:
            raise ValueError(f"lock_timeout 必须是正数: {lock_timeout!r}")

        self.path = Path(path).expanduser()
        self.rotated_path = self.path.with_name(self.path.name + ".1")
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.max_bytes = max_bytes
        self.original_chars = original_chars
        self.lock_timeout = float(lock_timeout)

    def __repr__(self) -> str:
        return (
            f"<HistoryAuditLog path={str(self.path)!r} max_bytes={self.max_bytes} "
            f"original_chars={self.original_chars}>"
        )

    # ------------------------------------------------------------------ 对外

    async def record(self, event: dict[str, Any]) -> None:
        """格式化并追加一条事件; 任何异常都只告警, 不向上抛."""
        try:
            text = self.format_event(event)
            async with async_file_lock(self.lock_path, timeout=self.lock_timeout):
                await asyncio.to_thread(self._append_locked, text)
        except Exception as exc:  # noqa: BLE001 审计失败绝不能影响主流程
            logger.warning("HISTORY.md 写入失败(fail-soft): %s (%r)", self.path, exc)

    def format_event(self, event: dict[str, Any]) -> str:
        """把审计事件格式化为一个 Markdown section(纯函数, 便于单测)."""
        event = event or {}
        header = (
            f"## {self._format_timestamp(event.get('timestamp_ms'))} | "
            f"session={event.get('session') or '-'} | "
            f"storage={event.get('storage_id') or '-'} | "
            f"event={event.get('event') or '-'} | "
            f"result={event.get('result') or '-'}"
        )

        original = list(event.get("original_messages") or [])
        bullets = [
            f"- 触发: estimated_input_tokens={event.get('estimated_before', 0)} "
            f"> budget={event.get('budget', 0)} (counter={event.get('counter') or '-'})",
            f"- 替换: 最早 {event.get('dropped_turns', 0)} 个 turn / {len(original)} 条消息",
            "- 摘要: model={model}, summary_tokens≈{tokens}, elapsed={elapsed}, cached={cached}".format(
                model=event.get("summary_model") or "-",
                tokens=event.get("summary_tokens")
                if event.get("summary_tokens") is not None
                else "-",
                elapsed=self._format_elapsed(event.get("elapsed_ms")),
                cached=bool(event.get("cached")),
            ),
            f"- 压缩后: estimated_input_tokens={event.get('estimated_after', 0)}",
        ]
        reason = event.get("reason")
        if reason:
            bullets.append(f"- 原因: {reason}")

        summary = event.get("summary")
        summary_text = summary if isinstance(summary, str) and summary.strip() else "（无：本次为硬裁降级）"
        storage = event.get("storage_id") or "<storage_id>"

        parts = [
            header,
            "",
            *bullets,
            "",
            "### 摘要",
            "",
            summary_text.strip(),
            "",
            f"### 原文（单条记录上限 {self.original_chars} 字符；完整原文见 sessions/{storage}.jsonl）",
            "",
            "```json",
            self._format_original(original),
            "```",
            "",
        ]
        return "\n".join(parts) + "\n"

    # ------------------------------------------------------------------ 内部

    def _append_locked(self, text: str) -> None:
        """在持有文件锁的前提下轮转并追加(同步 I/O, 调用方放线程池)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size >= self.max_bytes:
            os.replace(self.path, self.rotated_path)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(text)

    def _format_original(self, messages: Sequence[dict[str, Any]]) -> str:
        if not messages:
            return "[]"
        raw = json.dumps(list(messages), ensure_ascii=False, indent=2)
        if len(raw) <= self.original_chars:
            return raw
        half = max(1, self.original_chars // 2)
        return raw[:half] + _TRUNCATE_MARKER + raw[-half:]

    @staticmethod
    def _format_timestamp(timestamp_ms: Optional[Any]) -> str:
        try:
            value = int(timestamp_ms)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            value = int(time.time() * 1000)
        return ms_to_iso(value)

    @staticmethod
    def _format_elapsed(elapsed_ms: Optional[Any]) -> str:
        try:
            value = float(elapsed_ms)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return "-"
        return f"{value / 1000:.1f}s"
