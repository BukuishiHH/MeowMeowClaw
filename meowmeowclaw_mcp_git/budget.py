"""输出预算(§4.2/§7.3/G3): 按字节/行裁剪文本与结构化列表, 并标记 ``truncated``.

- 文本类工具(diff/show patch、file、blame、grep)共用; 裁剪在 UTF-8 边界上做,
  截断处丢弃半个字符, 保证 JSON 可序列化;
- ``total_*`` 保留完整尺寸, 便于模型决定是否需要缩小范围或分段读取;
- 结构化列表(blame 行/grep 匹配)按紧凑 JSON 的字节数累计裁剪, 避免逐条累加后
  整个信封超限。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional, Sequence

# 文本类工具的默认预算(可由工具参数 max_bytes 调整, 同时受 --max-output-bytes 硬约束)
DEFAULT_TEXT_BYTES = 32_768
DEFAULT_FILE_BYTES = 65_536
DEFAULT_ITEMS_BYTES = 32_768


@dataclass(frozen=True)
class TextBudget:
    """裁剪结果(``total_*`` 是完整尺寸, ``returned_*`` 是实际返回尺寸)."""

    text: str
    truncated: bool
    total_bytes: int
    total_lines: int
    returned_bytes: int
    returned_lines: int

    def to_dict(self) -> dict[str, Any]:
        """并入工具 data 的尺寸信息."""
        return {
            "truncated": self.truncated,
            "total_bytes": self.total_bytes,
            "total_lines": self.total_lines,
            "returned_bytes": self.returned_bytes,
            "returned_lines": self.returned_lines,
        }


def count_lines(text: str) -> int:
    """行数; 末尾换行不额外算一行, 空文本为 0."""
    if not text:
        return 0
    newlines = text.count("\n")
    return newlines if text.endswith("\n") else newlines + 1


def utf8_size(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def fit_text(text: str, *, max_bytes: int, max_lines: Optional[int] = None) -> TextBudget:
    """把文本裁剪到 ``max_bytes``/``max_lines`` 之内(先按行、再按字节)."""
    if max_bytes <= 0:
        raise ValueError("max_bytes 必须 > 0")
    total_bytes = utf8_size(text)
    total_lines = count_lines(text)
    truncated = False
    prefix = text
    if max_lines is not None and max_lines >= 0 and total_lines > max_lines:
        truncated = True
        if max_lines == 0:
            prefix = ""
        else:
            prefix = "".join(text.splitlines(keepends=True)[:max_lines])
    if utf8_size(prefix) > max_bytes:
        truncated = True
        raw = prefix.encode("utf-8", errors="replace")[:max_bytes]
        prefix = raw.decode("utf-8", errors="ignore")  # 丢弃半个多字节字符
    return TextBudget(
        text=prefix,
        truncated=truncated,
        total_bytes=total_bytes,
        total_lines=total_lines,
        returned_bytes=utf8_size(prefix),
        returned_lines=count_lines(prefix),
    )


def item_size(item: Any) -> int:
    """单条结构化数据的紧凑 JSON 字节数(含分隔符预留 1 字节)."""
    return utf8_size(json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)) + 1


def fit_items(
    items: Sequence[Any],
    *,
    max_bytes: int,
    max_items: Optional[int] = None,
) -> tuple[list[Any], bool, int]:
    """按字节/条数裁剪结构化列表; 返回 ``(保留列表, 是否截断, 完整字节数)``."""
    if max_bytes <= 0:
        raise ValueError("max_bytes 必须 > 0")
    total_bytes = sum(item_size(item) for item in items)
    kept: list[Any] = []
    used = 0
    for item in items:
        if max_items is not None and len(kept) >= max_items:
            break
        size = item_size(item)
        if used + size > max_bytes:
            break
        kept.append(item)
        used += size
    truncated = len(kept) < len(items)
    return kept, truncated, total_bytes
