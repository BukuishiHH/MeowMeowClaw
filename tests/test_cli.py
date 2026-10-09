"""meowmeowclaw/cli.py 的 Mock 单元测试.

- ``interactive_loop``: 真实 Application(JsonlSessionStore + ConversationService + 替身 Provider),
  input() 由 conftest 的 feed_input fixture 脚本化;
- 覆盖 /new、/clear [id] [--purge]、/sessions、/help、/tools、/skills 与失败兜底;
- ``main``: monkeypatch build_application / asyncio.run, 验证启动输出与退出码。
"""

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

import meowmeowclaw.cli as cli_module
from meowmeowclaw.agent.context import ContextBuilder
from meowmeowclaw.agent.loop import AgentLoop
from meowmeowclaw.bootstrap import Application, ConfigError
from meowmeowclaw.channels.cli_adapter import CliAdapter
from meowmeowclaw.channels.cli_policy import CliPolicy
from meowmeowclaw.config import Settings
from meowmeowclaw.conversation import ConversationService
from meowmeowclaw.llm.base import (
    FINISH_REASON_ERROR,
    FINISH_REASON_STOP,
    LLMProvider,
    LLMResponse,
)
from meowmeowclaw.memory import (
    JsonlSessionStore,
    MemoryStoreError,
    SessionKey,
    SessionStoreError,
)
from meowmeowclaw.skills import Skill, SkillConfigError
from meowmeowclaw.tools.filesystem import ReadFileTool
from meowmeowclaw.tools.registry import ToolRegistry


# --------------------------------------------------------------------- 测试替身


class ScriptedProvider(LLMProvider):
    """按脚本返回回答; 可注入错误模拟 Provider 层失败."""

    def __init__(
        self,
        answers: Optional[list[str]] = None,
        *,
        error: Optional[BaseException] = None,
    ) -> None:
        self._answers = list(answers or [])
        self.error = error
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None, model=None) -> LLMResponse:
        self.calls.append([dict(message) for message in messages])
        if self.error is not None:
            return LLMResponse(
                content=f"[LLM调用失败] {self.error}", finish_reason=FINISH_REASON_ERROR
            )
        answer = self._answers.pop(0) if self._answers else "默认回答"
        return LLMResponse(content=answer, finish_reason=FINISH_REASON_STOP)


class FakeCatalog:
    """轻量技能索引替身: 只实现 cli 用到的接口."""

    def __init__(self, skills=(), root: str = "/tmp/fake-skills") -> None:
        self._skills = list(skills)
        self.root = root

    def __len__(self) -> int:
        return len(self._skills)

    def skills(self) -> list:
        return list(self._skills)

    def names(self) -> list:
        return [skill.name for skill in self._skills]

    def summary(self) -> str:
        return "- exec (exec/SKILL.md): 执行命令\n" if self._skills else ""


class RecordingStore:
    """可注入 append 失败的 store 替身, 用于验证 fail-soft 提示."""

    def __init__(self, *, append_error: Optional[BaseException] = None) -> None:
        self.append_error = append_error
        self.appended: list = []

    async def load_recent(self, key, *, max_turns=None, max_chars=None):
        return []

    async def append_turn(self, key, messages, *, turn_id=None, meta=None):
        if self.append_error is not None:
            raise self.append_error
        self.appended.append(list(messages))
        return None  # type: ignore[return-value]


def default_catalog() -> FakeCatalog:
    return FakeCatalog(
        [
            Skill(
                name="exec",
                description="执行命令",
                body="# exec 指南\n",
                dir_name="exec",
                source="/tmp/fake-skills/exec/SKILL.md",
            )
        ]
    )


def cli_session(conversation_id: str = "sess-1") -> SessionKey:
    return SessionKey(channel="cli", scope="session", conversation_id=conversation_id)


