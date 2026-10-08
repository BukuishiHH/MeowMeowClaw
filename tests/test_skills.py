"""meowmeowclaw/skills/loader.py 的单元测试.

测试策略:
- 全部使用 tmp_path 里的真实文件树, 覆盖面最广(目录缺失/非目录/无 SKILL.md/坏 YAML/不可读);
- 不可读、越界等分支用 monkeypatch 制造, 不依赖文件权限(以 root 运行时 chmod 无效);
- 规格写死的文案(摘要引导语 / 摘要行格式)用**字面量**钉住, 避免随常量一起漂移。

运行: pytest tests/test_skills.py -v
"""

import logging
import os
import pathlib
from typing import Any, Optional

import pytest

from meowmeowclaw.paths import PROJECT_ROOT
from meowmeowclaw.skills import (
    BUILTIN_SKILLS_DIR,
    DEFAULT_DESCRIPTION,
    SKILL_FILE_NAME,
    SKILLS_SUMMARY_HEADER,
    SkillsLoader,
)

# 规格要求的摘要引导语(按项目"统一英文标点"约定落地)
EXPECTED_HEADER = (
    "你有以下技能可用. 当某项技能与当前任务相关时, "
    "请调用 load_skill 工具并传入技能名, 获取该技能的详细指南.\n\n可用技能:\n"
)


# --------------------------------------------------------------------- 测试工具


def write_skill(
    root: pathlib.Path,
    dir_name: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    body: str = "正文内容",
    raw: Optional[str] = None,
) -> pathlib.Path:
    """在 root/dir_name/SKILL.md 写一个技能文件."""
    skill_dir = root / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / SKILL_FILE_NAME
    if raw is not None:
        skill_file.write_text(raw, encoding="utf-8")
        return skill_file

    front = []
    if name is not None:
        front.append(f"name: {name}")
    if description is not None:
        front.append(f"description: {description}")
    text = "---\n" + "\n".join(front) + "\n---\n" + body
    skill_file.write_text(text, encoding="utf-8")
    return skill_file


@pytest.fixture
def skills_root(tmp_path) -> pathlib.Path:
    return tmp_path / "skills"


# ------------------------------------------------------------ frontmatter 解析


class TestParseFrontmatter:
    def test_valid_frontmatter(self):
        content = "---\nname: pdf\ndescription: 处理 PDF\n---\n# 正文\n内容\n"

        metadata, body = SkillsLoader._parse_frontmatter(content)

        assert metadata == {"name": "pdf", "description": "处理 PDF"}
        assert body == "# 正文\n内容\n"

    def test_spec_header_literal(self):
        """规格写死的引导语必须钉字面量."""
        assert SKILLS_SUMMARY_HEADER == EXPECTED_HEADER

    def test_no_frontmatter_returns_original(self):
        content = "# 直接就是正文\n没有 frontmatter\n"

        metadata, body = SkillsLoader._parse_frontmatter(content)

        assert metadata == {}
        assert body == content  # 原文返回, 不做裁剪

    def test_empty_frontmatter(self):
        metadata, body = SkillsLoader._parse_frontmatter("---\n---\n正文\n")

        assert metadata == {}
        assert body == "正文\n"

    def test_missing_closing_fence_is_treated_as_no_frontmatter(self, caplog):
        content = "---\nname: pdf\n正文没有结束符\n"

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.skills.loader"):
            metadata, body = SkillsLoader._parse_frontmatter(content)

        assert (metadata, body) == ({}, content)
        assert any("结束符" in r.message for r in caplog.records)

    def test_malformed_yaml_is_treated_as_no_frontmatter(self, caplog):
        content = "---\nname: [未闭合\n---\n正文\n"

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.skills.loader"):
            metadata, body = SkillsLoader._parse_frontmatter(content)

        assert (metadata, body) == ({}, content)
        assert any("YAML" in r.message for r in caplog.records)

    @pytest.mark.parametrize("yaml_text", ["- a\n- b", "just a string", "123"])
    def test_non_dict_yaml_becomes_empty_metadata(self, yaml_text):
        metadata, body = SkillsLoader._parse_frontmatter(f"---\n{yaml_text}\n---\n正文\n")

        assert metadata == {}
        assert body == "正文\n"

    def test_crlf_and_bom_are_normalized(self):
        content = "\ufeff---\r\nname: pdf\r\ndescription: 处理 PDF\r\n---\r\n正文\r\n"

        metadata, body = SkillsLoader._parse_frontmatter(content)

        assert metadata == {"name": "pdf", "description": "处理 PDF"}
        assert body == "正文\n"  # 换行被统一为 \n

    def test_indented_fence_inside_yaml_does_not_end_frontmatter(self):
        content = (
            "---\n"
            "name: pdf\n"
            "description: |\n"
            "  第一行\n"
            "  ---\n"          # 缩进的 --- 属于 YAML 内容, 不是结束符
            "  第二行\n"
            "---\n"
            "正文\n"
        )

        metadata, body = SkillsLoader._parse_frontmatter(content)

        assert metadata["name"] == "pdf"
        assert "第二行" in metadata["description"]
        assert body == "正文\n"

    def test_body_leading_newlines_are_trimmed(self):
        metadata, body = SkillsLoader._parse_frontmatter("---\nname: x\n---\n\n\n正文\n")

        assert metadata == {"name": "x"}
        assert body == "正文\n"

    def test_extra_metadata_keys_are_kept(self):
        metadata, _ = SkillsLoader._parse_frontmatter(
            "---\nname: x\nversion: 2\nallowed-tools: [read_file]\n---\n正文\n"
        )

        assert metadata == {"name": "x", "version": 2, "allowed-tools": ["read_file"]}

    @pytest.mark.parametrize("content", ["", "---\n", "  ---\nname: x\n---\n正文"])
    def test_content_without_opening_fence(self, content):
        metadata, body = SkillsLoader._parse_frontmatter(content)

        assert metadata == {}
        assert body == content


