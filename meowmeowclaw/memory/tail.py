"""会话 JSONL 的尾读工具(A+ A2, 见 ``docs/A_PLUS_OPTIMIZATION.md`` §3.2).

只提供"从文件末尾反向读取完整行"的底层能力, 不感知会话语义:

- :func:`read_header` —— 读首行并解析为 dict(损坏/非 dict → ``None``);
- :func:`read_last_lines` —— 反向块读, 返回末尾完整行(**从新到旧**, 不含行尾 ``\\n``);
- :func:`read_last_valid_turn` —— 反向找最后一条 ``type == "turn"`` 的合法 dict 记录。

**行为等价约定**(必须与全量读取路径 ``jsonl.py::_read_turns`` 一致, A3 会做差分测试):

1. 二进制读取, 只在 ``b"\\n"`` 切出的**整行**上解码 —— 不会把 UTF-8 多字节字符截断;
2. 末尾没有换行的那一段仍作为**候选行**返回(可能是一次写全但缺尾换行的记录, 也可能是
   崩溃半行): 交给调用方的 JSON 解析判定 —— 解析失败即跳过, 与全量路径完全一致;
3. 空行(纯空白)不返回;
4. I/O 异常(``OSError``)与解码异常(``UnicodeDecodeError``)向上抛, 由调用方决定回退全量扫描;
5. 只读不改: 不写入、不创建文件; 文件不存在时 ``open`` 会抛 ``FileNotFoundError``(``OSError`` 子类)。
"""

import json
import os
from pathlib import Path
from typing import Any, Iterator, Optional, Union

# 反向块读的块大小(64KB): 单块已能覆盖多数"最后一行"; 超大行会自动多读几块
BLOCK_SIZE = 64 * 1024

__all__ = ["BLOCK_SIZE", "read_header", "read_last_lines", "read_last_valid_turn"]


def _parse_line(raw: bytes) -> Optional[dict[str, Any]]:
    """字节行 -> dict; 空白/非 UTF-8/非法 JSON/非 dict 一律 None."""
    text = raw.strip()
    if not text:
        return None
    try:
        record = json.loads(text.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def read_header(path: Union[str, Path]) -> Optional[dict[str, Any]]:
    """读取并解析首行; 首行损坏/为空白/非 dict/文件为空 → None(只读一行, O(1))."""
    with open(Path(path), "rb") as handle:
        raw = handle.readline()
    return _parse_line(raw)


def _iter_last_lines(path: Union[str, Path]) -> Iterator[bytes]:
    """惰性反向产出完整行(**新 → 旧**); 调用方可以提前 ``break``(命中即停).

    这是尾读的核心: ``read_last_valid_turn`` 只需读到第一条有效 turn 就停,
    因此正常文件只读最后一个块(默认 64KB), 与文件总长度无关。
    """
    with open(Path(path), "rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        carry = b""  # 更靠前、尚未凑成完整行的一段
        while position > 0:
            size = min(BLOCK_SIZE, position)
            position -= size
            handle.seek(position)
            chunk = handle.read(size) + carry
            parts = chunk.split(b"\n")
            carry = parts[0]  # 最旧的一段可能不完整, 留给下一轮
            # parts[1:] 是本次读到的行(旧 → 新); 逐条从新到旧产出
            for raw in reversed(parts[1:]):
                if raw.strip():
                    yield raw
        if carry.strip() and position == 0:
            # 文件不以换行结尾/首行没有换行: 仍作为候选行产出(由调用方解析判定)
            yield carry


def read_last_lines(
    path: Union[str, Path],
    *,
    max_lines: Optional[int] = None,
    max_bytes: Optional[int] = None,
) -> list[bytes]:
    """反向块读文件末尾的完整行, 返回**从新到旧**的字节行(不含行尾 ``\n``).

    - ``max_lines``: 最多返回多少行(``<=0`` 直接返回空);
    - ``max_bytes``: 累计行字节达到该值后停止继续向前读(最后一行可能略微超出);
    - 两个上限都为 ``None`` 时会一路读到文件头(``read_last_valid_turn`` 用的是惰性版本,
      命中即停, 不会受此影响)。
    """
    if max_lines is not None and max_lines <= 0:
        return []
    if max_bytes is not None and max_bytes <= 0:
        return []

    lines: list[bytes] = []
    total_bytes = 0
    for raw in _iter_last_lines(path):
        lines.append(raw)
        total_bytes += len(raw) + 1
        if max_lines is not None and len(lines) >= max_lines:
            break
        if max_bytes is not None and total_bytes >= max_bytes:
            break
    return lines


def read_last_valid_turn(path: Union[str, Path]) -> Optional[dict[str, Any]]:
    """反向找最后一条合法的 turn 记录; 全是损坏行/没有 turn → None.

    惰性读取 + 命中即停: 正常文件只读末尾一个块(≤64KB), 与文件总长度无关。
    """
    for raw in _iter_last_lines(path):
        record = _parse_line(raw)
        if record is not None and record.get("type") == "turn":
            return record
    return None
