"""技能子系统: 内置 SKILL.md 的扫描、摘要与按名加载.

内置技能以纯文本资源形式放在 ``builtin/<技能名>/SKILL.md``, 随代码入库与分发;
``SkillsLoader`` 默认从该目录读取, 不再依赖 workspace 或当前工作目录。
"""

from .loader import (
    BUILTIN_SKILLS_DIR,
    DEFAULT_DESCRIPTION,
    SKILL_FILE_NAME,
    SKILLS_SUMMARY_HEADER,
    SkillsLoader,
)

__all__ = [
    "BUILTIN_SKILLS_DIR",
    "DEFAULT_DESCRIPTION",
    "SKILL_FILE_NAME",
    "SKILLS_SUMMARY_HEADER",
    "SkillsLoader",
]
