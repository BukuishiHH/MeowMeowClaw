"""meowmeowclaw/agent/tools/load_skill.py 的单元测试.

LoadSkillTool 是"技能系统"对模型的唯一入口: 它把 SkillsLoader 的按名加载能力
暴露成工具, 从而让模型无需接触工作区文件即可取回内置技能正文。这里既验证工具契约与行为,
也验证它与引导语(skills/loader.py)的口径一致 —— 引导语说"调用 load_skill", 工具就必须真的在。

运行: pytest tests/test_skills_tool.py -v
"""

import pathlib
from typing import Any, Optional

import pytest

from meowmeowclaw.agent.tools import BaseTool, LoadSkillTool
from meowmeowclaw.agent.tools.load_skill import MAX_SKILL_CHARS, TRUNCATE_NOTICE
from meowmeowclaw.agent.tools.registry import ToolRegistry
from meowmeowclaw.skills import SKILLS_SUMMARY_HEADER, SkillsLoader


def write_skill(root: pathlib.Path, dir_name: str, name: str, description: str, body: str) -> pathlib.Path:
    skill_dir = root / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}", encoding="utf-8"
    )
    return skill_file


@pytest.fixture
def skills_root(tmp_path) -> pathlib.Path:
    root = tmp_path / "skills"
    write_skill(root, "pdf", "pdf", "处理 PDF 文件", "# PDF 指南\n第一步\n")
    write_skill(root, "docx", "docx", "读写 Word", "# Word 指南\n第二步\n")
    return root


@pytest.fixture
def tool(skills_root) -> LoadSkillTool:
    return LoadSkillTool(SkillsLoader(str(skills_root)))


# ------------------------------------------------------------------ 工具契约


class TestToolContract:
    def test_name_description_parameters(self, tool):
        assert isinstance(tool, BaseTool)
        assert tool.name == "load_skill"
        assert "技能" in tool.description
        assert tool.parameters["required"] == ["name"]
        assert tool.parameters["properties"]["name"]["type"] == "string"
        assert tool.parameters["additionalProperties"] is False

    def test_to_function_definition(self, tool):
        definition = tool.to_function_definition()

        assert definition["function"]["name"] == "load_skill"
        assert definition["function"]["parameters"] == tool.parameters

    def test_repr_shows_skills_dir(self, skills_root):
        assert str(skills_root) in repr(LoadSkillTool(SkillsLoader(str(skills_root))))

    def test_guidance_text_points_to_this_tool(self, skills_root):
        """引导语必须指向 load_skill(而不是 read_file), 否则模型会去读不到的地方找."""
        summary = SkillsLoader(str(skills_root)).build_skills_summary()

        assert "load_skill" in SKILLS_SUMMARY_HEADER
        assert "load_skill" in summary
        assert "read_file" not in summary


# ------------------------------------------------------------------ 加载行为


class TestExecute:
    @pytest.mark.asyncio
    async def test_loads_body_without_frontmatter(self, tool):
        result = await tool.execute(name="pdf")

        assert result == "# PDF 指南\n第一步\n"
        assert "---" not in result.splitlines()[0]

    @pytest.mark.asyncio
    async def test_unknown_skill_lists_available_names(self, tool):
        result = await tool.execute(name="不存在的技能")

        assert result.startswith("[错误] 未找到技能: 不存在的技能")
        assert "docx" in result and "pdf" in result  # 便于模型自纠

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kwargs", [{}, {"name": ""}, {"name": "   "}, {"name": None}])
    async def test_empty_name_is_rejected(self, tool, kwargs):
        assert await tool.execute(**kwargs) == "[错误] 技能名不能为空"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["..", "../etc", "../../etc/passwd", "/etc"])
    async def test_escaping_name_is_rejected(self, tool, name):
        result = await tool.execute(name=name)

        assert result.startswith("[错误] 未找到技能: ")

    @pytest.mark.asyncio
    async def test_long_body_is_truncated(self, skills_root):
        write_skill(skills_root, "huge", "huge", "超长技能", "x" * (MAX_SKILL_CHARS + 500))
        tool = LoadSkillTool(SkillsLoader(str(skills_root)))

        result = await tool.execute(name="huge")

        assert len(result) == MAX_SKILL_CHARS + len(TRUNCATE_NOTICE)
        assert result.endswith(TRUNCATE_NOTICE)

    @pytest.mark.asyncio
    async def test_exactly_at_limit_is_kept(self, skills_root):
        write_skill(skills_root, "edge", "edge", "边界技能", "y" * MAX_SKILL_CHARS)
        tool = LoadSkillTool(SkillsLoader(str(skills_root)))

        result = await tool.execute(name="edge")

        assert len(result) == MAX_SKILL_CHARS
        assert TRUNCATE_NOTICE not in result

    @pytest.mark.asyncio
    async def test_unreadable_skill_returns_error_text(self, skills_root, monkeypatch):
        def boom(self, *args: Any, **kwargs: Any) -> str:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(pathlib.Path, "read_text", boom)
        tool = LoadSkillTool(SkillsLoader(str(skills_root)))

        result = await tool.execute(name="pdf")  # 不抛异常

        assert result.startswith("[错误] 未找到技能: pdf")

    @pytest.mark.asyncio
    async def test_name_is_stripped(self, tool):
        assert (await tool.execute(name="  pdf  ")).startswith("# PDF 指南")

    @pytest.mark.asyncio
    async def test_empty_skills_dir_reports_no_available_skills(self, tmp_path):
        tool = LoadSkillTool(SkillsLoader(str(tmp_path / "skills")))

        assert "可用技能: 无" in await tool.execute(name="pdf")


# ------------------------------------------------------------- 与 Registry 联调


class TestRegistryIntegration:
    @pytest.mark.asyncio
    async def test_registry_routes_name_argument(self, tool):
        registry = ToolRegistry()
        registry.register(tool)

        result = await registry.execute("load_skill", {"name": "docx"})

        assert registry.list_tools() == ["load_skill"]
        assert "Word 指南" in result

    @pytest.mark.asyncio
    async def test_registry_with_empty_arguments(self, tool):
        registry = ToolRegistry()
        registry.register(tool)

        assert await registry.execute("load_skill", {}) == "[错误] 技能名不能为空"
