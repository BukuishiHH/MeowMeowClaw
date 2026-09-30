"""System Prompt 与 messages 构建器.

把工作区路径、人设文件、当前时间、长期记忆组装成 LLM 需要的 System Prompt,
并按 OpenAI 消息格式拼出完整 messages 列表.

用法::

    builder = ContextBuilder(workspace="/path/to/project")
    messages = builder.build_messages(history=history, current_message="帮我改个 bug")

人设文件的查找顺序(前者优先):
    1. workspace/identity_file     -- 工作区里的人设, 随项目走
    2. backend/identity_file       -- 项目自带兜底人设(identity.md 就放在这里)
两者都不可用时回退 DEFAULT_IDENTITY.
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 人设兜底目录: 项目 backend/ 目录(与 agent/ 同级), 工作区里没放人设时用它自带的 identity.md
FALLBACK_IDENTITY_DIR = str(Path(__file__).resolve().parents[1])

# 人设文件全都不可用时的兜底人设
DEFAULT_IDENTITY = "你是 MeowMeowClaw, 一个善解人意的 AI 助手, 可以调用工具帮用户读写文件、分析代码."

# 长期记忆文件相对工作区的位置(预留能力: 放入该文件后会自动进入 System Prompt)
MEMORY_RELATIVE_PATH = os.path.join("memory", "MEMORY.md")

# 当前时间格式: 2025-03-05 09:08 (Wednesday)
TIME_FORMAT = "%Y-%m-%d %H:%M (%A)"


class ContextBuilder:
    """
    System Prompt 与 messages 构建器

    Args:
        workspace: 工作区根目录, 也是文件工具的操作范围(会归一化为绝对路径)
        identity_file: 人设文件名; 绝对路径直接使用, 相对路径先查 workspace/ 再兜底 backend/

    注意:
        - 每次 build_system_prompt() 都重新读盘, 因此运行中修改人设/记忆会立即生效;
        - 人设文件缺失/不可读/空白时依次尝试下一个候选, 全部失败才用默认人设, 不阻断 Agent 启动
    """

    def __init__(self, workspace: str, identity_file: str = "identity.md") -> None:
        self.workspace = os.path.abspath(workspace)
        self.identity_file = identity_file
        # 候选路径按优先级排列: [工作区, backend/]
        self.identity_candidates = self._resolve_identity_candidates(identity_file)
        self.identity_path = self.identity_candidates[0]            # 首选: 工作区
        self.fallback_identity_path = self.identity_candidates[1]   # 兜底: 项目 backend/

    def __repr__(self) -> str:
        return (
            f"<ContextBuilder workspace={self.workspace!r} identity_file={self.identity_file!r}>"
        )

    # ------------------------------------------------------------------ 路径解析

    def _resolve_identity_candidates(self, identity_file: str) -> tuple[str, str]:
        """
        解析人设文件候选路径

        :return: (首选路径, 兜底路径); 首个是工作区下的文件, 次个是 backend/ 下的同名文件
        """
        primary = (
            identity_file
            if os.path.isabs(identity_file)
            else os.path.join(self.workspace, identity_file)
        )
        fallback = os.path.join(FALLBACK_IDENTITY_DIR, os.path.basename(identity_file))
        return primary, fallback

    @property
    def memory_path(self) -> str:
        """长期记忆文件路径: workspace/memory/MEMORY.md"""
        return os.path.join(self.workspace, MEMORY_RELATIVE_PATH)

    # ------------------------------------------------------------------ 私有加载

    def _load_identity(self) -> str:
        """按 工作区 → backend/ 顺序取第一个非空人设; 都不可用则返回默认人设."""
        for path in self.identity_candidates:
            content = self._read_identity_candidate(path)
            if content:
                return content

        logger.warning(
            "人设文件均不可用, 使用默认人设(候选: %s)", " | ".join(self.identity_candidates)
        )
        return DEFAULT_IDENTITY

    @staticmethod
    def _read_identity_candidate(path: str) -> str:
        """
        读取单个候选人设文件

        :return: 文件内容(已去首尾空白); 不存在/不可读/非 UTF-8/空白一律返回空串,
                 以便继续尝试下一个候选, 由调用方决定最终兜底
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""
        except UnicodeDecodeError as exc:
            logger.warning("人设文件不是合法 UTF-8 文本, 跳过该候选: %s (%r)", path, exc)
            return ""
        except OSError as exc:  # IsADirectoryError / PermissionError 等
            logger.warning("读取人设文件失败, 跳过该候选: %s (%r)", path, exc)
            return ""

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
        """拼接完整 System Prompt: 人设 + 当前时间 + 工作区 + 长期记忆(非空才拼)."""
        sections = [
            self._load_identity(),
            f"## 当前时间\n{self._format_now()}",
            f"## 工作区\n所有文件操作都限制在工作区目录内, 根目录: {self.workspace}",
        ]

        memory = self._load_memory()
        if memory:  # 预留能力: 无记忆时不输出空章节
            sections.append(f"## 长期记忆\n{memory}")

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