# ------------------------------------------------------------------- 摘要生成


class TestBuildSkillsSummary:
    def test_missing_dir_returns_empty(self, tmp_path):
        assert SkillsLoader(str(tmp_path / "nope")).build_skills_summary() == ""

    def test_empty_dir_returns_empty(self, skills_root):
        skills_root.mkdir()

        assert SkillsLoader(str(skills_root)).build_skills_summary() == ""

    def test_dir_without_skill_md_returns_empty(self, skills_root):
        (skills_root / "empty-skill").mkdir(parents=True)  # 有子目录但没有 SKILL.md
        (skills_root / "loose.txt").write_text("x", encoding="utf-8")  # 散落文件也算不上技能

        assert SkillsLoader(str(skills_root)).build_skills_summary() == ""

    def test_single_skill_exact_output(self, skills_root):
        write_skill(skills_root, "pdf", name="pdf", description="处理 PDF 文件", body="指南正文")

        result = SkillsLoader(str(skills_root)).build_skills_summary()

        assert result == EXPECTED_HEADER + "- pdf (pdf/SKILL.md): 处理 PDF 文件\n"

    def test_multiple_skills_are_sorted_by_dir_name(self, skills_root):
        write_skill(skills_root, "zebra", name="zebra", description="最后一个")
        write_skill(skills_root, "alpha", name="alpha", description="第一个")
        write_skill(skills_root, "mid", name="mid", description="中间")

        result = SkillsLoader(str(skills_root)).build_skills_summary()

        lines = result.splitlines()[3:]  # 跳过引导语两行 + "可用技能:" 一行
        assert lines == [
            "- alpha (alpha/SKILL.md): 第一个",
            "- mid (mid/SKILL.md): 中间",
            "- zebra (zebra/SKILL.md): 最后一个",
        ]

    def test_fallbacks_when_frontmatter_missing(self, skills_root):
        write_skill(skills_root, "excel", raw="# 只有正文\n")

        result = SkillsLoader(str(skills_root)).build_skills_summary()

        assert result == EXPECTED_HEADER + f"- excel (excel/SKILL.md): {DEFAULT_DESCRIPTION}\n"

    def test_name_falls_back_to_dir_name(self, skills_root):
        write_skill(skills_root, "real-dir", description="有描述没名字")

        result = SkillsLoader(str(skills_root)).build_skills_summary()

        assert "- real-dir (real-dir/SKILL.md): 有描述没名字" in result

    def test_frontmatter_name_wins_over_dir_name(self, skills_root):
        write_skill(skills_root, "dir-name", name="漂亮名字", description="描述")

        assert "- 漂亮名字 (dir-name/SKILL.md): 描述" in SkillsLoader(
            str(skills_root)
        ).build_skills_summary()

    def test_broken_yaml_skill_still_listed_with_fallbacks(self, skills_root):
        write_skill(skills_root, "broken", raw="---\nname: [未闭合\n---\n正文\n")

        assert "- broken (broken/SKILL.md): " + DEFAULT_DESCRIPTION in SkillsLoader(
            str(skills_root)
        ).build_skills_summary()

    def test_unreadable_skill_is_skipped_others_remain(self, skills_root, monkeypatch, caplog):
        write_skill(skills_root, "good", name="good", description="正常技能")
        blocked = write_skill(skills_root, "blocked", name="blocked", description="读不了")
        real_read_text = pathlib.Path.read_text

        def fake_read_text(self, *args: Any, **kwargs: Any) -> str:
            if self == blocked:
                raise PermissionError(13, "Permission denied")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "read_text", fake_read_text)

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.skills.loader"):
            result = SkillsLoader(str(skills_root)).build_skills_summary()

        assert "- good (good/SKILL.md): 正常技能" in result
        assert "blocked" not in result
        assert any("跳过" in r.message for r in caplog.records)

    def test_subdirectories_deeper_than_one_level_are_ignored(self, skills_root):
        nested = skills_root / "group" / "inner"
        nested.mkdir(parents=True)
        (nested / SKILL_FILE_NAME).write_text("---\nname: inner\n---\n正文", encoding="utf-8")

        assert SkillsLoader(str(skills_root)).build_skills_summary() == ""


