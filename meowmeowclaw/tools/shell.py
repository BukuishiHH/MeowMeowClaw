"""Shell 命令执行工具(工作区内执行, 带危险命令黑名单防护).

⚠️ 安全声明: 本工具把"执行任意 Shell 命令"的能力交给模型, 黑名单只能拦住
**显式书写**的危险命令, 无法防御 `$(...)`/反引号/base64 解码执行/脚本文件等绕过手段.
它是**护栏而非沙箱**. 生产环境应当: 放在容器或受限用户下运行、或默认不注册本工具.
"""

import asyncio
import logging
import os
import re
import signal
from typing import Any, Optional

from .base import BaseTool

logger = logging.getLogger(__name__)

# 命令执行超时(秒)与输出上限(字符)
EXEC_TIMEOUT_SECONDS = 60
MAX_OUTPUT_CHARS = 10000
TRUNCATE_NOTICE = "\n...(输出过长, 已截断)"

# 危险命令黑名单(正则, 匹配时忽略大小写)
# 说明: 这是**黑名单**, 只覆盖常见破坏性命令; 顺序即优先级, 命中即返回该条模式
DENY_PATTERNS: tuple[str, ...] = (
    r"rm\s+.*-r",            # 递归删除(含 rm -rf)
    r"rm\s+-rf",             # 递归强制删除
    r"rmdir\s+/s",           # Windows 递归删除目录
    r"format\s+",            # 格式化磁盘
    r"mkfs",                 # Linux 格式化文件系统
    r"shutdown",             # 关机
    r"reboot",               # 重启
    r"sudo\s+",              # 权限提升
    r"\bsu\b",               # 切换用户
    r"chmod\s+777",          # 危险权限(全局可写可执行)
    r">\s*/dev/",            # 覆盖设备文件
    r"wget\s+.*\|\s*sh",     # 下载并直接执行
    r"curl\s+.*\|\s*bash",   # 下载并直接执行
    r"nc\s+-l",              # 开监听后门
    r"ncat\s+-l",            # 开监听后门
    r"dd\s+if=",             # 磁盘镜像覆写
    r":\(\)\{.*\}",          # Fork 炸弹
)


class ExecTool(BaseTool):
    """
    在工作目录下执行 Shell 命令并回收输出

    Args:
        workspace: 命令执行的工作目录(会归一化为绝对路径); 默认当前目录

    行为约定:
        - 危险命令直接被拦截, **不会**创建任何进程;
        - 超时 60 秒, 超时后杀掉整个进程组(避免 shell 的子进程变成孤儿);
        - 标准输出与标准错误合并返回, 标准错误带 "标准错误:" 前缀;
        - 输出超过 10000 字符截断, 末尾附截断提示, 再附退出码;
        - 任何异常都会被包装成文本返回, 不会抛给上层.
    """

    # 子类可覆盖以扩展/收紧黑名单
    deny_patterns: tuple[str, ...] = DENY_PATTERNS

    def __init__(self, workspace: str = ".") -> None:
        self.workspace = os.path.abspath(workspace)

    def __repr__(self) -> str:
        return f"<Tool name={self.name}, label={self.label}, workspace={self.workspace!r}>"

    # ------------------------------------------------------------------ 工具契约

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return (
            f"在工作目录下执行 Shell 命令并返回输出(含退出码). 命令在 {self.workspace} 下执行, "
            f"超时 {EXEC_TIMEOUT_SECONDS} 秒, 输出超过 {MAX_OUTPUT_CHARS} 字符会被截断, "
            "危险命令(递归删除/格式化/关机/提权等)会被安全拦截. "
            "用于运行测试、查看系统信息、执行构建脚本等."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 Shell 命令, 例如 pytest -q 或 ls -la src",
                }
            },
            "required": ["command"],
            "additionalProperties": False,
        }

    # ------------------------------------------------------------------ 安全防护

    def _is_dangerous(self, command: str) -> Optional[str]:
        """
        按黑名单检查命令

        :return: 命中时返回拦截提示文本, 否则返回 None
        """
        for pattern in self.deny_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return f"安全拦截: 检测到危险命令模式 '{pattern}'"
        return None

    # ------------------------------------------------------------------ 执行

    async def execute(self, **kwargs: Any) -> str:
        """
        执行命令

        :param kwargs: 需包含 command(str)
        :return: 命令输出 + 退出码; 被拦截/超时/异常时返回可读提示文本
        """
        command = str(kwargs.get("command") or "").strip()
        if not command:
            return "[错误] 命令不能为空"

        blocked = self._is_dangerous(command)
        if blocked:
            logger.warning("拦截危险命令: %r", command)
            return blocked

        if not os.path.isdir(self.workspace):
            return f"[错误] 工作目录不存在: {self.workspace}"

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace,
                start_new_session=True,  # 独立进程组, 便于超时后整组清理
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=EXEC_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                await self._terminate(process)
                logger.warning("命令执行超时(%s 秒), 已终止: %r", EXEC_TIMEOUT_SECONDS, command)
                return f"命令执行超时({EXEC_TIMEOUT_SECONDS}秒), 已终止"
        except Exception as exc:  # noqa: BLE001 任何异常都转成文本, 不炸主循环
            logger.warning("命令执行异常: %r (%r)", command, exc)
            return f"[命令执行异常] {exc!r}"

        return self._format_result(stdout, stderr, process.returncode)

    async def _terminate(self, process: Any) -> None:
        """超时清理: 优先杀掉整个进程组(避免 shell 的子进程成为孤儿), 再兜底杀单进程."""
        try:
            if os.name == "posix" and hasattr(os, "killpg"):
                pgid = os.getpgid(process.pid)
                # 安全阀: 只有子进程处于**自己的**进程组(由 start_new_session 保证)时才整组清理.
                # 否则 killpg 会打到我们自己的进程组 -- 那等于自杀/误杀父进程.
                if pgid != os.getpgid(0):
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    logger.warning("子进程与父进程同组(未开启独立会话), 退化为 kill 单进程")
                    process.kill()
            else:  # pragma: no cover - 非 POSIX 平台
                process.kill()
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.debug("杀进程组失败(%r), 退化为 kill 单进程", exc)
            try:
                process.kill()
            except ProcessLookupError:
                pass

        try:  # 回收子进程, 避免僵尸
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:  # pragma: no cover - 极端情况
            logger.warning("终止命令后回收进程超时")

    @staticmethod
    def _format_result(stdout: bytes, stderr: bytes, returncode: Optional[int]) -> str:
        """拼装 stdout + stderr(带前缀) → 截断 → 附退出码."""
        out_text = stdout.decode("utf-8", errors="replace").strip()
        err_text = stderr.decode("utf-8", errors="replace").strip()

        parts = [out_text] if out_text else []
        if err_text:
            parts.append(f"标准错误:\n{err_text}")
        text = "\n".join(parts)

        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS] + TRUNCATE_NOTICE

        return f"{text}\n[退出码: {returncode}]" if text else f"[退出码: {returncode}]"
