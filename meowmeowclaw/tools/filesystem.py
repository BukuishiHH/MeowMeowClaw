"""工作区文件工具: read_file / write_file / list_dir.

路径防护集中在本模块顶部的 ``resolve_in_workspace()``:
先归一化绝对路径, 再用 ``os.path.commonpath`` 判定是否落在工作区内,
因此同前缀兄弟目录(如 ``/tmp/ws_evil`` vs ``/tmp/ws``)不会被误放行;
非法路径在触达任何文件系统调用之前就被拦截。
"""

import os
from typing import Any

from .base import BaseTool


class PathOutsideWorkspaceError(ValueError):
    """请求路径越出工作区(由 ``resolve_in_workspace`` 抛出)."""


def resolve_in_workspace(workspace: str, user_path: Any) -> str:
    """
    把用户提供的相对/绝对路径解析为工作区内的绝对路径.

    :param workspace: 工作区根目录(应已存在或即将创建)
    :param user_path: 从模型参数取得的路径; 非 str/PathLike 会抛 TypeError
    :return: 归一化后的绝对路径
    :raises PathOutsideWorkspaceError: 目标落在工作区之外
    :raises TypeError: user_path 不是 str/bytes/os.PathLike
    """
    base = os.path.abspath(os.fspath(workspace))
    raw = os.fspath(user_path)  # None/int 等在这里抛 TypeError, 由调用方包装成可读文本
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    candidate = os.path.abspath(os.path.join(base, raw))

    try:
        common = os.path.commonpath([candidate, base])
    except ValueError as exc:  # Windows 跨盘符等
        raise PathOutsideWorkspaceError(f"无法判定路径归属: {candidate}") from exc

    if common != base:
        raise PathOutsideWorkspaceError(f"路径越出工作区: {candidate}")
    return candidate


class ReadFileTool(BaseTool):
    """
    读取本地文件工具, 带工作区路径防护, 超长内容自动截断
    Args:
        workspace: 工作区根目录的绝对路径, 所有文件均限制于该目录下
    """

    def __init__(self, workspace: str):
        self.workspace = os.path.abspath(workspace)

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "读取工作区内指定文件的文本内容. 只能读取工作目录内文件, 路径穿越会被拦截. 文件内容超过16000字符将被截断. 当需要查看源代码、配置文件、文档内容时调用."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "相对工作区的文件路径, 例如 src/main.py"
                }
            },
            "required": ["file_path"],
            "additionalProperties": False
        }

    async def execute(self, **kwargs: Any) -> str:
        file_rel_path = kwargs.get("file_path", "")
        try:
            absolute_path = resolve_in_workspace(self.workspace, file_rel_path)
        except PathOutsideWorkspaceError:
            return f"[安全拦截] 禁止访问工作区外路径, 请求路径: {file_rel_path}"
        except Exception as exc:  # 非字符串路径等: 包装成可读文本而不是向上抛
            return f"[读取文件异常] {repr(exc)}"

        try:
            with open(absolute_path, "r", encoding="utf-8") as f:
                content = f.read()
            max_len = 16000
            if len(content) > max_len:
                content = content[:max_len] + "\n\n==== 内容已截断, 超过16000字符 ===="
            return content
        except FileNotFoundError:
            return f"[错误] 文件不存在: {absolute_path}"
        except IsADirectoryError:
            return f"[错误]给定路径是目录, 不是文件: {absolute_path}"
        except Exception as e:
            return f"[读取文件异常] {repr(e)}"


class WriteFileTool(BaseTool):
    """
    写入文件工具, 自动创建父目录, 带工作区路径防护
    Args:
        workspace: 工作区根目录的绝对路径, 所有文件均限制于该目录下
    """

    def __init__(self, workspace: str):
        self.workspace = os.path.abspath(workspace)

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "向工作区内写入文本文件, 会覆盖原有内容. 自动创建不存在的父文件夹. 路径穿越会被拦截. 用于新建、修改代码、保存配置和文档."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "相对工作区的目标文件路径, 例如 src/new.py"
                },
                "content": {
                    "type": "string",
                    "description": "要写入文件的文本内容"
                }
            },
            "required": ["file_path", "content"],
            "additionalProperties": False
        }

    async def execute(self, **kwargs: Any) -> str:
        file_rel_path = kwargs.get("file_path", "")
        content = kwargs.get("content", "")
        try:
            absolute_path = resolve_in_workspace(self.workspace, file_rel_path)
        except PathOutsideWorkspaceError:
            return f"[安全拦截] 禁止写入工作区外路径, 请求路径: {file_rel_path}"
        except Exception as exc:
            return f"[写入文件异常] {repr(exc)}"

        try:
            parent_dir = os.path.dirname(absolute_path)
            os.makedirs(parent_dir, exist_ok=True)
            with open(absolute_path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"[成功] 文件已写入: {file_rel_path}"
        except IsADirectoryError:
            return f"[错误] 目标路径是目录, 不能作为文件: {absolute_path}"
        except Exception as e:
            return f"[写入文件异常] {repr(e)}"


class ListDirTool(BaseTool):
    """
    列出目录内容, 带工作区防护, 目录末尾加/, 附带文件大小, 名称排序
    Args:
        workspace: 工作区根目录的绝对路径, 所有文件均限制于该目录下
    """

    def __init__(self, workspace: str):
        self.workspace = os.path.abspath(workspace)

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return "列出工作区内指定目录下的文件和子目录. 目录名称末尾会附加/, 并附带文件字节大小, 结果按名称升序排序. 路径穿越会被拦截. 浏览项目结构、查找文件时调用."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "dir_path": {
                    "type": "string",
                    "description": "相对工作区的目录路径, 空字符串代表工作区根目录"
                }
            },
            "required": ["dir_path"],
            "additionalProperties": False
        }

    async def execute(self, **kwargs: Any) -> str:
        dir_rel_path = kwargs.get("dir_path", "")
        try:
            absolute_path = resolve_in_workspace(self.workspace, dir_rel_path)
        except PathOutsideWorkspaceError:
            return f"[安全拦截] 禁止列出工作区外目录, 请求路径: {dir_rel_path}"
        except Exception as exc:
            return f"[列举目录异常] {repr(exc)}"

        if not os.path.isdir(absolute_path):
            return f"[错误] 路径不是有效目录: {absolute_path}"
        try:
            entries = os.listdir(absolute_path)
            lines = []
            for entry in sorted(entries):
                full_entry_path = os.path.join(absolute_path, entry)
                if os.path.isdir(full_entry_path):
                    lines.append(f"{entry}/")
                else:
                    size = os.path.getsize(full_entry_path)
                    lines.append(f"{entry}  size={size} bytes")
            result = "\n".join(lines)
            return result if result else "目录为空"
        except PermissionError:
            return f"[错误] 无权限读取目录: {absolute_path}"
        except Exception as e:
            return f"[列举目录异常] {repr(e)}"
