"""meowmeowclaw/bootstrap.py 的单元测试.

测试策略:
- 用 ``monkeypatch`` 顶掉 bootstrap.load_config / bootstrap.SkillCatalog, 提供受控配置与技能目录;
- Provider 用真实 OpenAICompatProvider(离线构造, 不发网络请求)验证装配参数;
- ToolRegistry 用真实实现 + tmp_path 工作区, 验证工具确实绑定到配置路径;
- 本文件只测"装配与异常语义"; 用户可见输出与 REPL 见 test_cli.py.
"""

import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import meowmeowclaw.bootstrap as bootstrap_module
from meowmeowclaw.agent.compression import ContextCompressor
from meowmeowclaw.agent.context import ContextBuilder
from meowmeowclaw.bootstrap import Application, ConfigError, build_application
from meowmeowclaw.config import Settings
from meowmeowclaw.llm.openai_compat import OpenAICompatProvider
from meowmeowclaw.memory import SessionKey
from meowmeowclaw.paths import IDENTITY_FILE, PROJECT_ROOT
from meowmeowclaw.skills import SkillConfigError
from meowmeowclaw.tools.registry import ToolRegistry

WORKSPACE = "/tmp/fake-workspace"

# 装配清单(不包含技能工具; 有技能时额外加 load_skill)
EXPECTED_TOOLS = ("read_file", "write_file", "list_dir", "exec", "web_search", "web_fetch")
EXPECTED_ALL_TOOLS = EXPECTED_TOOLS + ("load_skill",)
BUILTIN_SKILL_NAMES = ["exec", "list_dir", "read_file", "web_fetch", "web_search", "write_file"]


# --------------------------------------------------------------------- 测试替身


def make_settings(**overrides) -> Settings:
    """构造受控 Settings; workspace 统一转 Path, 允许用例继续传 str."""
    params = {
        "model": "test-model",
        "api_key": "sk-test-key",
        "base_url": "http://localhost:8000/v1",
        "workspace": Path(WORKSPACE),
        "max_iterations": 7,
        "source": "/tmp/fake.env",
    }
    params.update(overrides)
    params["workspace"] = Path(params["workspace"])
    # 默认把记忆目录放到 tmp workspace 下, 避免污染仓库
    params["memory_dir"] = Path(params.get("memory_dir") or (params["workspace"] / "memory"))
    params.setdefault("memory_max_turns", 20)
    params.setdefault("memory_max_chars", 50_000)
    return Settings(**params)


class _EmptyCatalog:
    """"没有任何技能"的假 catalog, 用于覆盖无技能装配分支."""

    root = "/nonexistent/skills"

    def summary(self) -> str:
        return ""

    def __len__(self) -> int:
        return 0

    def names(self) -> list:
        return []


def make_empty_catalog(*args, **kwargs) -> _EmptyCatalog:
    return _EmptyCatalog()


# ------------------------------------------------------------------ 工具注册


class TestBuildRegistry:
    def test_registers_expected_tools(self):
        registry = bootstrap_module.build_registry(make_settings())

        assert isinstance(registry, ToolRegistry)
        assert registry.list_tools() == list(EXPECTED_TOOLS)

    @pytest.mark.asyncio
    async def test_tools_are_bound_to_config_workspace(self, tmp_path):
        config = make_settings(workspace=tmp_path)
        registry = bootstrap_module.build_registry(config)
        (tmp_path / "a.txt").write_text("工作区里的内容", encoding="utf-8")

        # 读: 能读到工作区内的真实文件
        assert await registry.execute("read_file", {"file_path": "a.txt"}) == "工作区里的内容"
        # 写: 落盘在工作区内
        await registry.execute("write_file", {"file_path": "sub/b.txt", "content": "新文件"})
        assert (tmp_path / "sub" / "b.txt").read_text(encoding="utf-8") == "新文件"
        # 列目录
        assert "a.txt" in await registry.execute("list_dir", {"dir_path": ""})
        # 越界防护仍然生效
        assert "安全拦截" in await registry.execute("read_file", {"file_path": "../outside.txt"})


# ------------------------------------------------------------------ 工作目录


class TestEnsureWorkspace:
    def test_creates_directory_and_returns_true(self, tmp_path):
        target = tmp_path / "auto" / "created"

        assert bootstrap_module.ensure_workspace(target) is True
        assert target.is_dir()

    def test_failure_is_logged_not_raised(self, tmp_path, monkeypatch, caplog):
        def boom(self, *args, **kwargs):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(Path, "mkdir", boom)

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.bootstrap"):
            result = bootstrap_module.ensure_workspace(tmp_path / "blocked")

        assert result is False
        assert "创建工作目录失败" in caplog.text


# ------------------------------------------------------------------ 装配结果


