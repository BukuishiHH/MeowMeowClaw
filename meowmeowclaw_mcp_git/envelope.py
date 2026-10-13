"""统一返回信封(``docs/GIT_MCP_DESIGN.md`` D8/§4.2).

- 成功: ``{"ok": true, "data": {...}, "meta": {...}}``
- 失败: ``{"ok": false, "error_code": "...", "message": "...", "meta": {...}}``

``meta`` 始终存在(可能为空对象), 保证工具返回形状稳定, 模型无需分支处理。
序列化用紧凑 JSON + 不转义中文, 控制上下文占用(G3)。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional


def ok(data: Optional[Mapping[str, Any]] = None, meta: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """构造成功信封."""
    return {
        "ok": True,
        "data": dict(data or {}),
        "meta": dict(meta or {}),
    }


def failure(
    error_code: str,
    message: str,
    meta: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """构造失败信封."""
    return {
        "ok": False,
        "error_code": error_code,
        "message": message,
        "meta": dict(meta or {}),
    }


def dumps(payload: Mapping[str, Any]) -> str:
    """序列化为 MCP TextContent 使用的 JSON 文本."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
