"""技能子系统的数据模型与错误类型."""

from dataclasses import dataclass


class SkillConfigError(Exception):
    """技能资源配置错误(如多个技能重名).

    属于仓库/发布配置问题, 应在启动装配阶段尽早暴露, 而不是等在模型调用时才发现。
    """


@dataclass(frozen=True)
class Skill:
    """一个已解析完成的技能.

    Attributes:
        name: 技能名, 也是 ``load_skill`` 的查询键(frontmatter 优先, 缺省用目录名)
        description: 一句话描述, 渲染进 System Prompt
        body: 去掉 frontmatter 的正文, 按需回给模型
        dir_name: 技能所在子目录名(摘要展示 / 排障用)
        source: SKILL.md 的资源位置字符串(排障用)
    """

    name: str
    description: str
    body: str
    dir_name: str
    source: str

    def summary_line(self) -> str:
        """渲染成 System Prompt 里的一行(不含前导换行)."""
        return f"- {self.name} ({self.dir_name}/SKILL.md): {self.description}"
