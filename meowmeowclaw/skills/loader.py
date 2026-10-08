"""技能目录扫描器: 启动时扫描一次, 建立 ``{name: Skill}`` 索引.

内置技能放在 ``<包>/skills/builtin/<目录>/SKILL.md``, 通过 ``importlib.resources``
读取: 源码运行、editable 安装与 wheel/zipimport 场景行为一致, 不依赖 workspace,
也不依赖进程当前工作目录。

用法::

    catalog = SkillCatalog()
    summary = catalog.summary()        # 拼进 System Prompt
    skill = catalog.get("exec")        # 按名精确查索引
    body = catalog.load("exec")        # 取去掉 frontmatter 的正文

``load_skill`` 的越界防护不再依赖路径拼接校验: 先按名字从索引里精确查找,
查不到直接返回 None, 根本不会接触文件系统。
"""

import logging
import os
import re
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Iterator, Optional, Union

import yaml

from meowmeowclaw.paths import PROJECT_ROOT
from meowmeowclaw.skills.models import Skill, SkillConfigError

logger = logging.getLogger(__name__)

SKILL_FILE_NAME = "SKILL.md"
# 内置技能资源: 包名 + 目录名(用 importlib.resources 定位, 兼容 wheel/zipimport)
BUILTIN_PACKAGE = "meowmeowclaw.skills"
BUILTIN_DIR_NAME = "builtin"
DEFAULT_DESCRIPTION = "无描述"

# frontmatter: 首行必须是独占一行的 ---, 结束符也是独占一行的 ---
_OPENING_FENCE_RE = re.compile(r"^---[ \t]*\n")
_CLOSING_FENCE_RE = re.compile(r"^---[ \t]*$", re.MULTILINE)

class _InvalidFrontmatterError(Exception):
    """SKILL.md 写了 frontmatter 但 YAML 语法错误; 该技能应被跳过而不是原样进上下文."""


# 拼进 System Prompt 的引导语
SKILLS_SUMMARY_HEADER = (
    "你有以下技能可用. 当某项技能与当前任务相关时, "
    "请调用 load_skill 工具并传入技能名, 获取该技能的详细指南.\n\n可用技能:\n"
)


def default_builtin_root() -> Traversable:
    """包内内置技能根目录(``<包>/skills/builtin``).

    返回 ``Traversable`` 而非真实路径: wheel/zipimport 下同样可用。
    """
    return resources.files(BUILTIN_PACKAGE) / BUILTIN_DIR_NAME


