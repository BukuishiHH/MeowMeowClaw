"""meowmeowclaw/skills/loader.py(SkillCatalog) 的单元测试.

测试策略:
- 全部使用 tmp_path 里的真实文件树, 覆盖扫描/索引/摘要/异常跳过等分支;
- 不可读等分支用 monkeypatch 制造, 不依赖文件权限(以 root 运行时 chmod 无效);
- 规格写死的文案(摘要引导语 / 摘要行格式)用**字面量**钉住, 避免随常量一起漂移;
- 额外保证包内 builtin 资源始终可被 importlib.resources 发现.

运行: pytest tests/skills/test_catalog.py -v
"""

import logging
from importlib import resources
from pathlib import Path
from typing import Optional

import pytest

from meowmeowclaw.paths import PROJECT_ROOT
from meowmeowclaw.skills import (
    DEFAULT_DESCRIPTION,
    SKILL_FILE_NAME,
    SKILLS_SUMMARY_HEADER,
    Skill,
    SkillCatalog,
    SkillConfigError,
    default_builtin_root,
)

# 规格要求的摘要引导语(按项目"统一英文标点"约定落地)
EXPECTED_HEADER = (
    "你有以下技能可用. 当某项技能与当前任务相关时, "
    "请调用 load_skill 工具并传入技能名, 获取该技能的详细指南.\n\n可用技能:\n"
)


def write_skill(
    root: Path,
    dir_name: str,
    content: Optional[str] = None,
    *,
    name: Optional[str] = None,
    description: Optional[str] = "描述",
    body: str = "# 指南\n正文\n",
) -> Path:
    """在 root/dir_name/ 下写一个 SKILL.md; content 传了就原样写入."""
    skill_dir = root / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    if content is None:
        content = (
            f"---\nname: {name or dir_name}\ndescription: {description}\n---\n{body}"
        )
    (skill_dir / SKILL_FILE_NAME).write_text(content, encoding="utf-8")
    return skill_dir


# ------------------------------------------------------------- frontmatter 解析


class TestFrontmatterParsing:
    def test_parses_metadata_and_body(self):
        metadata, body = SkillCatalog._parse_frontmatter(
            "---\nname: pdf\ndescription: 处理 PDF\n---\n# 指南\n正文\n"
        )

        assert metadata == {"name": "pdf", "description": "处理 PDF"}
        assert body == "# 指南\n正文\n"

    def test_no_frontmatter_returns_original_text(self):
        text = "# 没有 frontmatter\n正文"

        metadata, body = SkillCatalog._parse_frontmatter(text)

        assert metadata == {}
        assert body == text

    def test_unterminated_frontmatter_returns_original_text(self):
        text = "---\nname: pdf\n# 少了结束符"

        metadata, body = SkillCatalog._parse_frontmatter(text)

        assert metadata == {}
        assert body == text

    def test_empty_frontmatter_gives_empty_metadata(self):
        metadata, body = SkillCatalog._parse_frontmatter("---\n---\n正文")

        assert metadata == {}
        assert body == "正文"

    def test_non_mapping_frontmatter_gives_empty_metadata(self):
        metadata, body = SkillCatalog._parse_frontmatter("---\n- a\n- b\n---\n正文")

        assert metadata == {}
        assert body == "正文"

    def test_bom_and_crlf_are_normalized(self):
        metadata, body = SkillCatalog._parse_frontmatter(
            "\ufeff---\r\nname: pdf\r\n---\r\n正文\r\n"
        )

        assert metadata == {"name": "pdf"}
        assert body == "正文\n"

    def test_opening_fence_must_be_first_line(self):
        text = "\n---\nname: pdf\n---\n正文"

        metadata, body = SkillCatalog._parse_frontmatter(text)

        assert metadata == {}
        assert body == text

    def test_body_leading_blank_lines_are_stripped(self):
        metadata, body = SkillCatalog._parse_frontmatter(
            "---\nname: pdf\n---\n\n\n# 指南\n"
        )

        assert metadata == {"name": "pdf"}
        assert body == "# 指南\n"

    def test_empty_body_is_allowed(self):
        metadata, body = SkillCatalog._parse_frontmatter("---\nname: pdf\n---\n")

        assert metadata == {"name": "pdf"}
        assert body == ""


# ------------------------------------------------------------------ 扫描与索引


