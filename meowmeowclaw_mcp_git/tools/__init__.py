"""工具声明与处理器(按安全等级分组).

GM1: ``git_status``; GM2: 其余只读工具(diff/log/show/blame/file_at_revision/
list_branches/list_tags/list_remotes/rev_parse/config_get/grep/search_history)。
写/远端/破坏性工具分别在 GM3/GM4 加入, 由 ``app.select_tools`` 按权限矩阵过滤。
"""

from .base import (
    LEVEL_DESTRUCTIVE,
    LEVEL_READ,
    LEVEL_REMOTE,
    LEVEL_WRITE,
    TOOL_LEVELS,
    ToolContext,
    ToolOutcome,
    ToolSpec,
)
from .read import build_core_tools
from .refs import build_ref_tools
from .remote import build_remote_tools
from .search import build_search_tools
from .write import build_write_tools

__all__ = [
    "LEVEL_DESTRUCTIVE",
    "LEVEL_READ",
    "LEVEL_REMOTE",
    "LEVEL_WRITE",
    "TOOL_LEVELS",
    "ToolContext",
    "ToolOutcome",
    "ToolSpec",
    "build_core_tools",
    "build_available_tools",
    "build_read_tools",
    "build_ref_tools",
    "build_remote_tools",
    "build_search_tools",
    "build_write_tools",
]


def build_read_tools() -> tuple[ToolSpec, ...]:
    """全部只读工具(GM1 + GM2)."""
    return build_core_tools() + build_ref_tools() + build_search_tools()


def build_available_tools() -> tuple[ToolSpec, ...]:
    """服务器可提供的全部工具(只读 + 写 + 远端); 由 ``app.select_tools`` 按权限矩阵过滤."""
    return build_read_tools() + build_write_tools() + build_remote_tools()