class SkillCatalog:
    """技能索引: 扫描一次后, 所有查询都走内存索引.

    Args:
        root: 技能根目录(只扫描一层子目录); ``None`` 时用内置资源目录。
              传入 ``str``/``Path`` 时按目录处理: 相对路径基于项目根解析,
              便于测试与本地自定义目录; 也可直接传入 ``Traversable``。

    容错约定:
        - 目录不存在 / 没有任何技能 -> 空索引, ``summary()`` 返回空字符串;
        - 单个 SKILL.md 读不了 / YAML 坏了 -> 跳过该技能并打 warning;
        - 多个技能重名 -> 抛 ``SkillConfigError``(内置资源重名属于仓库错误)。
    """

    def __init__(self, root: Optional[Union[str, os.PathLike, Traversable]] = None) -> None:
        self.root: Traversable = self._resolve_root(root)
        self._skills: dict[str, Skill] = {}
        self._scan()

    def __repr__(self) -> str:
        return f"<SkillCatalog root={str(self.root)!r} skills={len(self._skills)}>"

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._skills

    # ------------------------------------------------------------------ 路径

    @staticmethod
    def _resolve_root(
        root: Optional[Union[str, os.PathLike, Traversable]]
    ) -> Traversable:
        """None -> 内置资源目录; str/Path -> 归一化路径; 其余视为 Traversable 原样使用."""
        if root is None:
            return default_builtin_root()
        if isinstance(root, (str, os.PathLike)):
            path = Path(root).expanduser()
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            return Path(os.path.normpath(str(path)))
        return root

    # ------------------------------------------------------------------ 解析

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
        """
        拆出 frontmatter 与正文

        :param content: SKILL.md 的完整内容
        :return: (metadata, body); 没有 frontmatter / 缺少结束符时返回 ({}, 原文)
        :raises _InvalidFrontmatterError: 写了 frontmatter 但 YAML 语法错误
        """
        # 统一换行与 BOM: Windows 上编辑的 SKILL.md 也要能解析
        text = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")

        opening = _OPENING_FENCE_RE.match(text)
        if opening is None:
            return {}, text

        rest = text[opening.end():]
        closing = _CLOSING_FENCE_RE.search(rest)
        if closing is None:
            logger.warning("SKILL.md 的 frontmatter 缺少结束符 ---, 按无 frontmatter 处理")
            return {}, text

        raw_yaml = rest[: closing.start()]
        body = rest[closing.end():].lstrip("\n")

        try:
            metadata = yaml.safe_load(raw_yaml)
        except yaml.YAMLError as exc:
            raise _InvalidFrontmatterError(f"frontmatter YAML 解析失败: {exc}") from exc

        if not isinstance(metadata, dict):  # 空 frontmatter / 写成了列表等
            metadata = {}
        return metadata, body

    # ------------------------------------------------------------------ 扫描

    def _iter_skill_dirs(self) -> Iterator[Traversable]:
        """产出第一层子目录, 按名字排序, 保证索引顺序稳定."""
        try:
            entries = sorted(self.root.iterdir(), key=lambda entry: entry.name)
        except FileNotFoundError:
            return
        except (NotADirectoryError, OSError) as exc:
            logger.warning("读取技能目录失败, 本次忽略: %s (%r)", self.root, exc)
            return

        for entry in entries:
            try:
                if entry.is_dir():
                    yield entry
            except OSError:  # 单个条目异常不影响其余技能
                logger.warning("检查技能子目录失败, 已跳过: %s", entry)
                continue

    def _scan(self) -> None:
        """扫描并建立索引; 单个技能出错只跳过它自己, 重名则整体报错."""
        skills: dict[str, Skill] = {}
        duplicates: dict[str, list[str]] = {}

        for skill_dir in self._iter_skill_dirs():
            dir_name = skill_dir.name
            skill_file = skill_dir / SKILL_FILE_NAME
            try:
                if not skill_file.is_file():
                    continue
                content = skill_file.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("读取技能文件失败, 已跳过: %s (%r)", skill_file, exc)
                continue

            try:
                metadata, body = self._parse_frontmatter(content)
            except _InvalidFrontmatterError as exc:
                logger.warning("SKILL.md frontmatter 解析失败, 已跳过: %s (%r)", skill_file, exc)
                continue

            name = str(metadata.get("name") or "").strip() or dir_name
            if name != dir_name:
                logger.warning(
                    "技能目录 %r 的 frontmatter name=%r 与目录名不一致, 以 frontmatter 为准",
                    dir_name,
                    name,
                )
            skill = Skill(
                name=name,
                description=str(metadata.get("description") or "").strip()
                or DEFAULT_DESCRIPTION,
                body=body,
                dir_name=dir_name,
                source=str(skill_file),
            )

            if name in skills:
                duplicates.setdefault(name, [skills[name].dir_name]).append(dir_name)
                continue
            skills[name] = skill

        if duplicates:
            detail = "; ".join(f"{name} <- {dirs}" for name, dirs in duplicates.items())
            raise SkillConfigError(f"技能名重复: {detail}")

        self._skills = skills

    # ------------------------------------------------------------------ 对外

    def names(self) -> list[str]:
        """已索引的技能名(按字典序)."""
        return sorted(self._skills)

    def skills(self) -> list[Skill]:
        """已索引的技能(按目录扫描顺序)."""
        return list(self._skills.values())

    def get(self, name: str) -> Optional[Skill]:
        """按名精确查索引; 未知名返回 None(不触碰文件系统)."""
        return self._skills.get(str(name).strip())

    def load(self, name: str) -> Optional[str]:
        """按名取技能正文(已去掉 frontmatter); 未知名返回 None."""
        skill = self.get(name)
        return None if skill is None else skill.body

    def summary(self) -> str:
        """生成技能摘要(用于拼进 System Prompt); 无技能时返回空字符串."""
        if not self._skills:
            return ""
        lines = [skill.summary_line() for skill in self._skills.values()]
        return SKILLS_SUMMARY_HEADER + "\n".join(lines) + "\n"