class TestScanAndIndex:
    def test_discovers_all_skills_in_sorted_order(self, tmp_path):
        root = tmp_path / "skills"
        write_skill(root, "web_search", name="web_search", description="搜索")
        write_skill(root, "exec", name="exec", description="执行")
        write_skill(root, "read_file", name="read_file", description="读文件")

        catalog = SkillCatalog(root)

        assert len(catalog) == 3
        assert catalog.names() == ["exec", "read_file", "web_search"]
        assert [skill.name for skill in catalog.skills()] == ["exec", "read_file", "web_search"]

    def test_name_falls_back_to_dir_name(self, tmp_path):
        root = tmp_path / "skills"
        write_skill(root, "plain", content="---\ndescription: 无 name\n---\n正文")

        catalog = SkillCatalog(root)

        assert catalog.names() == ["plain"]
        assert catalog.get("plain").description == "无 name"

    def test_description_falls_back_to_default(self, tmp_path):
        root = tmp_path / "skills"
        write_skill(root, "plain", content="---\nname: plain\n---\n正文")

        assert SkillCatalog(root).get("plain").description == DEFAULT_DESCRIPTION

    def test_duplicate_names_raise_config_error(self, tmp_path):
        root = tmp_path / "skills"
        write_skill(root, "pdf-a", name="pdf", description="A")
        write_skill(root, "pdf-b", name="pdf", description="B")

        with pytest.raises(SkillConfigError) as excinfo:
            SkillCatalog(root)

        message = str(excinfo.value)
        assert "技能名重复" in message
        assert "pdf" in message
        assert "pdf-a" in message and "pdf-b" in message

    def test_frontmatter_name_wins_over_dir_name_with_warning(self, tmp_path, caplog):
        root = tmp_path / "skills"
        write_skill(root, "dir-name", name="pretty", description="描述")

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.skills.loader"):
            catalog = SkillCatalog(root)

        assert catalog.names() == ["pretty"]
        assert "不一致" in caplog.text
        assert catalog.get("dir-name") is None  # 索引键是 frontmatter name

    def test_ignores_non_dirs_and_dirs_without_skill_file(self, tmp_path):
        root = tmp_path / "skills"
        root.mkdir()
        (root / "README.md").write_text("普通文件", encoding="utf-8")
        (root / "empty-dir").mkdir()
        write_skill(root, "good", name="good")

        catalog = SkillCatalog(root)

        assert catalog.names() == ["good"]

    def test_broken_yaml_skill_is_skipped_but_others_survive(self, tmp_path, caplog):
        root = tmp_path / "skills"
        write_skill(root, "broken", content="---\nname: [坏\n---\n正文")
        write_skill(root, "good", name="good", description="正常")

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.skills.loader"):
            catalog = SkillCatalog(root)

        assert catalog.names() == ["good"]
        assert "YAML 解析失败" in caplog.text

    def test_unreadable_skill_is_skipped(self, tmp_path, monkeypatch, caplog):
        root = tmp_path / "skills"
        write_skill(root, "good", name="good")
        write_skill(root, "bad", name="bad")
        bad_file = root / "bad" / SKILL_FILE_NAME
        original_read_text = Path.read_text

        def fake_read_text(self, *args, **kwargs):
            if self == bad_file:
                raise PermissionError(13, "Permission denied")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake_read_text)

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.skills.loader"):
            catalog = SkillCatalog(root)

        assert catalog.names() == ["good"]
        assert "读取技能文件失败" in caplog.text

    def test_missing_root_gives_empty_catalog(self, tmp_path):
        catalog = SkillCatalog(tmp_path / "nope")

        assert len(catalog) == 0
        assert catalog.names() == []
        assert catalog.summary() == ""
        assert catalog.load("pdf") is None

    def test_root_file_instead_of_dir_gives_empty_catalog(self, tmp_path):
        not_a_dir = tmp_path / "SKILL.md"
        not_a_dir.write_text("x", encoding="utf-8")

        catalog = SkillCatalog(not_a_dir)

        assert len(catalog) == 0

    def test_relative_root_resolves_against_project_root(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)

        catalog = SkillCatalog("my-skills")

        assert catalog.root == PROJECT_ROOT / "my-skills"

    def test_contains_len_and_repr(self, tmp_path):
        root = tmp_path / "skills"
        write_skill(root, "exec", name="exec")

        catalog = SkillCatalog(root)

        assert "exec" in catalog
        assert "nope" not in catalog
        assert len(catalog) == 1
        assert str(root) in repr(catalog)
        assert "skills=1" in repr(catalog)