def make_application(
    tmp_path,
    provider: LLMProvider,
    *,
    catalog=None,
    store=None,
    config: Optional[Settings] = None,
) -> Application:
    """真实 Application: JSONL store + ConversationService + 真实 AgentLoop(替身 Provider)."""
    config = config or Settings(
        model="test-model",
        api_key="sk-test-key",
        base_url="http://localhost:8000/v1",
        workspace=Path("/tmp/fake-workspace"),
        max_iterations=3,
        memory_dir=tmp_path / "memory",
        memory_max_turns=50,
        memory_max_chars=120_000,
        source="/tmp/fake.env",
    )
    registry = ToolRegistry()
    registry.register(ReadFileTool("/tmp"))
    context = MagicMock(spec=ContextBuilder)
    context.build_messages.side_effect = lambda history=None, current_message="": (
        [{"role": "system", "content": "SYS"}]
        + (list(history) if history else [])
        + ([{"role": "user", "content": current_message}] if current_message else [])
    )

    def agent_factory(session_key: SessionKey) -> AgentLoop:
        return AgentLoop(
            provider=provider,
            tools=registry,
            context=context,
            model=config.model,
            max_iterations=config.max_iterations,
        )

    store = store or JsonlSessionStore(config.memory_dir)
    conversation = ConversationService(
        store,
        agent_factory,
        max_turns=config.memory_max_turns,
        max_chars=config.memory_max_chars,
    )
    return Application(
        config=config,
        provider=provider,
        registry=registry,
        catalog=catalog if catalog is not None else default_catalog(),
        context=context,
        session_store=store,
        conversation=conversation,
    )


# -------------------------------------------------------------- interactive_loop


