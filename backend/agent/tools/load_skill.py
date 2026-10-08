"""把技能加载能力暴露成工具, 供模型按需取回 SKILL.md 正文.

为什么要有这个工具: 技能文件位于工作区下的 skills/ 目录, 而 read_file 的路径是相对
工作区解析的, 模型按摘要里的相对路径去 read_file 会差一层目录; 更重要的是 read_file
会把 frontmatter 一并读回来。用专门的工具直接返回"去掉 frontmatter 的正文"更干净,
也更符合"技能按需加载"的语义。
"""

import logging
from typing import Any

from backend.agent.skills import SkillsLoader
from backend.agent.tools import BaseTool

logger = logging.getLogger(__name__)

MAX_SKILL_CHARS = 16000  # 与 read_file 保持一致
TRUNCATE_NOTICE = "\n...(内容过长, 已截断)"


class LoadSkillTool(BaseTool):
    """
    按名加载技能指南

    Args:
        loader: 已配置好技能目录的 SkillsLoader 实例
    """

    def __init__(self, loader: SkillsLoader) -> None:
        self.loader = loader

    def __repr__(self) -> str:
        return f"<Tool name={self.name}, label={self.label}, skills_dir={self.loader.skills_dir!r}>"

    # ------------------------------------------------------------------ 工具契约

    @property
    def name(self) -> str:
        return "load_skill"

    @property
    def description(self) -> str:
        return (
            "加载指定技能的完整使用指南. 当系统提示的可用技能列表中有一项与当前任务相关时, "
            "调用本工具并传入技能名, 获取该技能的详细步骤与注意事项."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "技能名, 取自系统提示的技能列表, 例如 pdf",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        }

    # ------------------------------------------------------------------ 执行

    async def execute(self, **kwargs: Any) -> str:
        """
        加载技能正文(已去掉 frontmatter)

        :param kwargs: 需包含 name(str)
        :return: 技能正文; 技能名非法或不存在时返回可读提示(附可用技能名, 便于模型自纠)
        """
        name = str(kwargs.get("name") or "").strip()
        if not name:
            return "[错误] 技能名不能为空"

        content = self.loader.load_skill(name)
        if content is None:
            available = ", ".join(record["name"] for record in self.loader.list_skills()) or "无"
            logger.warning("技能不存在或不可读: %r", name)
            return f"[错误] 未找到技能: {name}. 可用技能: {available}"

        if len(content) > MAX_SKILL_CHARS:
            content = content[:MAX_SKILL_CHARS] + TRUNCATE_NOTICE
        return content
