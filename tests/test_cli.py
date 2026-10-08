"""meowmeowclaw/cli.py 的 Mock 单元测试.

测试策略:
- ``interactive_loop`` 用 make_stub_application: 真实 AgentLoop + 替身 Provider,
  input() 由 conftest 的 feed_input fixture 脚本化(取尽抛 EOFError, 防止死循环);
- ``main`` 用 monkeypatch 顶掉 build_application / asyncio.run, 只验证启动流程、
  退出码与用户可见输出;
- 装配本身(工具/技能/Provider)见 test_bootstrap.py, 这里不重复测.
"""

from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

import meowmeowclaw.cli as cli_module
from meowmeowclaw.agent.context import ContextBuilder
from meowmeowclaw.agent.loop import AgentLoop
from meowmeowclaw.bootstrap import Application, ConfigError
from meowmeowclaw.config import Settings
from meowmeowclaw.llm.base import FINISH_REASON_STOP, LLMProvider, LLMResponse
from meowmeowclaw.skills import Skill, SkillConfigError
from meowmeowclaw.tools.filesystem import ReadFileTool
from meowmeowclaw.tools.registry import ToolRegistry

# --------------------------------------------------------------------- 测试替身


class StubProvider(LLMProvider):
    """最小真实 Provider: 记录 messages 快照, 可注入异常."""

    def __init__(self, answer: str = "模型回答", error: Optional[BaseException] = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None, model=None) -> LLMResponse:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return LLMResponse(content=self.answer, finish_reason=FINISH_REASON_STOP)


class FakeCatalog:
    """轻量技能索引替身: 只实现 cli 用到的接口, 避免测试依赖真实内置资源."""

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


def make_stub_application(
    answer: str = "模型回答",
    error: Optional[BaseException] = None,
    catalog: Optional[FakeCatalog] = None,
    config: Optional[Settings] = None,
) -> Application:
    """真实 AgentLoop(含真实 ToolRegistry) + 替身 Provider / Context, 离线可跑."""
    config = config or Settings(
        model="test-model",
        api_key="sk-test-key",
        base_url="http://localhost:8000/v1",
        workspace=Path("/tmp/fake-workspace"),
        max_iterations=3,
        source="/tmp/fake.env",
    )
    provider = StubProvider(answer, error)
    registry = ToolRegistry()
    registry.register(ReadFileTool("/tmp"))

    # 忠实模仿真实 ContextBuilder 的拼装规则(system + 历史 + 当前消息)
    context = MagicMock(spec=ContextBuilder)
    context.build_messages.side_effect = lambda history=None, current_message="": (
        [{"role": "system", "content": "SYS"}]
        + (list(history) if history else [])
        + ([{"role": "user", "content": current_message}] if current_message else [])
    )

    agent = AgentLoop(
        provider=provider,
        tools=registry,
        context=context,
        model=config.model,
        max_iterations=config.max_iterations,
    )
    return Application(
        config=config,
        provider=provider,
        registry=registry,
        catalog=catalog if catalog is not None else default_catalog(),
        context=context,
        agent=agent,
    )


# -------------------------------------------------------------- interactive_loop


