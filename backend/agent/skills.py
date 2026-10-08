"""技能加载器: 扫描技能目录下的 SKILL.md, 生成技能摘要 / 按名加载技能正文.

SKILL.md 约定(参考 Claude Skills / OpenClaw 的 frontmatter 写法)::

    ---
    name: pdf
    description: 处理 PDF 文件, 提取文本与表格
    ---
    # 使用说明
    这里是给模型看的详细指南...

用法::

    loader = SkillsLoader()                 # 默认 <项目根>/skills
    summary = loader.build_skills_summary() # 拼进 System Prompt
    guide = loader.load_skill("pdf")        # 按需加载正文

注意: 摘要里给的是**相对 skills_dir 的路径**(如 `pdf/SKILL.md`)。若希望模型能用
`read_file` 工具直接按该路径读到文件, `skills_dir` 需要位于工作区(workspace)之内。
"""

import logging
import os
import re
from pathlib import Path
from typing import Any, Iterator, Optional

import yaml

from backend.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

SKILL_FILE_NAME = "SKILL.md"
DEFAULT_SKILLS_DIR = "skills"
DEFAULT_DESCRIPTION = "无描述"

# frontmatter: 首行必须是独占一行的 ---, 结束符也是独占一行的 ---
_OPENING_FENCE_RE = re.compile(r"^---[ \t]*\n")
_CLOSING_FENCE_RE = re.compile(r"^---[ \t]*$", re.MULTILINE)

# 拼进 System Prompt 的引导语
SKILLS_SUMMARY_HEADER = (
    "你有以下技能可用. 当你需要使用某项技能时, "
    "请先用 read_file 工具读取对应的 SKILL.md 文件获取详细指南.\n\n可用技能:\n"
)


class SkillsLoader:
    """
    技能目录扫描器

    Args:
        skills_dir: 技能目录; 相对路径按**项目根**解析(不随当前工作目录漂移),
                    绝对路径原样使用。默认 ``<项目根>/skills``。

    容错约定:
        - 目录不存在 / 没有任何技能 -> 摘要返回空字符串;
        - 单个 SKILL.md 读不了或 YAML 坏了 -> 跳过该技能并打 warning, 不影响其余技能。
    """

    def __init__(self, skills_dir: str = DEFAULT_SKILLS_DIR) -> None:
        self.skills_dir = self._resolve_skills_dir(skills_dir)

    def __repr__(self) -> str:
        return f"<SkillsLoader skills_dir={self.skills_dir!r} skills={len(self.list_skills())}>"

    # ------------------------------------------------------------------ 路径

    @staticmethod
    def _resolve_skills_dir(skills_dir: str) -> str:
        """相对路径按项目根解析, 绝对路径原样(与 config.resolve_workspace 同一约定)."""
        path = Path(skills_dir).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return os.path.normpath(path)

    def _is_inside_skills_dir(self, path: str) -> bool:
        """防越界: 目标必须落在 skills_dir 内(比 startswith 更严谨, 不会被同前缀兄弟目录绕过)."""
        try:
            return os.path.commonpath([os.path.abspath(path), self.skills_dir]) == self.skills_dir
        except ValueError:  # Windows 跨盘符
            return False

    # ------------------------------------------------------------------ 解析

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
        """
        拆出 frontmatter 与正文

        :param content: SKILL.md 的完整内容
        :return: (metadata, body); 没有 frontmatter / YAML 非法时返回 ({{}}, 原文)
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
            logger.warning("frontmatter YAML 解析失败(%r), 按无 frontmatter 处理", exc)
            return {}, text

        if not isinstance(metadata, dict):  # 空 frontmatter / 写成了列表等
            metadata = {}
        return metadata, body

    # ------------------------------------------------------------------ 扫描

    def _iter_skill_files(self) -> Iterator[tuple[str, str]]:
        """产出 (子目录名, SKILL.md 绝对路径); 按目录名排序, 只认一层子目录."""
        if not os.path.isdir(self.skills_dir):
            return
        for entry in sorted(os.listdir(self.skills_dir)):
            skill_dir = os.path.join(self.skills_dir, entry)
            if not os.path.isdir(skill_dir):
                continue
            skill_file = os.path.join(skill_dir, SKILL_FILE_NAME)
            if not os.path.isfile(skill_file):
                continue
            yield entry, skill_file

    def _scan(self) -> list[dict[str, Any]]:
        """扫描全部技能; 单个技能出错只跳过它自己."""
        records: list[dict[str, Any]] = []
        for dir_name, skill_file in self._iter_skill_files():
            try:
                content = Path(skill_file).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("读取技能文件失败, 已跳过: %s (%r)", skill_file, exc)
                continue

            metadata, body = self._parse_frontmatter(content)
            records.append(
                {
                    "dir": dir_name,  # 摘要里用的相对路径
                    "name": str(metadata.get("name") or "").strip() or dir_name,
                    "description": str(metadata.get("description") or "").strip()
                    or DEFAULT_DESCRIPTION,
                    "path": skill_file,  # SKILL.md 的绝对路径
                    "body": body,
                }
            )
        return records

    # ------------------------------------------------------------------ 对外

    def build_skills_summary(self) -> str:
        """
        生成技能摘要(用于拼进 System Prompt)

        :return: 引导语 + 每行 ``- name (子目录/SKILL.md): description``;
                 目录不存在或没有任何技能时返回空字符串
        """
        if not os.path.isdir(self.skills_dir):
            return ""

        records = self._scan()
        if not records:
            return ""

        lines = [
            f"- {record['name']} ({record['dir']}/{SKILL_FILE_NAME}): {record['description']}"
            for record in records
        ]
        return SKILLS_SUMMARY_HEADER + "\n".join(lines) + "\n"

    def load_skill(self, name: str) -> Optional[str]:
        """
        按技能名加载正文(已去掉 frontmatter)

        :param name: 技能子目录名
        :return: 正文; 找不到或读取失败返回 None
        """
        skill_dir = os.path.join(self.skills_dir, name)
        if not self._is_inside_skills_dir(skill_dir):
            logger.warning("拒绝越界访问技能: %r", name)
            return None

        skill_file = os.path.join(skill_dir, SKILL_FILE_NAME)
        try:
            content = Path(skill_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("加载技能失败: %s (%r)", skill_file, exc)
            return None

        _, body = self._parse_frontmatter(content)
        return body

    def list_skills(self) -> list[dict[str, Any]]:
        """
        列出已发现的技能(调试/管理用)

        :return: ``[{{"name": ..., "description": ..., "path": SKILL.md 绝对路径}}, ...]``
        """
        return [
            {
                "name": record["name"],
                "description": record["description"],
                "path": record["path"],
            }
            for record in self._scan()
        ]
