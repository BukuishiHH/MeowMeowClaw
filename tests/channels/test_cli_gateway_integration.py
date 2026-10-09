"""CLI 渠道走网关的端到端对等测试(W3)."""

import pytest

from meowmeowclaw.channels.cli_adapter import CliAdapter
from meowmeowclaw.channels.cli_policy import CliPolicy
from meowmeowclaw.gateway import Gateway
from meowmeowclaw.memory import SessionKey

from _qq_helpers import ScriptedProvider, build_conversation


async def build_stack(tmp_path, provider, *, inputs=None, outputs=None):
    conversation = build_conversation(tmp_path / "memory", provider)
    counter = {"n": 0}

    def factory() -> SessionKey:
        counter["n"] += 1
        return SessionKey(channel="cli", scope="session", conversation_id=f"cli-{counter['n']}")

    policy = CliPolicy(conversation, session_factory=factory)
    input_iter = iter(inputs or [])
    adapter = CliAdapter(
        input_func=(lambda prompt: next(input_iter)) if inputs is not None else None,
        print_func=(outputs.append if outputs is not None else (lambda text: None)),
    )
    gateway = Gateway(conversation=conversation, policies={"cli": policy})
    await gateway.start(adapters=[adapter])
    return gateway, adapter, policy, conversation


class TestCliGatewayShutdown:
    @pytest.mark.asyncio
    async def test_keyboard_interrupt_still_stops_gateway(self, tmp_path):
        from types import SimpleNamespace

        from meowmeowclaw.cli import _run_gateway_repl

        provider = ScriptedProvider(["答"])
        conversation = build_conversation(tmp_path / "memory", provider)
        policy = CliPolicy(
            conversation,
            session_factory=lambda: SessionKey(
                channel="cli", scope="session", conversation_id="ki-1"
            ),
        )
        adapter = CliAdapter(
            input_func=lambda prompt: (_ for _ in ()).throw(KeyboardInterrupt()),
            print_func=lambda text: None,
        )
        gateway = Gateway(conversation=conversation)
        app = SimpleNamespace(gateway=gateway)

        with pytest.raises(KeyboardInterrupt):
            await _run_gateway_repl(app, policy, adapter)

        assert gateway.started is False


class TestCliGatewayParity:
    @pytest.mark.asyncio
    async def test_ask_round_trip_persists(self, tmp_path):
        provider = ScriptedProvider(["你好呀"])
        gateway, adapter, policy, conversation = await build_stack(tmp_path, provider)

        reply = await adapter.ask("hi")

        assert reply is not None and reply.text == "你好呀"
        meta = await conversation.get_meta(policy.current_session)
        assert meta is not None and meta.turn_count == 1
        await gateway.stop()

    @pytest.mark.asyncio
    async def test_repl_answers_help_and_exit(self, tmp_path):
        provider = ScriptedProvider(["回答一"])
        outputs: list[str] = []
        gateway, adapter, _, _ = await build_stack(
            tmp_path, provider, inputs=["你好", "/help", "/exit"], outputs=outputs
        )

        assert await adapter.run_repl() == "exit"
        text = "\n".join(outputs)
        assert "回答一" in text
        assert "/new" in text  # /help 内容
        assert len(provider.calls) == 1  # /help 不进 Agent
        await gateway.stop()

    @pytest.mark.asyncio
    async def test_new_command_switches_session(self, tmp_path):
        provider = ScriptedProvider(["答1", "答2"])
        gateway, adapter, policy, conversation = await build_stack(tmp_path, provider)
        first = policy.current_session

        await adapter.ask("第一问")
        await adapter.ask("/new")
        second = policy.current_session
        await adapter.ask("第二问")

        assert second != first
        first_meta = await conversation.get_meta(first)
        second_meta = await conversation.get_meta(second)
        assert first_meta is not None and first_meta.turn_count == 1
        assert second_meta is not None and second_meta.turn_count == 1
        await gateway.stop()

    @pytest.mark.asyncio
    async def test_clear_archives_current_and_switches(self, tmp_path):
        provider = ScriptedProvider(["答1"])
        gateway, adapter, policy, conversation = await build_stack(tmp_path, provider)
        first = policy.current_session
        await adapter.ask("第一问")

        await adapter.ask("/clear")

        assert policy.current_session != first
        first_meta = await conversation.get_meta(first)
        assert first_meta is not None and first_meta.archived is True
        await gateway.stop()
