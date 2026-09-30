"""backend/main.py 的 Mock 单元测试.

测试策略:
- ``build_agent()``: 用 ``monkeypatch`` 顶掉 ``load_config`` 提供受控配置,
  Provider 用真实实现(离线构造, 不发网络请求)以验证装配参数, 工具链则真的读写 tmp_path;
- ``interactive_loop()``: 用真实 ``AgentLoop`` + Mock Provider 驱动, 只把 ``input()`` 换成脚本化的
  假实现(取尽即抛 EOFError, 防止用例写错时死循环), 从而稳定复现各命令与 Ctrl+C/Ctrl+D 分支;
- ``main()``: Mock 掉 ``build_agent`` 与 ``asyncio.run``, 只验证启动流程与异常兜底。

运行: pytest backend/test/test_main.py -v
"""

import asyncio
import copy
from typing import Optional
from unittest.mock import MagicMock

import pytest

import backend.main as main_module
from backend.agent.context import ContextBuilder
from backend.agent.loop import AgentLoop
from backend.agent.tools.registry import ToolRegistry
from backend.config import Settings
from backend.providers.base import FINISH_REASON_STOP, LLMProvider, LLMResponse
from backend.providers.openai_compat import OpenAICompatProvider

WORKSPACE = "/tmp/fake-workspace"

# build_agent() 应当注册的工具清单(新增工具时只改这一处, 计数与断言自动跟随)
EXPECTED_TOOLS = ("read_file", "write_file", "list_dir", "exec", "web_search", "web_fetch")


# --------------------------------------------------------------------- 测试替身


def make_settings(**overrides) -> Settings:
    params = {
        "model": "test-model",
        "api_key": "sk-test-key",
        "base_url": "http://localhost:8000/v1",
        "workspace": WORKSPACE,
        "max_iterations": 7,
        "identity_file": "identity.md",
        "source": "/tmp/fake.env",
    }
    params.update(overrides)
    return Settings(**params)


class StubProvider(LLMProvider):
    """最小真实 Provider: 记录每次收到的 messages 深拷贝快照, 可注入异常."""

    def __init__(self, answer: str = "模型回答", error: Optional[BaseException] = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None, model=None) -> LLMResponse:
        self.calls.append(copy.deepcopy(messages))
        if self.error is not None:
            raise self.error
        return LLMResponse(content=self.answer, finish_reason=FINISH_REASON_STOP)


def make_stub_agent(answer: str = "模型回答", error: Optional[BaseException] = None) -> AgentLoop:
    """真实 AgentLoop(含真实 ToolRegistry) + 替身 Provider / Context(离线可跑)."""
    registry = ToolRegistry()
    registry.register(main_module.ReadFileTool("/tmp"))

    # 忠实模仿真实 ContextBuilder 的拼装规则(system + 历史 + 当前消息)
    context = MagicMock(spec=ContextBuilder)
    context.build_messages.side_effect = lambda history=None, current_message="": (
        [{"role": "system", "content": "SYS"}]
        + (list(history) if history else [])
        + ([{"role": "user", "content": current_message}] if current_message else [])
    )

    return AgentLoop(provider=StubProvider(answer, error), tools=registry, context=context)


@pytest.fixture
def feed_input(monkeypatch):
    """把 input() 换成脚本化输入; 脚本用尽后抛 EOFError, 避免死循环."""

    def _feed(*lines: str) -> list[str]:
        consumed: list[str] = []
        iterator = iter(lines)

        def fake_input(prompt: str = "") -> str:
            try:
                value = next(iterator)
            except StopIteration:
                raise EOFError
            consumed.append(value)
            return value

        monkeypatch.setattr("builtins.input", fake_input)
        return consumed

    return _feed


# ------------------------------------------------------------------ build_agent


