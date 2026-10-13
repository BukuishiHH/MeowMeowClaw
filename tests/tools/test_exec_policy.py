"""子代理 exec 环境/命令策略与敏感路径策略单元测试(SA4)."""

import os

import pytest

from meowmeowclaw.tools.exec_policy import (
    SensitivePathPolicy,
    SubagentCommandPolicy,
    build_subagent_env,
)


class TestBuildSubagentEnv:
    def test_scrubs_secrets_and_keeps_allowlisted(self, tmp_path):
        base = {
            "PATH": "/usr/bin",
            "LANG": "C.UTF-8",
            "CUSTOM": "v",
            "DEEPSEEK_API_KEY": "secret-key",
            "QQ_TOKEN": "qq-token",
            "HTTPS_PROXY": "http://proxy",
            "PASSWORD": "pw",
        }

        env = build_subagent_env(
            home_dir=tmp_path / "home",
            base_env=base,
            allowlist=("CUSTOM", "DEEPSEEK_API_KEY", "QQ_TOKEN"),
        )

        assert env["PATH"] == "/usr/bin"
        assert env["LANG"] == "C.UTF-8"
        assert env["CUSTOM"] == "v"
        assert env["HOME"] == str(tmp_path / "home")
        assert env["TMPDIR"] == str(tmp_path / "home")
        # 密钥/令牌/代理即使显式 allowlist 也不放行
        assert "DEEPSEEK_API_KEY" not in env
        assert "QQ_TOKEN" not in env
        assert "HTTPS_PROXY" not in env
        assert "PASSWORD" not in env

    def test_missing_path_falls_back_to_os_defpath(self, tmp_path):
        env = build_subagent_env(home_dir=tmp_path / "home", base_env={})

        assert env["PATH"] == os.defpath
        assert env["HOME"] == str(tmp_path / "home")


class TestSubagentCommandPolicy:
    @pytest.mark.parametrize(
        "command",
        ["pytest -q", "ls -la", "python -m pytest tests/", "ruff check ."],
    )
    def test_guarded_allows_normal_commands(self, command):
        policy = SubagentCommandPolicy(mode="guarded")

        assert policy.check(command) is None

    @pytest.mark.parametrize(
        "command",
        ["nohup python app.py &", "setsid sleep 10", "disown", "sleep 1 &"],
    )
    def test_guarded_rejects_background_and_daemonize(self, command):
        policy = SubagentCommandPolicy(mode="guarded")

        reason = policy.check(command)

        assert reason is not None
        assert "后台" in reason or "守护" in reason or reason

    @pytest.mark.parametrize(
        "command",
        ["pytest -q", "pytest -q tests/test_x.py -k foo", "ruff check .", "ls -la"],
    )
    def test_allowlist_allows_configured_prefix(self, command):
        policy = SubagentCommandPolicy(
            mode="allowlist",
            allowlist=("pytest -q", "ruff check", "ls -la"),
        )

        assert policy.check(command) is None

    @pytest.mark.parametrize(
        "command",
        [
            "pytest -q ; rm -rf .",
            "pytest -q && echo hacked",
            "pytest -q | tee out",
            "echo $(date)",
            "rm -rf .",
        ],
    )
    def test_allowlist_rejects_shell_control_or_unlisted(self, command):
        policy = SubagentCommandPolicy(
            mode="allowlist", allowlist=("pytest -q", "echo")
        )

        reason = policy.check(command)

        assert reason is not None

    def test_allowlist_without_prefixes_rejects(self):
        policy = SubagentCommandPolicy(mode="allowlist", allowlist=())

        assert policy.check("pytest -q") is not None

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError):
            SubagentCommandPolicy(mode="sandbox")


class TestSensitivePathPolicy:
    def test_matches_configured_patterns_only(self, tmp_path):
        policy = SensitivePathPolicy((".env", "*.pem", "id_rsa*", ".git/config"))
        workspace = tmp_path / "ws"
        (workspace / "sub").mkdir(parents=True)
        (workspace / ".git").mkdir()
        cases = {
            workspace / ".env": True,
            workspace / "sub" / "a.key.pem": True,
            workspace / "sub" / "id_rsa": True,
            workspace / "id_rsa_backup": True,
            workspace / ".git" / "config": True,
            workspace / "normal.txt": False,
            workspace / "sub" / "main.py": False,
        }

        for path, expected in cases.items():
            assert policy.is_denied(path, workspace=workspace) is expected, path

    def test_empty_patterns_never_deny(self, tmp_path):
        policy = SensitivePathPolicy(())

        assert policy.is_denied(tmp_path / ".env", workspace=tmp_path) is False

    def test_path_outside_workspace_not_denied(self, tmp_path):
        policy = SensitivePathPolicy((".env",))

        assert (
            policy.is_denied(tmp_path / "outside" / ".env", workspace=tmp_path / "ws")
            is False
        )