# ------------------------------------------------------------------- 按名加载


class TestLoadSkill:
    def test_returns_body_without_frontmatter(self, skills_root):
        write_skill(skills_root, "pdf", name="pdf", description="描述", body="# 指南\n第一步\n")

        assert SkillsLoader(str(skills_root)).load_skill("pdf") == "# 指南\n第一步\n"

    def test_returns_original_when_no_frontmatter(self, skills_root):
        write_skill(skills_root, "plain", raw="就是正文\n")

        assert SkillsLoader(str(skills_root)).load_skill("plain") == "就是正文\n"

    @pytest.mark.parametrize("name", ["nope", "", ".", "..", "../etc", "../../etc/passwd", "/etc"])
    def test_unknown_or_escaping_name_returns_none(self, skills_root, name):
        write_skill(skills_root, "pdf", name="pdf", description="描述")

        assert SkillsLoader(str(skills_root)).load_skill(name) is None

    def test_path_traversal_is_blocked_even_when_file_exists(self, tmp_path, caplog):
        """skills_dir 外真实存在 SKILL.md, 也必须被拦住(不能只靠"文件不存在")."""
        skills_root = tmp_path / "skills"
        write_skill(skills_root, "pdf", name="pdf", description="描述")
        outside = tmp_path / "outside"
        write_skill(outside, "secret", name="secret", description="机密")

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.skills.loader"):
            result = SkillsLoader(str(skills_root)).load_skill("../outside/secret")

        assert result is None
        assert any("越界" in r.message for r in caplog.records)

    def test_sibling_dir_sharing_prefix_is_blocked(self, tmp_path):
        """回归防线: 越界判定若用 str.startswith, 同前缀兄弟目录(/skills-evil)会被放行."""
        skills_root = tmp_path / "skills"
        write_skill(skills_root, "pdf", name="pdf", description="描述")
        write_skill(tmp_path / "skills-evil", "secret", name="secret", description="机密")

        assert SkillsLoader(str(skills_root)).load_skill("../skills-evil/secret") is None

    def test_name_pointing_to_file_returns_none(self, skills_root):
        skills_root.mkdir(parents=True)
        (skills_root / "not-a-dir").write_text("x", encoding="utf-8")

        assert SkillsLoader(str(skills_root)).load_skill("not-a-dir") is None

    def test_unreadable_skill_returns_none(self, skills_root, monkeypatch, caplog):
        blocked = write_skill(skills_root, "blocked", name="blocked", description="读不了")

        def boom(self, *args: Any, **kwargs: Any) -> str:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(pathlib.Path, "read_text", boom)

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.skills.loader"):
            assert SkillsLoader(str(skills_root)).load_skill("blocked") is None

        assert any("加载技能失败" in r.message for r in caplog.records)
        assert blocked.exists()

    def test_missing_skills_dir_returns_none(self, tmp_path):
        assert SkillsLoader(str(tmp_path / "nope")).load_skill("pdf") is None


