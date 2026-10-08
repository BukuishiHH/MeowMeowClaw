"""System Prompt 与 messages 构建器.

把工作区路径、人设文件、当前时间、长期记忆组装成 LLM 需要的 System Prompt,
并按 OpenAI 消息格式拼出完整 messages 列表.

用法::

    builder = ContextBuilder(
        workspace=Path("/path/to/workspace"),
        identity_path=Path("/path/to/identity.md"),
    )
    messages = builder.build_messages(history=history, current_message="帮我改个 bug")

人设文件路径由装配层显式传入(固定为 ``<项目根>/identity.md``); 读取失败/内容为空时
回退 ``DEFAULT_IDENTITY``。本模块不计算项目根, 也不自己查找候选路径。
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)

# 人设文件不可用时的兜底人设
DEFAULT_IDENTITY = "你是 MeowMeowClaw, 一个善解人意的 AI 助手, 可以调用工具帮用户读写文件、分析代码."

# 长期记忆文件相对工作区的位置(预留能力: 放入该文件后会自动进入 System Prompt)
MEMORY_RELATIVE_PATH = Path("memory") / "MEMORY.md"

# 当前时间格式: 2025-03-05 09:08 (Wednesday)
TIME_FORMAT = "%Y-%m-%d %H:%M (%A)"


class ContextBuilder:
    """
    System Prompt 与 messages 构建器

    Args:
        workspace: 工作区根目录, 也是文件工具的操作范围(会归一化为绝对路径)
        identity_path: 人设文件路径; 由装配层传入绝对路径(通常为 ``<项目根>/identity.md``),
                       相对路径按进程当前工作目录解析
        skills_summary: 技能摘要文本(来自 SkillsLoader.build_skills_summary());
            非空时在 System Prompt 末尾追加 "## 可用技能" 章节

    注意:
        - 每次 build_system_prompt() 都重新读盘, 因此运行中修改人设/记忆会立即生效;
        - 人设文件缺失/不可读/空白时警告并回退 DEFAULT_IDENTITY, 不阻断 Agent 启动
    """

    def __init__(
        self,
        workspace: Union[str, Path],
        identity_path: Union[str, Path],
        skills_summary: str = "",
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.identity_path = Path(identity_path).expanduser().resolve()
        # 技能摘要由调用方(如 main.build_agent)注入, ContextBuilder 不关心技能从哪来
        self.skills_summary = skills_summary

    def __repr__(self) -> str:
        return (
            f"<ContextBuilder workspace={str(self.workspace)!r} "
            f"identity_path={str(self.identity_path)!r}>"
        )

    @property
    def memory_path(self) -> Path:
        """长期记忆文件路径: workspace/memory/MEMORY.md"""
        return self.workspace / MEMORY_RELATIVE_PATH

    # ------------------------------------------------------------------ 私有加载

    def _load_identity(self) -> str:
        """读取人设文件; 缺失/不可读/空白一律警告并回退 DEFAULT_IDENTITY."""
        try:
            with open(self.identity_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except FileNotFoundError:
            logger.warning("人设文件不存在, 使用默认人设: %s", self.identity_path)
            return DEFAULT_IDENTITY
        except UnicodeDecodeError as exc:
            logger.warning(
                "人设文件不是合法 UTF-8 文本, 使用默认人设: %s (%r)", self.identity_path, exc
            )
            return DEFAULT_IDENTITY
        except OSError as exc:  # IsADirectoryError / PermissionError 等
            logger.warning("读取人设文件失败, 使用默认人设: %s (%r)", self.identity_path, exc)
            return DEFAULT_IDENTITY

        if not content:
            logger.warning("人设文件为空, 使用默认人设: %s", self.identity_path)
            return DEFAULT_IDENTITY
        return content

    def _load_memory(self) -> str:
        """读取工作区长期记忆; 不存在或不可读时返回空字符串."""
        try:
            with open(self.memory_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("读取长期记忆失败, 本次忽略: %s (%r)", self.memory_path, exc)
            return ""

    # ------------------------------------------------------------------ 对外方法

    def build_system_prompt(self) -> str:
        """拼接完整 System Prompt: 人设 + 当前时间 + 工作区 + 长期记忆 + 可用技能(后两者非空才拼)."""
        sections = [
            self._load_identity(),
            f"## 当前时间\n{self._format_now()}",
            f"## 工作区\n所有文件操作都限制在工作区目录内, 根目录: {self.workspace}",
        ]

        memory = self._load_memory()
        if memory:  # 预留能力: 无记忆时不输出空章节
            sections.append(f"## 长期记忆\n{memory}")

        if self.skills_summary:  # 无技能时不输出空章节
            sections.append(f"## 可用技能\n{self.skills_summary}")

        return "\n\n".join(sections)

    def build_messages(
        self,
        history: Optional[list[dict[str, Any]]] = None,
        current_message: str = "",
    ) -> list[dict[str, Any]]:
        """
        构建完整 messages 列表 = [System Prompt] + 历史对话 + 当前用户消息

        :param history: 历史消息(OpenAI 格式), 不修改入参列表
        :param current_message: 当前用户消息; 为空时不追加(避免空 user 消息被网关拒绝)
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.build_system_prompt()}
        ]
        if history:
            messages.extend(history)
        if current_message:
            messages.append({"role": "user", "content": current_message})
        return messages

    @staticmethod
    def _format_now() -> str:
        """当前时间文本, 如 2025-03-05 09:08 (Wednesday); 每次调用实时取值."""
        return datetime.now().strftime(TIME_FORMAT)