# ------------------------------------------------------------------ 查询与正文


class TestLookup:
    @pytest.fixture
    def catalog(self, tmp_path) -> SkillCatalog:
        root = tmp_path / "skills"
        write_skill(root, "exec", name="exec", description="执行", body="# exec 指南\n正文\n")
        return SkillCatalog(root)

    def test_load_returns_body_without_frontmatter(self, catalog):
        assert catalog.load("exec") == "# exec 指南\n正文\n"

    def test_get_returns_skill_model(self, catalog):
        skill = catalog.get("exec")

        assert isinstance(skill, Skill)
        assert skill.name == "exec"
        assert skill.description == "执行"
        assert skill.dir_name == "exec"
        assert "SKILL.md" in skill.source
        assert skill.summary_line() == "- exec (exec/SKILL.md): 执行"

    def test_name_is_stripped(self, catalog):
        assert catalog.get("  exec  ").name == "exec"

    @pytest.mark.parametrize("name", ["", "   ", "nope", "../outside/secret", "a/b"])
    def test_unknown_or_traversal_name_returns_none(self, catalog, tmp_path, name):
        # 不存在同名密钥目录, 但索引查询天然不会触碰文件系统
        outside = tmp_path / "outside" / "secret"
        outside.mkdir(parents=True)
        (outside / SKILL_FILE_NAME).write_text("---\nname: secret\n---\n机密", encoding="utf-8")

        assert catalog.get(name.strip()) is None
        assert catalog.load(name) is None

    def test_skills_returns_all_models(self, catalog):
        assert [skill.name for skill in catalog.skills()] == ["exec"]


# ------------------------------------------------------------------ 摘要渲染


class TestSummary:
    def test_summary_format(self, tmp_path):
        root = tmp_path / "skills"
        write_skill(root, "exec", name="exec", description="执行命令")
        write_skill(root, "pdf", name="pdf", description="处理 PDF")

        summary = SkillCatalog(root).summary()

        assert summary == (
            EXPECTED_HEADER
            + "- exec (exec/SKILL.md): 执行命令\n"
            + "- pdf (pdf/SKILL.md): 处理 PDF\n"
        )
        assert summary.startswith(SKILLS_SUMMARY_HEADER)
        assert summary.endswith("\n")

    def test_empty_catalog_summary_is_empty_string(self, tmp_path):
        assert SkillCatalog(tmp_path / "nope").summary() == ""


# ------------------------------------------------- 包内自带技能(防误删/防写坏)


class TestDefaultBuiltinSkills:
    TOOL_SKILLS = ("exec", "list_dir", "read_file", "web_fetch", "web_search", "write_file")

    def test_default_root_is_package_resource_dir(self):
        root = default_builtin_root()

        assert root.is_dir()
        assert str(root).replace("\\", "/").endswith("meowmeowclaw/skills/builtin")

    def test_skill_files_are_importable_as_resources(self):
        builtin = resources.files("meowmeowclaw.skills").joinpath("builtin")

        for name in self.TOOL_SKILLS:
            skill_file = builtin.joinpath(name, SKILL_FILE_NAME)
            assert skill_file.is_file(), f"缺少内置技能文件: {name}/SKILL.md"
            assert skill_file.read_text(encoding="utf-8").startswith("---\n")

    def test_default_catalog_discovers_all_tool_skills(self):
        catalog = SkillCatalog()

        found = {skill.name: skill for skill in catalog.skills()}
        missing = [name for name in self.TOOL_SKILLS if name not in found]
        assert not missing, f"skills/builtin 缺少工具技能: {missing}"
        for name in self.TOOL_SKILLS:
            assert found[name].description != DEFAULT_DESCRIPTION, f"{name} 缺少 description"
            assert found[name].body.startswith("# "), f"{name} 正文异常"

    def test_default_summary_lists_all_tool_skills(self):
        summary = SkillCatalog().summary()

        for name in self.TOOL_SKILLS:
            assert f"- {name} ({name}/SKILL.md): " in summary