class TestBuildApplication:
    def test_missing_api_key_raises_config_error(self, monkeypatch):
        monkeypatch.setattr(
            bootstrap_module, "load_config", lambda *a, **k: make_settings(api_key="")
        )

        with pytest.raises(ConfigError) as excinfo:
            build_application()

        assert "api_key" in str(excinfo.value)

    def test_wires_components_from_config(self, monkeypatch, tmp_path):
        config = make_settings(workspace=tmp_path)
        monkeypatch.setattr(bootstrap_module, "load_config", lambda *a, **k: config)

        app = build_application()

        assert isinstance(app, Application)
        # Provider 用配置里的密钥/地址/模型
        assert isinstance(app.provider, OpenAICompatProvider)
        assert app.provider.api_key == config.api_key
        assert app.provider.base_url == config.base_url
        assert app.provider.model == config.model
        # 工具清单 = 6 内置 + 内置技能带来的 load_skill
        assert app.registry.list_tools() == list(EXPECTED_ALL_TOOLS)
        # 技能: 包内 6 个内置技能
        assert len(app.catalog) == 6
        # Context 与 Loop 的配置
        assert isinstance(app.context, ContextBuilder)
        assert app.context.workspace == tmp_path.resolve()
        assert app.context.identity_path == IDENTITY_FILE
        assert "- exec (exec/SKILL.md): " in app.context.skills_summary
        # 记忆系统: JSONL store + ConversationService 已装配
        assert app.session_store.root == config.memory_dir
        assert app.session_store.sessions_dir.is_dir()
        assert app.conversation.store is app.session_store
        # 文件工具与 ContextBuilder 都拿到了配置的记忆目录
        assert app.registry._tools["read_file"].memory_dir == str(config.memory_dir)  # noqa: SLF001
        assert app.registry._tools["write_file"].memory_dir == str(config.memory_dir)  # noqa: SLF001
        assert app.context.memory_path == config.memory_dir / "MEMORY.md"
        assert app.conversation.max_turns == config.memory_max_turns
        assert app.conversation.max_chars == config.memory_max_chars

    def test_load_config_is_called_once(self, monkeypatch):
        loader = MagicMock(return_value=make_settings())
        monkeypatch.setattr(bootstrap_module, "load_config", loader)

        build_application()

        loader.assert_called_once_with(None)

    def test_creates_workspace_directory(self, monkeypatch, tmp_path):
        target = tmp_path / "auto" / "created"
        monkeypatch.setattr(
            bootstrap_module, "load_config", lambda *a, **k: make_settings(workspace=target)
        )

        build_application()

        assert target.is_dir()
        assert (target / "memory" / "sessions").is_dir()

    def test_skill_config_error_propagates(self, monkeypatch):
        monkeypatch.setattr(bootstrap_module, "load_config", lambda *a, **k: make_settings())

        def boom(*args, **kwargs):
            raise SkillConfigError("技能名重复: pdf <- pdf-a/pdf-b")

        monkeypatch.setattr(bootstrap_module, "SkillCatalog", boom)

        with pytest.raises(SkillConfigError):
            build_application()

    def test_empty_catalog_skips_load_skill(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            bootstrap_module, "load_config", lambda *a, **k: make_settings(workspace=tmp_path)
        )
        monkeypatch.setattr(bootstrap_module, "SkillCatalog", make_empty_catalog)

        app = build_application()

        assert app.registry.list_tools() == list(EXPECTED_TOOLS)
        assert app.context.skills_summary == ""
        assert len(app.catalog) == 0

    def test_workspace_skills_are_ignored(self, monkeypatch, tmp_path):
        # 技能只来自包内置目录: 用户往 workspace/skills 放技能不应被读取
        skill_dir = tmp_path / "skills" / "pdf"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: pdf\ndescription: 处理 PDF 文件\n---\n# 指南\n正文\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            bootstrap_module, "load_config", lambda *a, **k: make_settings(workspace=tmp_path)
        )

        app = build_application()

        assert len(app.catalog) == 6
        assert "pdf" not in app.context.skills_summary
        assert app.catalog.names() == BUILTIN_SKILL_NAMES


    def test_compression_is_wired_per_session(self, monkeypatch, tmp_path):
        config = make_settings(
            workspace=tmp_path,
            compression_enabled=True,
            token_budget=12345,
            keep_recent_turns=3,
            summary_model="cheap-model",
        )
        monkeypatch.setattr(bootstrap_module, "load_config", lambda *a, **k: config)
        app = build_application()

        key = SessionKey(channel="cli", scope="session", conversation_id="abc")
        agent = app.conversation._agent_for(key)  # noqa: SLF001 - 装配契约
        compressor = agent.compressor
        assert isinstance(compressor, ContextCompressor)
        assert compressor.token_budget == 12345
        assert compressor.keep_recent_turns == 3
        assert compressor.summary_model == "cheap-model"
        assert compressor.session == key.canonical
        assert compressor.provider is app.provider
        # 同会话复用同一个 AgentLoop/compressor, 不同会话各自实例
        assert app.conversation._agent_for(key) is agent  # noqa: SLF001
        other = app.conversation._agent_for(
            SessionKey(channel="cli", scope="session", conversation_id="other")
        )
        assert other.compressor is not compressor

    def test_compression_disabled_passes_none(self, monkeypatch, tmp_path):
        config = make_settings(workspace=tmp_path, compression_enabled=False)
        monkeypatch.setattr(bootstrap_module, "load_config", lambda *a, **k: config)
        app = build_application()

        key = SessionKey(channel="cli", scope="session", conversation_id="abc")
        agent = app.conversation._agent_for(key)  # noqa: SLF001

        assert agent.compressor is None


# ------------------------------------------------------- 包导入边界(无副作用)


class TestBootstrapImportBoundary:
    def test_import_bootstrap_does_not_load_concrete_tools(self):
        code = (
            "import sys\n"
            "import meowmeowclaw.bootstrap\n"
            "loaded = [name for name in (\n"
            "    'meowmeowclaw.tools.filesystem',\n"
            "    'meowmeowclaw.tools.shell',\n"
            "    'meowmeowclaw.tools.web_search',\n"
            "    'meowmeowclaw.tools.web_fetch',\n"
            ") if name in sys.modules]\n"
            "print(','.join(loaded))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""
