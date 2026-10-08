"""meowmeowclaw/skills/tool.py(LoadSkillTool) 的单元测试.

LoadSkillTool 是技能子系统对模型的唯一入口: 它把 SkillCatalog 的按名索引能力暴露成
工具, 从而让模型无需接触工作区文件即可取回内置技能正文。这里既验证工具契约与行为,
也验证它与引导语(skills/loader.py)的口径一致 —— 引导语说"调用 load_skill", 工具就必须真的在.

运行: pytest tests/skills/test_tool.py -v
"""

import logging
from pathlib import Path
from typing import Optional

import pytest

from meowmeowclaw.agent.tools.registry import ToolRegistry
from meowmeowclaw.skills import (
    DEFAULT_DESCRIPTION,
    SKILL_FILE_NAME,
    SKILLS_SUMMARY_HEADER,
    LoadSkillTool,
    SkillCatalog,
)
from meowmeowclaw.skills.tool import MAX_SKILL_CHARS, TRUNCATE_NOTICE


def write_skill(
    root: Path,
    dir_name: str,
    *,
    name: Optional[str] = None,
    description: str = "描述",
    body: str = "# 指南\n正文\n",
) -> Path:
    skill_dir = root / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\nname: {name or dir_name}\ndescription: {description}\n---\n{body}"
    (skill_dir / SKILL_FILE_NAME).write_text(content, encoding="utf-8")
    return skill_dir


@pytest.fixture
def catalog(tmp_path) -> SkillCatalog:
    root = tmp_path / "skills"
    write_skill(root, "exec", description="执行命令", body="# exec 指南\n正文\n")
    write_skill(root, "pdf", description="处理 PDF", body="# PDF 指南\n")
    return SkillCatalog(root)


@pytest.fixture
def tool(catalog) -> LoadSkillTool:
    return LoadSkillTool(catalog)


# ------------------------------------------------------------------ 工具契约


class TestToolContract:
    def test_is_base_tool_subclass(self, tool):
        from meowmeowclaw.agent.tools.base import BaseTool

        assert isinstance(tool, BaseTool)

    def test_name_and_label(self, tool):
        assert tool.name == "load_skill"
        assert tool.label == "load_skill"

    def test_description_is_for_model(self, tool):
        assert "技能" in tool.description
        assert "load_skill" not in tool.description  # 描述给模型看, 不自我指涉

    def test_parameters_schema(self, tool):
        params = tool.parameters

        assert params["type"] == "object"
        assert params["properties"]["name"]["type"] == "string"
        assert params["required"] == ["name"]
        assert params["additionalProperties"] is False

    def test_to_function_definition(self, tool):
        definition = tool.to_function_definition()

        assert definition["type"] == "function"
        function = definition["function"]
        assert function["name"] == "load_skill"
        assert function["parameters"] == tool.parameters
        assert function["strict"] is False

    def test_repr_shows_skill_count(self, tool):
        text = repr(tool)

        assert "load_skill" in text
        assert "skills=2" in text


# ------------------------------------------------------------------ 执行行为


