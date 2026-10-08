"""把技能目录暴露成 ``load_skill`` 工具, 供模型按需取回 SKILL.md 正文.

为什么要有这个工具: 技能是随代码分发的内置资源, read_file 只能读工作区内的文件;
即使用户拷贝一份到工作区, read_file 也会把 frontmatter 一并读回来。用专门的工具直接
返回"去掉 frontmatter 的正文"更干净, 也更符合"技能按需加载"的语义。

本模块是技能子系统对 Agent 的唯一出口: 只依赖 ``BaseTool`` 契约与 ``SkillCatalog``,
不反向依赖 agent 的控制流。
"""

import logging
from typing import Any

from meowmeowclaw.tools.base import BaseTool
from meowmeowclaw.skills.loader import SkillCatalog

logger = logging.getLogger(__name__)

MAX_SKILL_CHARS = 16000  # 与 read_file 保持一致
TRUNCATE_NOTICE = "\n...(内容过长, 已截断)"


class LoadSkillTool(BaseTool):
    """
    按名加载技能指南

    Args:
        catalog: 已完成扫描的技能索引(``SkillCatalog``)
    """

    def __init__(self, catalog: SkillCatalog) -> None:
        self.catalog = catalog

    def __repr__(self) -> str:
        return f"<Tool name={self.name}, label={self.label}, skills={len(self.catalog)}>"

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
                    "description": "技能名, 取自系统提示的技能列表, 例如 exec",
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

        skill = self.catalog.get(name)
        if skill is None:
            available = ", ".join(self.catalog.names()) or "无"
            logger.warning("技能不存在: %r", name)
            return f"[错误] 未找到技能: {name}. 可用技能: {available}"

        if len(skill.body) > MAX_SKILL_CHARS:
            return skill.body[:MAX_SKILL_CHARS] + TRUNCATE_NOTICE
        return skill.body