class TestInteractiveLoop:
    @pytest.mark.asyncio
    async def test_prints_model_answer(self, feed_input, capsys):
        app = make_stub_application("你好, 我是 MeowMeowClaw")
        feed_input("hi", "/exit")

        await cli_module.interactive_loop(app)

        out = capsys.readouterr().out
        assert "你好, 我是 MeowMeowClaw" in out
        assert len(app.provider.calls) == 1

    @pytest.mark.asyncio
    async def test_exit_command_returns_without_calling_model(self, feed_input, capsys):
        app = make_stub_application()
        consumed = feed_input("/exit", "这一行不该被读取")

        await cli_module.interactive_loop(app)

        assert consumed == ["/exit"]  # 立即退出, 不再消费后续输入
        assert "再见" in capsys.readouterr().out
        assert app.provider.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["/quit", "/Q", "/EXIT"])
    async def test_exit_command_aliases_and_case(self, command, feed_input):
        app = make_stub_application()
        consumed = feed_input(command, "/exit")

        await cli_module.interactive_loop(app)

        assert consumed == [command]  # 别名同样立即退出
        assert app.provider.calls == []

    @pytest.mark.asyncio
    async def test_blank_input_is_skipped(self, feed_input):
        app = make_stub_application()
        feed_input("", "   ", "/exit")

        await cli_module.interactive_loop(app)

        assert app.provider.calls == []

    @pytest.mark.asyncio
    async def test_clear_command_empties_history(self, feed_input, capsys):
        app = make_stub_application()
        feed_input("第一问", "/clear", "/exit")

        await cli_module.interactive_loop(app)

        assert app.agent._session_history == []
        assert "已清空对话历史与工具调用记录" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_tools_command_prints_registry(self, feed_input, capsys):
        app = make_stub_application()
        feed_input("/tools", "/exit")

        await cli_module.interactive_loop(app)

        out = capsys.readouterr().out
        assert "已注册工具(1 个)" in out
        assert "read_file" in out

    @pytest.mark.asyncio
    async def test_skills_command_prints_catalog(self, feed_input, capsys):
        app = make_stub_application()
        feed_input("/skills", "/exit")

        await cli_module.interactive_loop(app)

        out = capsys.readouterr().out
        assert "已发现技能(1 个)" in out
        assert "exec (exec/SKILL.md): 执行命令" in out

    @pytest.mark.asyncio
    async def test_unknown_command_prints_hint(self, feed_input, capsys):
        app = make_stub_application()
        feed_input("/nope", "/exit")

        await cli_module.interactive_loop(app)

        out = capsys.readouterr().out
        assert "未知命令: /nope" in out
        assert "/skills" in out

    @pytest.mark.asyncio
    async def test_model_exception_is_reported_and_session_continues(self, feed_input, capsys):
        app = make_stub_application(error=RuntimeError("boom"))
        consumed = feed_input("hi", "/exit")

        await cli_module.interactive_loop(app)  # 不抛异常

        assert consumed == ["hi", "/exit"]
        assert "[异常] 本轮处理失败" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_at_prompt_is_graceful(self, monkeypatch, capsys):
        app = make_stub_application()

        def raise_interrupt(prompt: str = "") -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", raise_interrupt)

        await cli_module.interactive_loop(app)  # 不抛异常

        assert "已按下 Ctrl+C" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_eof_at_prompt_is_graceful(self, feed_input, capsys):
        app = make_stub_application()
        feed_input()  # 第一行就取尽 -> EOFError

        await cli_module.interactive_loop(app)  # 不抛异常

        assert "输入已结束" in capsys.readouterr().out


# ----------------------------------------------------------------------- main


class TestMain:
    def test_prints_banner_and_starts_loop(self, monkeypatch, capsys):
        app = make_stub_application()
        coroutines = []

        def fake_run(coro):
            coroutines.append(coro)
            coro.close()  # 不真正进入交互循环
            return None

        monkeypatch.setattr(cli_module, "build_application", MagicMock(return_value=app))
        monkeypatch.setattr(cli_module.asyncio, "run", fake_run)

        code = cli_module.main()

        out = capsys.readouterr().out
        assert code == 0
        assert cli_module.APP_NAME in out          # banner
        assert "发现 1 个技能: /tmp/fake-skills" in out
        assert "已注册工具(1 个)" in out
        assert len(coroutines) == 1

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
        assert "技能名重复" in out

    def test_missing_identity_prints_warning(self, monkeypatch, capsys, tmp_path):
        app = make_stub_application()
        monkeypatch.setattr(cli_module, "IDENTITY_FILE", tmp_path / "missing-identity.md")
        monkeypatch.setattr(cli_module, "build_application", MagicMock(return_value=app))
        monkeypatch.setattr(cli_module.asyncio, "run", lambda coro: coro.close())

        cli_module.main()

        out = capsys.readouterr().out
        assert "[启动警告] 未找到人设文件" in out

    def test_empty_catalog_prints_warning(self, monkeypatch, capsys):
        app = make_stub_application(catalog=FakeCatalog([]))
        monkeypatch.setattr(cli_module, "build_application", MagicMock(return_value=app))
        monkeypatch.setattr(cli_module.asyncio, "run", lambda coro: coro.close())

        cli_module.main()

        out = capsys.readouterr().out
        assert "[启动警告] 未发现内置技能" in out
        assert "发现 0 个技能" not in out

    def test_keyboard_interrupt_is_graceful(self, monkeypatch, capsys):
        def raise_interrupt(coro):
            coro.close()
            raise KeyboardInterrupt

        monkeypatch.setattr(cli_module, "build_application", MagicMock(return_value=make_stub_application()))
        monkeypatch.setattr(cli_module.asyncio, "run", raise_interrupt)

        code = cli_module.main()  # 不抛异常

        assert code == 0
        assert "已中断" in capsys.readouterr().out

    def test_startup_output_does_not_leak_api_key(self, monkeypatch, capsys):
        app = make_stub_application(
            config=Settings(
                model="test-model",
                api_key="sk-super-secret",
                base_url="http://localhost:8000/v1",
                workspace=Path("/tmp/fake-workspace"),
                max_iterations=3,
                source="/tmp/fake.env",
            )
        )
        monkeypatch.setattr(cli_module, "build_application", MagicMock(return_value=app))
        monkeypatch.setattr(cli_module.asyncio, "run", lambda coro: coro.close())

        cli_module.main()

        assert "sk-super-secret" not in capsys.readouterr().out
