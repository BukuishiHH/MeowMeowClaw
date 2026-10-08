"""工作区文件工具: read_file / write_file / list_dir.

防护分两层:
- 工作区路径校验: 先归一化绝对路径, 再用 ``os.path.commonpath`` 判定 (见 ``resolve_in_workspace``);
- 记忆目录策略(D27): ``<memory_dir>/sessions|active|archive`` 对三件套全禁;
  ``memory/MEMORY.md`` 可读可写, 且 ``write_file`` 覆盖前会把上一版滚动备份为
  ``MEMORY.md.bak``(D32), 避免模型一次覆盖清空长期记忆。
"""

import os
import shutil
from typing import Any, Optional, Union

from .base import BaseTool

MEMORY_FILE_NAME = "MEMORY.md"
MEMORY_BACKUP_NAME = "MEMORY.md.bak"
RUNTIME_MEMORY_DIRS = ("sessions", "active", "archive")


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
    # realpath: 把工作区内指向外部的符号链接解析掉, 防止绕过路径防护(M7)
    base = os.path.realpath(os.path.abspath(os.fspath(workspace)))
    raw = os.fspath(user_path)  # None/int 等在这里抛 TypeError, 由调用方包装成可读文本
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    candidate = os.path.realpath(os.path.abspath(os.path.join(base, raw)))

    try:
        common = os.path.commonpath([candidate, base])
    except ValueError as exc:  # Windows 跨盘符等
        raise PathOutsideWorkspaceError(f"无法判定路径归属: {candidate}") from exc

    if common != base:
        raise PathOutsideWorkspaceError(f"路径越出工作区: {candidate}")
    return candidate


def is_within(path: str, base: str) -> bool:
    """path 是否落在 base 内(commonpath 判定, 防同前缀兄弟目录绕过)."""
    try:
        real_path = os.path.realpath(os.path.abspath(path))
        real_base = os.path.realpath(os.path.abspath(base))
        return os.path.commonpath([real_path, real_base]) == real_base
    except ValueError:  # Windows 跨盘符
        return False


def is_memory_denied(absolute_path: str, memory_dir: str, *, listing: bool = False) -> bool:
    """
    判断路径是否命中记忆运行时目录禁令.

    - ``memory/sessions|active|archive`` 及其子路径: 读写列举全禁;
    - ``memory`` 根目录: 仅列举时拒绝(读写本身会因"IsADirectory"失败);
    - 其他路径(含 ``memory/MEMORY.md``): 放行。
    """
    real_path = os.path.realpath(os.path.abspath(absolute_path))
    real_memory = os.path.realpath(os.path.abspath(memory_dir))
    if not is_within(real_path, real_memory):
        return False
    relative = os.path.relpath(real_path, real_memory)
    if relative in (os.curdir, ""):
        return listing
    top = relative.split(os.sep, 1)[0]
    return top in RUNTIME_MEMORY_DIRS


def memory_file_path(memory_dir: str) -> str:
    """长期记忆文件路径 ``<memory_dir>/MEMORY.md``."""
    return os.path.join(memory_dir, MEMORY_FILE_NAME)


def backup_existing_memory(memory_path: str, backup_path: Optional[str] = None) -> None:
    """
    把现有长期记忆滚动备份为 ``MEMORY.md.bak``(单份覆盖).

    文件不存在则什么都不做; 备份失败会向上抛 OSError, 由调用方决定是否阻止写入。
    """
    if not os.path.isfile(memory_path):
        return
    target = backup_path or os.path.join(
        os.path.dirname(memory_path), MEMORY_BACKUP_NAME
    )
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(memory_path, target)


