"""工具框架: 契约与注册表.

本包只导出 ``BaseTool`` / ``ToolRegistry``; 具体工具由装配层(组合根)按需 import 并注册,
因此 ``import meowmeowclaw.tools`` 不会拉起任何具体工具, 也不会触发配置/网络等副作用。
"""

from .base import BaseTool
from .registry import ToolRegistry

__all__ = [
    "BaseTool",
    "ToolRegistry",
]
