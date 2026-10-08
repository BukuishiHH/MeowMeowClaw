import os
from typing import Any
from meowmeowclaw.agent.tools import BaseTool


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
        absolute_path = os.path.abspath(os.path.join(self.workspace, file_rel_path))
        # 路径安全校验
        if not absolute_path.startswith(self.workspace):
            return f"[安全拦截] 禁止访问工作区外路径, 请求路径: {file_rel_path}"
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
        absolute_path = os.path.abspath(os.path.join(self.workspace, file_rel_path))
        if not absolute_path.startswith(self.workspace):
            return f"[安全拦截] 禁止写入工作区外路径, 请求路径: {file_rel_path}"
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

    async def execute(self,** kwargs: Any) -> str:
        dir_rel_path = kwargs.get("dir_path", "")
        absolute_path = os.path.abspath(os.path.join(self.workspace, dir_rel_path))
        if not absolute_path.startswith(self.workspace):
            return f"[安全拦截] 禁止列出工作区外目录, 请求路径: {dir_rel_path}"
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