class TestBuildAgent:
    def test_exits_with_code_1_when_api_key_missing(self, monkeypatch, capsys):
        monkeypatch.setattr(main_module, "load_config", lambda *a, **k: make_settings(api_key=""))

        with pytest.raises(SystemExit) as excinfo:
            main_module.build_agent()

        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "api_key" in out
        assert "[启动失败]" in out

    def test_wires_components_from_config(self, monkeypatch):
        config = make_settings()
        monkeypatch.setattr(main_module, "load_config", lambda *a, **k: config)

        agent = main_module.build_agent()

        assert isinstance(agent, AgentLoop)
        # Provider 用配置里的密钥/地址/模型
        assert isinstance(agent.provider, OpenAICompatProvider)
        assert agent.provider.api_key == config.api_key
        assert agent.provider.base_url == config.base_url
        assert agent.provider.model == config.model
        # 工具清单与预期完全一致(多一个少一个都要失败)
        assert agent.tools.list_tools() == list(EXPECTED_TOOLS)
        # Context 与 Loop 的配置
        assert isinstance(agent.context, ContextBuilder)
        assert agent.context.workspace == config.workspace
        assert agent.context.identity_file == config.identity_file
        assert agent.model == config.model
        assert agent.max_iterations == config.max_iterations

    def test_load_config_is_called_once(self, monkeypatch):
        loader = MagicMock(return_value=make_settings())
        monkeypatch.setattr(main_module, "load_config", loader)

        main_module.build_agent()

        loader.assert_called_once_with()

    def test_prints_registered_tools(self, monkeypatch, capsys):
        monkeypatch.setattr(main_module, "load_config", lambda *a, **k: make_settings())

        main_module.build_agent()

        out = capsys.readouterr().out
        assert f"已注册工具({len(EXPECTED_TOOLS)} 个)" in out
        for name in EXPECTED_TOOLS:
            assert name in out

    def test_startup_output_does_not_leak_api_key(self, monkeypatch, capsys):
        monkeypatch.setattr(
            main_module, "load_config", lambda *a, **k: make_settings(api_key="sk-super-secret")
        )

        main_module.build_agent()

        assert "sk-super-secret" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_tools_are_bound_to_config_workspace(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            main_module, "load_config", lambda *a, **k: make_settings(workspace=str(tmp_path))
        )
        (tmp_path / "a.txt").write_text("工作区里的内容", encoding="utf-8")

        agent = main_module.build_agent()

        # 读: 能读到工作区内的真实文件
        assert await agent.tools.execute("read_file", {"file_path": "a.txt"}) == "工作区里的内容"
        # 写: 落盘在工作区内
        await agent.tools.execute("write_file", {"file_path": "sub/b.txt", "content": "新文件"})
        assert (tmp_path / "sub" / "b.txt").read_text(encoding="utf-8") == "新文件"
        # 列目录
        assert "a.txt" in await agent.tools.execute("list_dir", {"dir_path": ""})
        # 越界防护仍然生效
        assert "安全拦截" in await agent.tools.execute("read_file", {"file_path": "../outside.txt"})


# -------------------------------------------------------------- interactive_loop