class _MemoryAwareTool(BaseTool):
    """带工作区 + 记忆目录策略的工具基类(仅内部复用)."""

    def __init__(
        self,
        workspace: Union[str, os.PathLike],
        memory_dir: Optional[Union[str, os.PathLike]] = None,
    ) -> None:
        self.workspace = os.path.abspath(os.fspath(workspace))
        if memory_dir is None:
            self.memory_dir = os.path.join(self.workspace, "memory")
        else:
            self.memory_dir = os.path.abspath(os.fspath(memory_dir))

    def _denied(self, absolute_path: str, *, listing: bool = False) -> bool:
        return is_memory_denied(absolute_path, self.memory_dir, listing=listing)

    def _is_memory_file(self, absolute_path: str) -> bool:
        return os.path.normcase(os.path.realpath(absolute_path)) == os.path.normcase(
            os.path.realpath(memory_file_path(self.memory_dir))
        )


class ReadFileTool(_MemoryAwareTool):
    """
    读取本地文件工具, 带工作区路径防护与记忆目录策略, 超长内容自动截断

    Args:
        workspace: 工作区根目录, 所有文件均限制于该目录下
        memory_dir: 记忆目录(默认 ``<workspace>/memory``); 其运行时子目录禁止读取
    """

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "读取工作区内指定文件的文本内容. 只能读取工作目录内文件, 路径穿越会被拦截. 记忆运行时目录禁止读取, 长期记忆 MEMORY.md 可读. 文件内容超过16000字符将被截断. 当需要查看源代码、配置文件、文档内容时调用."

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

        if self._denied(absolute_path):
            return f"[安全拦截] 禁止读取记忆运行时目录, 请求路径: {file_rel_path}"

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


class WriteFileTool(_MemoryAwareTool):
    """
    写入文件工具, 自动创建父目录, 带工作区路径防护与记忆目录策略

    写 ``memory/MEMORY.md`` 时, 会先把现有内容滚动备份为 ``MEMORY.md.bak``(D32)。

    Args:
        workspace: 工作区根目录, 所有文件均限制于该目录下
        memory_dir: 记忆目录(默认 ``<workspace>/memory``)
    """

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "向工作区内写入文本文件, 会覆盖原有内容. 自动创建不存在的父文件夹. 路径穿越会被拦截. 记忆运行时目录禁止写入; 覆盖长期记忆 MEMORY.md 前会自动备份上一版. 用于新建、修改代码、保存配置和文档."

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

        if self._denied(absolute_path):
            return f"[安全拦截] 禁止写入记忆运行时目录, 请求路径: {file_rel_path}"

        try:
            if self._is_memory_file(absolute_path):
                # 备份失败则不写入, 避免无保护地清空长期记忆
                backup_existing_memory(absolute_path)
            parent_dir = os.path.dirname(absolute_path)
            os.makedirs(parent_dir, exist_ok=True)
            with open(absolute_path, "w", encoding="utf-8") as f:
                f.write(content)
            if self._is_memory_file(absolute_path):
                return f"[成功] 长期记忆已写入(上一版备份为 {MEMORY_BACKUP_NAME}): {file_rel_path}"
            return f"[成功] 文件已写入: {file_rel_path}"
        except IsADirectoryError:
            return f"[错误] 目标路径是目录, 不能作为文件: {absolute_path}"
        except Exception as e:
            return f"[写入文件异常] {repr(e)}"


class ListDirTool(_MemoryAwareTool):
    """
    列出目录内容, 带工作区防护与记忆目录策略, 目录末尾加/, 附带文件大小, 名称排序

    Args:
        workspace: 工作区根目录, 所有文件均限制于该目录下
        memory_dir: 记忆目录(默认 ``<workspace>/memory``); 根目录与运行时子目录禁止列举
    """

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return "列出工作区内指定目录下的文件和子目录. 目录名称末尾会附加/, 并附带文件字节大小, 结果按名称升序排序. 路径穿越会被拦截. 记忆运行时目录禁止列举. 浏览项目结构、查找文件时调用."

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

        if self._denied(absolute_path, listing=True):
            return f"[安全拦截] 禁止列举记忆运行时目录, 请求路径: {dir_rel_path}"

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