class TestInteractiveLoop:
    @pytest.mark.asyncio
    async def test_prints_model_answer_and_persists(self, tmp_path, feed_input, capsys):
        provider = ScriptedProvider(["你好, 我是 MeowMeowClaw"])
        app = make_application(tmp_path, provider)
        key = cli_session()
        feed_input("hi", "/exit")

        await cli_module.interactive_loop(app, key)

        out = capsys.readouterr().out
        assert "你好, 我是 MeowMeowClaw" in out
        assert len(provider.calls) == 1
        meta = await app.session_store.get_meta(key)
        assert meta is not None
        assert meta.turn_count == 1

    @pytest.mark.asyncio
    async def test_exit_command_returns_without_calling_model(self, tmp_path, feed_input, capsys):
        provider = ScriptedProvider()
        app = make_application(tmp_path, provider)
        consumed = feed_input("/exit", "这一行不该被读取")

        await cli_module.interactive_loop(app, cli_session())

        assert consumed == ["/exit"]
        assert "再见" in capsys.readouterr().out
        assert provider.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["/quit", "/Q", "/EXIT"])
    async def test_exit_command_aliases_and_case(self, command, tmp_path, feed_input):
        provider = ScriptedProvider()
        app = make_application(tmp_path, provider)
        consumed = feed_input(command, "/exit")

        await cli_module.interactive_loop(app, cli_session())

        assert consumed == [command]
        assert provider.calls == []

    @pytest.mark.asyncio
    async def test_blank_input_is_skipped(self, tmp_path, feed_input):
        provider = ScriptedProvider()
        app = make_application(tmp_path, provider)
        feed_input("", "   ", "/exit")

        await cli_module.interactive_loop(app, cli_session())

        assert provider.calls == []

    @pytest.mark.asyncio
    async def test_help_and_unknown_command(self, tmp_path, feed_input, capsys):
        app = make_application(tmp_path, ScriptedProvider())
        feed_input("/help", "/nope", "/exit")

        await cli_module.interactive_loop(app, cli_session())

        out = capsys.readouterr().out
        assert "/new" in out and "/sessions" in out
        assert "未知命令或参数: /nope" in out

    @pytest.mark.asyncio
    async def test_tools_and_skills_commands(self, tmp_path, feed_input, capsys):
        app = make_application(tmp_path, ScriptedProvider())
        feed_input("/tools", "/skills", "/exit")

        await cli_module.interactive_loop(app, cli_session())

        out = capsys.readouterr().out
        assert "已注册工具(1 个)" in out
        assert "read_file" in out
        assert "已发现技能(1 个)" in out
        assert "exec (exec/SKILL.md): 执行命令" in out

    @pytest.mark.asyncio
    async def test_new_command_switches_session(self, tmp_path, feed_input, capsys):
        provider = ScriptedProvider(["答1", "答2"])
        app = make_application(tmp_path, provider)
        first = cli_session("first")
        feed_input("hi", "/new", "hi", "/exit")

        await cli_module.interactive_loop(app, first)

        out = capsys.readouterr().out
        assert "已开始新会话" in out
        summaries = await app.conversation.list_sessions()
        assert len(summaries) == 2  # /new 之前的会话保留(sessions/), 新会话继续
        assert all(summary.archived is False for summary in summaries)
        assert {summary.turn_count for summary in summaries} == {1}
        assert {summary.session_key for summary in summaries} >= {first}
        assert any(summary.session_key != first for summary in summaries)

    @pytest.mark.asyncio
    async def test_clear_archives_current_and_rotates(self, tmp_path, feed_input, capsys):
        provider = ScriptedProvider(["答1", "答2"])
        app = make_application(tmp_path, provider)
        first = cli_session("first")
        feed_input("hi", "/clear", "hi", "/exit")

        await cli_module.interactive_loop(app, first)

        out = capsys.readouterr().out
        assert "已归档会话" in out
        assert "已开始新会话" in out

        archived = await app.session_store.get_meta(first)
        assert archived is not None
        assert archived.archived is True

        summaries = await app.conversation.list_sessions()
        assert len(summaries) == 2  # 归档的旧会话 + 新会话
        assert {summary.archived for summary in summaries} == {True, False}

    @pytest.mark.asyncio
    async def test_clear_specific_session_by_short_id(self, tmp_path, feed_input, capsys):
        provider = ScriptedProvider(["答A", "答B", "答C"])
        app = make_application(tmp_path, provider)
        target = cli_session("target")
        current = cli_session("current")
        # 预先落盘两个会话(provider 消费答A/答B)
        await app.conversation.handle_message(target, "message-a")
        await app.conversation.handle_message(current, "message-b")

        feed_input(f"/clear {target.storage_id[:8]}", "/exit")

        await cli_module.interactive_loop(app, current)

        assert "已归档会话" in capsys.readouterr().out
        target_meta = await app.session_store.get_meta(target)
        assert target_meta is not None
        assert target_meta.archived is True
        current_meta = await app.session_store.get_meta(current)
        assert current_meta is not None
        assert current_meta.archived is False

    @pytest.mark.asyncio
    async def test_clear_purge_specific_session(self, tmp_path, feed_input, capsys):
        provider = ScriptedProvider(["答A", "答B"])
        app = make_application(tmp_path, provider)
        target = cli_session("target")
        current = cli_session("current")
        await app.conversation.handle_message(target, "message-a")
        await app.conversation.handle_message(current, "message-b")

        feed_input(f"/clear {target.storage_id[:8]} --purge", "/exit")

        await cli_module.interactive_loop(app, current)

        assert "已永久删除会话" in capsys.readouterr().out
        assert await app.session_store.get_meta(target) is None

    @pytest.mark.asyncio
    async def test_sessions_command_lists_sessions(self, tmp_path, feed_input, capsys):
        provider = ScriptedProvider(["答A"])
        app = make_application(tmp_path, provider)
        key = cli_session("visible")
        await app.conversation.handle_message(key, "message-a")

        feed_input("/sessions", "/exit")

        await cli_module.interactive_loop(app, key)

        out = capsys.readouterr().out
        assert key.storage_id[:8] in out
        assert "active" in out
        assert "*" in out  # 当前会话标记

    @pytest.mark.asyncio
    async def test_model_error_is_reported_and_not_persisted(self, tmp_path, feed_input, capsys):
        provider = ScriptedProvider(error=RuntimeError("boom"))
        app = make_application(tmp_path, provider)
        key = cli_session()
        consumed = feed_input("hi", "/exit")

        await cli_module.interactive_loop(app, key)

        assert consumed == ["hi", "/exit"]
        assert "[LLM调用失败]" in capsys.readouterr().out
        assert await app.session_store.get_meta(key) is None

    @pytest.mark.asyncio
    async def test_persist_failure_prints_hint(self, tmp_path, feed_input, capsys):
        store = RecordingStore(append_error=SessionStoreError("disk full"))
        app = make_application(tmp_path, ScriptedProvider(["答1"]), store=store)
        feed_input("hi", "/exit")

        await cli_module.interactive_loop(app, cli_session())

        assert "[提示] 本轮回答未能写入记忆" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_at_prompt_is_graceful(self, tmp_path, monkeypatch, capsys):
        app = make_application(tmp_path, ScriptedProvider())

        def raise_interrupt(prompt: str = "") -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", raise_interrupt)

        await cli_module.interactive_loop(app, cli_session())

        assert "已按下 Ctrl+C" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_eof_at_prompt_is_graceful(self, tmp_path, feed_input, capsys):
        app = make_application(tmp_path, ScriptedProvider())
        feed_input()

        await cli_module.interactive_loop(app, cli_session())

        assert "输入已结束" in capsys.readouterr().out


# ----------------------------------------------------------------------- main


