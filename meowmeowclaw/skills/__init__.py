"""技能子系统: 内置 SKILL.md 的发现、索引、摘要与按需加载.

内置技能以纯文本资源形式放在 ``builtin/<技能名>/SKILL.md``, 随代码入库与分发;
``SkillCatalog`` 通过 ``importlib.resources`` 读取, 不依赖 workspace 与当前工作目录;
``LoadSkillTool`` 是对 Agent 暴露的唯一出口。
"""

from .loader import (
    DEFAULT_DESCRIPTION,
    SKILL_FILE_NAME,
    SKILLS_SUMMARY_HEADER,
    SkillCatalog,
    default_builtin_root,
)
from .models import Skill, SkillConfigError
from .tool import LoadSkillTool

__all__ = [
    "DEFAULT_DESCRIPTION",
    "SKILL_FILE_NAME",
    "SKILLS_SUMMARY_HEADER",
    "LoadSkillTool",
    "Skill",
    "SkillCatalog",
    "SkillConfigError",
    "default_builtin_root",
]
