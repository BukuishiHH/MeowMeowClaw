"""``meowmeowclaw/qq.py`` 入口测试: 配置校验、--check 输出、启动与优雅关闭.

``run_qq`` 用真实 ``build_application``(临时 .env + 隔离 workspace) +
真实 ``OneBot11Transport``(随机端口) 验证进程生命周期; 不连接真实 NapCat、不调用模型。
"""

import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

import meowmeowclaw.qq as qq_entry
from meowmeowclaw.bootstrap import ConfigError, build_application
from meowmeowclaw.channels.onebot11 import OneBot11Transport
from meowmeowclaw.config import Settings
from meowmeowclaw.qq import build_transport, main, qq_config_problems, run_qq


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def write_env(tmp_path: Path, content: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(content, encoding="utf-8")
    return path


class TestQqConfigProblems:
    def test_defaults_report_gateway_and_owner(self):
        problems = qq_config_problems(Settings())

        assert len(problems) == 2
        assert any("gateway_enabled" in item for item in problems)
        assert any("qq_owner_id" in item for item in problems)

    def test_ready_config_has_no_problems(self):
        settings = Settings(gateway_enabled=True, qq_owner_id="2328428288")

        assert qq_config_problems(settings) == []

    def test_non_numeric_owner_is_rejected(self):
        settings = Settings(gateway_enabled=True, qq_owner_id="meow")

        problems = qq_config_problems(settings)
        assert len(problems) == 1
        assert "纯数字" in problems[0]


class TestBuildTransport:
    def test_maps_all_settings(self):
        settings = Settings(
            qq_ws_mode="forward",
            qq_ws_host="10.0.0.9",
            qq_ws_port=3001,
            qq_ws_path="/onebot/v11/ws",
            qq_token="tok",
            qq_action_timeout=3.0,
            qq_message_chunk_chars=500,
        )

        transport = build_transport(settings)

        assert transport.mode == "forward"
        assert transport.host == "10.0.0.9"
        assert transport.port == 3001
        assert transport.path == "/onebot/v11/ws"
        assert transport.token == "tok"
        assert transport.action_timeout == 3.0
        assert transport.max_text_chars == 500


class TestMainCheck:
    def test_check_prints_resolved_config_and_masks_token(self, monkeypatch, capsys):
        settings = Settings(
            api_key="sk-test",
            gateway_enabled=True,
            qq_owner_id="2328428288",
            qq_token="meowclaw-3100",
        )
        monkeypatch.setattr(qq_entry, "build_application", lambda env_file=None: SimpleNamespace(config=settings))

        code = main(["--check"])
        out = capsys.readouterr().out

        assert code == 0
        assert "ws://127.0.0.1:3100/onebot/v11/ws" in out
        assert "2328428288" in out
        assert "meowclaw-3100" not in out  # token 只显示掩码

    def test_refuses_to_start_without_gateway(self, monkeypatch, capsys):
        monkeypatch.setattr(
            qq_entry, "build_application", lambda env_file=None: SimpleNamespace(config=Settings())
        )

        code = main([])
        out = capsys.readouterr().out

        assert code == 2
        assert "gateway_enabled" in out
        assert "qq_owner_id" in out

    def test_config_error_returns_exit_code_1(self, monkeypatch, capsys):
        def boom(env_file=None):
            raise ConfigError("未读取到 api_key")

        monkeypatch.setattr(qq_entry, "build_application", boom)

        code = main([])
        out = capsys.readouterr().out

        assert code == 1
        assert "启动失败" in out


class TestRunQqLifecycle:
    @pytest.mark.asyncio
    async def test_starts_gateway_and_stops_on_signal_event(self, tmp_path):
        port = free_port()
        env = write_env(
            tmp_path,
            "model=stub-model\n"
            "api_key=sk-test\n"
            "base_url=http://127.0.0.1:9/v1\n"
            f"workspace={tmp_path / 'ws'}\n"
            f"memory_dir={tmp_path / 'memory'}\n"
            "compression_enabled=false\n"
            "gateway_enabled=true\n"
            "qq_owner_id=2328428288\n"
            "qq_ws_mode=reverse\n"
            "qq_ws_host=127.0.0.1\n"
            f"qq_ws_port={port}\n"
            "qq_ws_path=/onebot/v11/ws\n"
            "qq_token=meowclaw-test\n",
        )
        app = build_application(env)
        assert app.gateway is not None and app.config.qq_owner_id == "2328428288"

        transport = OneBot11Transport(
            mode="reverse", port=port, path="/onebot/v11/ws", token="meowclaw-test"
        )
        stop_event = asyncio.Event()
        task = asyncio.create_task(run_qq(app, transport=transport, stop_event=stop_event))
        try:
            assert await wait_until(lambda: transport.accepting and app.gateway is not None and app.gateway.started)

            # 模拟 Ctrl+C / SIGTERM
            stop_event.set()
            await asyncio.wait_for(task, timeout=10)
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        assert transport.accepting is False
        assert transport.connected is False
        assert app.gateway is not None and app.gateway.started is False