class TestExecute:
    @pytest.mark.asyncio
    async def test_returns_body_without_frontmatter(self, tool):
        result = await tool.execute(name="exec")

        assert result == "# exec 指南\n正文\n"
        assert "---" not in result
        assert "description:" not in result

    @pytest.mark.asyncio
    async def test_name_is_stripped(self, tool):
        assert await tool.execute(name="  pdf  ") == "# PDF 指南\n"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", [None, "", "   ", 0])
    async def test_empty_or_falsy_name(self, tool, name):
        # None/空串/纯空白以及 falsy 的 0 都视为"技能名不能为空"
        assert await tool.execute(name=name) == "[错误] 技能名不能为空"

    @pytest.mark.asyncio
    async def test_non_empty_non_string_name_is_treated_as_unknown(self, tool):
        assert "未找到技能: 0" in await tool.execute(name="0")

    @pytest.mark.asyncio
    async def test_missing_name_key_is_safe(self, tool):
        assert await tool.execute() == "[错误] 技能名不能为空"

    @pytest.mark.asyncio
    async def test_unknown_skill_lists_available_names(self, tool):
        result = await tool.execute(name="nope")

        assert result.startswith("[错误] 未找到技能: nope.")
        assert "exec" in result and "pdf" in result

    @pytest.mark.asyncio
    async def test_unknown_skill_logs_warning(self, tool, caplog):
        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.skills.tool"):
            await tool.execute(name="nope")

        assert "技能不存在" in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["../outside/secret", "a/b", "..", "../../etc/passwd"])
    async def test_traversal_style_names_are_just_not_found(self, tool, tmp_path, name):
        outside = tmp_path / "outside" / "secret"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / SKILL_FILE_NAME).write_text("---\nname: secret\n---\n机密", encoding="utf-8")

        result = await tool.execute(name=name)

        assert "未找到技能" in result
        assert "机密" not in result

    @pytest.mark.asyncio
    async def test_body_over_limit_is_truncated(self, tmp_path):
        root = tmp_path / "skills"
        big_body = "x" * (MAX_SKILL_CHARS + 100)
        write_skill(root, "big", body=big_body)

        result = await LoadSkillTool(SkillCatalog(root)).execute(name="big")

        assert len(result) == MAX_SKILL_CHARS + len(TRUNCATE_NOTICE)
        assert result.endswith(TRUNCATE_NOTICE)

    @pytest.mark.asyncio
    async def test_body_at_limit_is_not_truncated(self, tmp_path):
        root = tmp_path / "skills"
        body = "x" * MAX_SKILL_CHARS
        write_skill(root, "exact", body=body)

        result = await LoadSkillTool(SkillCatalog(root)).execute(name="exact")

        assert result == body
        assert TRUNCATE_NOTICE not in result

    @pytest.mark.asyncio
    async def test_empty_body_returns_empty_string(self, tmp_path):
        root = tmp_path / "skills"
        write_skill(root, "empty", body="")

        assert await LoadSkillTool(SkillCatalog(root)).execute(name="empty") == ""

    @pytest.mark.asyncio
    async def test_empty_catalog_says_no_available_skills(self, tmp_path):
        empty_tool = LoadSkillTool(SkillCatalog(tmp_path / "nope"))

        result = await empty_tool.execute(name="pdf")

        assert result.endswith("可用技能: 无")


# ------------------------------------------------------------ Agent 集成(注册表)


class TestRegistryIntegration:
    @pytest.mark.asyncio
    async def test_registry_routes_to_tool(self, catalog):
        registry = ToolRegistry()
        registry.register(LoadSkillTool(catalog))

        assert await registry.execute("load_skill", {"name": "exec"}) == "# exec 指南\n正文\n"

    def test_definition_is_exposed_to_model(self, catalog):
        registry = ToolRegistry()
        registry.register(LoadSkillTool(catalog))

        definitions = registry.get_definitions()
        names = [item["function"]["name"] for item in definitions]

        assert names == ["load_skill"]

    @pytest.mark.asyncio
    async def test_registry_wraps_missing_skill_as_text(self, catalog):
        registry = ToolRegistry()
        registry.register(LoadSkillTool(catalog))

        result = await registry.execute("load_skill", {"name": "nope"})

        assert "未找到技能" in result
        assert "exec" in result


# ------------------------------------------------------- 与引导语/提示词口径一致


class TestPromptConsistency:
    def test_summary_header_tells_model_to_call_this_tool(self, tool):
        assert "load_skill" in SKILLS_SUMMARY_HEADER
        assert tool.name == "load_skill"

    def test_summary_renders_skill_names_used_by_tool(self, catalog, tool):
        summary = catalog.summary()

        assert "- exec (exec/SKILL.md): 执行命令" in summary
        # 摘要里给出的名字, 通过工具一定能加载到
        for name in ("exec", "pdf"):
            assert catalog.get(name) is not None
            assert name in summary


def test_missing_description_falls_back_in_summary(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "plain", description="")

    summary = SkillCatalog(root).summary()

    assert DEFAULT_DESCRIPTION in summary