class TestInteractiveLoop:
    @pytest.mark.asyncio
    async def test_prints_model_answer(self, capsys, feed_input):
        agent = make_stub_agent("你好, 我是 MeowMeowClaw")
        feed_input("hi", "/exit")

        await main_module.interactive_loop(agent)

        out = capsys.readouterr().out
        assert "你好, 我是 MeowMeowClaw" in out
        assert len(agent.provider.calls) == 1

    @pytest.mark.asyncio
    async def test_exit_command_returns_without_calling_model(self, capsys, feed_input):
        agent = make_stub_agent()
        consumed = feed_input("/exit", "这一行不该被读取")

        await main_module.interactive_loop(agent)

        assert consumed == ["/exit"]  # 立即退出, 不再消费后续输入
        out = capsys.readouterr().out
        assert "再见" in out
        assert "未知命令" not in out
        assert agent.provider.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["/quit", "/Q", "/EXIT"])
    async def test_exit_command_aliases_and_case(self, command, feed_input):
        agent = make_stub_agent()
        consumed = feed_input(command, "/exit")

        await main_module.interactive_loop(agent)

        assert consumed == [command]  # 别名同样立即退出
        assert agent.provider.calls == []

    @pytest.mark.asyncio
    async def test_blank_input_is_skipped(self, capsys, feed_input):
        agent = make_stub_agent()
        feed_input("", "   ", "/exit")

        await main_module.interactive_loop(agent)

        assert agent.provider.calls == []

    @pytest.mark.asyncio
    async def test_clear_command_empties_history(self, capsys, feed_input):
        agent = make_stub_agent()
        feed_input("第一问", "/clear", "/exit")

        await main_module.interactive_loop(agent)

        assert agent._session_history == []
        assert agent._tool_call_history == []
        assert "已清空" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_tools_command_lists_tools(self, capsys, feed_input):
        agent = make_stub_agent()
        feed_input("/tools", "/exit")

        await main_module.interactive_loop(agent)

        out = capsys.readouterr().out
        assert "read_file" in out
        assert agent.provider.calls == []

    @pytest.mark.asyncio
    async def test_unknown_command_shows_hint(self, capsys, feed_input):
        agent = make_stub_agent()
        feed_input("/help", "/exit")

        await main_module.interactive_loop(agent)

        assert "未知命令" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_at_prompt_exits_gracefully(self, capsys, monkeypatch):
        def raise_interrupt(prompt: str = ""):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", raise_interrupt)

        await main_module.interactive_loop(make_stub_agent())  # 不抛异常

        assert "再见" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_eof_at_prompt_exits_gracefully(self, capsys, feed_input):
        feed_input()  # 立刻 EOF

        await main_module.interactive_loop(make_stub_agent())

        assert "再见" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_agent_exception_is_reported_and_loop_continues(self, capsys, feed_input):
        agent = make_stub_agent(error=RuntimeError("上游炸了"))
        feed_input("第一问", "第二问", "/exit")

        await main_module.interactive_loop(agent)

        out = capsys.readouterr().out
        assert "[异常]" in out
        assert "上游炸了" in out

    @pytest.mark.asyncio
    async def test_conversation_history_grows_across_turns(self, feed_input):
        agent = make_stub_agent()
        feed_input("第一问", "第二问", "/exit")

        await main_module.interactive_loop(agent)

        assert len(agent.provider.calls) == 2
        roles = [message["role"] for message in agent.provider.calls[1]]
        assert roles == ["system", "user", "assistant", "user"]  # 第二轮带上了历史


# ----------------------------------------------------------------------- main


class TestMain:
    def test_prints_banner_and_starts_loop(self, monkeypatch, capsys):
        agent = make_stub_agent()
        monkeypatch.setattr(main_module, "build_agent", MagicMock(return_value=agent))
        captured: dict = {}

        def fake_run(coro):
            captured["coro"] = coro
            coro.close()  # 避免 "coroutine was never awaited" 告警
            return None

        monkeypatch.setattr(main_module.asyncio, "run", fake_run)

        main_module.main()

        out = capsys.readouterr().out
        assert main_module.APP_NAME in out          # banner
        main_module.build_agent.assert_called_once_with()
        assert asyncio.iscoroutine(captured["coro"])

    def test_keyboard_interrupt_is_graceful(self, monkeypatch, capsys):
        monkeypatch.setattr(main_module, "build_agent", MagicMock(return_value=make_stub_agent()))

        def raise_interrupt(coro):
            coro.close()
            raise KeyboardInterrupt

        monkeypatch.setattr(main_module.asyncio, "run", raise_interrupt)

        main_module.main()  # 不抛异常

        assert "再见" in capsys.readouterr().out

    def test_stub_agent_helper_is_usable(self, feed_input, capsys):
        """自检: 测试替身本身能跑通一轮, 避免替身失真导致用例失效."""
        agent = make_stub_agent("自检回答")
        feed_input("hi", "/exit")

        asyncio.run(main_module.interactive_loop(agent))

        assert "自检回答" in capsys.readouterr().out