# ------------------------------------------------------------------- 技能清单


class TestListSkills:
    def test_empty_when_no_skills(self, tmp_path):
        assert SkillsLoader(str(tmp_path / "nope")).list_skills() == []

    def test_returns_name_description_and_path(self, skills_root):
        skill_file = write_skill(skills_root, "pdf", name="pdf", description="处理 PDF")

        assert SkillsLoader(str(skills_root)).list_skills() == [
            {"name": "pdf", "description": "处理 PDF", "path": str(skill_file)}
        ]

    def test_path_is_absolute_and_exists(self, skills_root):
        write_skill(skills_root, "pdf", name="pdf", description="处理 PDF")

        record = SkillsLoader(str(skills_root)).list_skills()[0]

        assert record["path"] == os.path.join(str(skills_root), "pdf", SKILL_FILE_NAME)
        assert os.path.isabs(record["path"])
        assert pathlib.Path(record["path"]).is_file()

    def test_consistent_with_summary(self, skills_root):
        write_skill(skills_root, "b-skill", name="b", description="B")
        write_skill(skills_root, "a-skill", name="a", description="A")
        loader = SkillsLoader(str(skills_root))

        names = [record["name"] for record in loader.list_skills()]
        summary = loader.build_skills_summary()

        assert names == ["a", "b"]
        for name in names:
            assert f"- {name} (" in summary

    def test_repr_includes_dir_and_count(self, skills_root):
        write_skill(skills_root, "pdf", name="pdf", description="处理 PDF")
        loader = SkillsLoader(str(skills_root))

        text = repr(loader)

        assert str(skills_root) in text
        assert "skills=1" in text


# --------------------------------------------------------------- 目录解析规则


class TestSkillsDirResolution:
    def test_default_is_builtin_skills(self):
        assert SkillsLoader().skills_dir == BUILTIN_SKILLS_DIR

    def test_relative_path_resolves_against_project_root_not_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        assert SkillsLoader("my-skills").skills_dir == str(PROJECT_ROOT / "my-skills")

    def test_absolute_path_is_kept(self, tmp_path):
        assert SkillsLoader(str(tmp_path)).skills_dir == str(tmp_path)

    def test_tilde_is_expanded(self):
        assert str(pathlib.Path("~").expanduser()) in SkillsLoader("~/my-skills").skills_dir

    def test_dot_segments_are_normalized(self):
        assert SkillsLoader("a/../b").skills_dir == str(PROJECT_ROOT / "b")


# ------------------------------------------------- 仓库自带技能内容(防误删/防写坏)


class TestShippedSkills:
    """包内 skills/builtin 自带"工具用法"技能, 这里保证它们始终可被发现且 frontmatter 合法."""

    TOOL_SKILLS = ("exec", "list_dir", "read_file", "web_fetch", "web_search", "write_file")

    def test_tool_skills_exist_and_are_parsable(self):
        loader = SkillsLoader()  # 默认即内置技能目录

        found = {record["name"]: record for record in loader.list_skills()}

        missing = [name for name in self.TOOL_SKILLS if name not in found]
        assert not missing, f"skills/builtin 缺少工具技能: {missing}"
        for name in self.TOOL_SKILLS:
            assert found[name]["description"] != DEFAULT_DESCRIPTION, f"{name} 缺少 description"
            body = loader.load_skill(name)
            assert body and body.startswith("# "), f"{name} 正文异常"

    def test_summary_lists_all_tool_skills(self):
        summary = SkillsLoader().build_skills_summary()

        for name in self.TOOL_SKILLS:
            assert f"- {name} ({name}/SKILL.md): " in summary