class TestMain:
    def _patch_loop(self, monkeypatch, app: Application) -> list:
        coroutines: list = []

        def fake_run(coro):
            coroutines.append(coro)
            coro.close()
            return None

        monkeypatch.setattr(cli_module, "build_application", MagicMock(return_value=app))
        monkeypatch.setattr(cli_module.asyncio, "run", fake_run)
        return coroutines

    def test_prints_banner_and_starts_loop(self, tmp_path, monkeypatch, capsys):
        app = make_application(tmp_path, ScriptedProvider())
        coroutines = self._patch_loop(monkeypatch, app)

        code = cli_module.main()

        out = capsys.readouterr().out
        assert code == 0
        assert cli_module.APP_NAME in out
        assert "发现 1 个技能: /tmp/fake-skills" in out
        assert "已注册工具(1 个)" in out
        assert "会话      : v1:cli:session:" in out
        assert len(coroutines) == 1

    def test_gateway_enabled_routes_to_gateway_repl(self, tmp_path, monkeypatch, capsys):
        app = replace(make_application(tmp_path, ScriptedProvider()), gateway=MagicMock())
        called: dict = {}

        async def fake_gateway_repl(app_, policy, adapter):
            called["policy"] = policy
            called["adapter"] = adapter

        def fake_run(coro):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(coro)
            finally:
                loop.close()

        monkeypatch.setattr(cli_module, "_run_gateway_repl", fake_gateway_repl)
        monkeypatch.setattr(cli_module, "build_application", MagicMock(return_value=app))
        monkeypatch.setattr(cli_module.asyncio, "run", fake_run)

        code = cli_module.main()

        assert code == 0
        assert isinstance(called["policy"], CliPolicy)
        assert isinstance(called["adapter"], CliAdapter)
        out = capsys.readouterr().out
        assert "会话      : v1:cli:session:" in out

    def test_missing_api_key_returns_code_1(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli_module,
            "build_application",
            MagicMock(side_effect=ConfigError("未读取到 api_key (配置文件: /tmp/.env)")),
        )

        code = cli_module.main()

        out = capsys.readouterr().out
        assert code == 1
        assert "[启动失败]" in out
        assert "api_key" in out
        assert "DEEPSEEK_API_KEY" in out

    def test_skill_config_error_returns_code_1(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli_module,
            "build_application",
            MagicMock(side_effect=SkillConfigError("技能名重复: pdf")),
        )

        code = cli_module.main()

        out = capsys.readouterr().out
        assert code == 1
        assert "技能配置错误" in out

    def test_memory_store_error_returns_code_1(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli_module,
            "build_application",
            MagicMock(side_effect=MemoryStoreError("disk full")),
        )

        code = cli_module.main()

        out = capsys.readouterr().out
        assert code == 1
        assert "记忆存储初始化失败" in out

    def test_missing_identity_prints_warning(self, tmp_path, monkeypatch, capsys):
        app = make_application(tmp_path, ScriptedProvider())
        monkeypatch.setattr(cli_module, "IDENTITY_FILE", tmp_path / "missing-identity.md")
        self._patch_loop(monkeypatch, app)

        cli_module.main()

        assert "[启动警告] 未找到人设文件" in capsys.readouterr().out

    def test_empty_catalog_prints_warning(self, tmp_path, monkeypatch, capsys):
        app = make_application(tmp_path, ScriptedProvider(), catalog=FakeCatalog([]))
        self._patch_loop(monkeypatch, app)

        cli_module.main()

        out = capsys.readouterr().out
        assert "[启动警告] 未发现内置技能" in out
        assert "发现 0 个技能" not in out

    def test_startup_output_does_not_leak_api_key(self, tmp_path, monkeypatch, capsys):
        config = Settings(
            model="test-model",
            api_key="sk-super-secret",
            base_url="http://localhost:8000/v1",
            workspace=Path("/tmp/fake-workspace"),
            max_iterations=3,
            memory_dir=tmp_path / "memory",
            source="/tmp/fake.env",
        )
        app = make_application(tmp_path, ScriptedProvider(), config=config)
        self._patch_loop(monkeypatch, app)

        cli_module.main()

        assert "sk-super-secret" not in capsys.readouterr().out

    def test_keyboard_interrupt_is_graceful(self, tmp_path, monkeypatch, capsys):
        app = make_application(tmp_path, ScriptedProvider())

        def raise_interrupt(coro):
            coro.close()
            raise KeyboardInterrupt

        monkeypatch.setattr(cli_module, "build_application", MagicMock(return_value=app))
        monkeypatch.setattr(cli_module.asyncio, "run", raise_interrupt)

        code = cli_module.main()

        assert code == 0
        assert "已中断" in capsys.readouterr().out
