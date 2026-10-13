"""GM1 配置解析单测(``meowmeowclaw_mcp_git/config.py``, 对照 GIT_MCP_DESIGN §3.1)."""

from __future__ import annotations

import os

import pytest

from meowmeowclaw_mcp_git.config import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_PROTECTED_BRANCHES,
    DEFAULT_TIMEOUT_SECONDS,
    load_config,
)
from meowmeowclaw_mcp_git.errors import ConfigError


class TestDefaults:
    def test_defaults_single_root_is_cwd(self):
        config = load_config([], environ={})

        assert config.roots == (os.path.abspath(os.getcwd()),)
        assert config.repos == ()
        assert config.read_only is True
        assert config.allow_remote is False
        assert config.allow_destructive is False
        assert config.protected_branches == DEFAULT_PROTECTED_BRANCHES
        assert config.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
        assert config.max_output_bytes == DEFAULT_MAX_OUTPUT_BYTES
        assert config.audit_log == ""
        assert config.lock_dir == ""
        assert config.log_level == "INFO"

    def test_defaults_read_only_for_tooling(self):
        config = load_config([], environ={})

        assert config.read_only is True
        assert config.allow_remote is False
        assert config.allow_destructive is False

    def test_describe_contains_key_flags_but_no_secret(self):
        config = load_config(["--root", "/tmp/app", "--read-only=false"], environ={})

        text = config.describe()
        assert "/tmp/app" in text
        assert "read_only=False" in text
        assert "max_output_bytes=131072" in text


class TestCliParsing:
    def test_root_is_absolutized(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = load_config(["--root", "sub"], environ={})

        assert config.roots == (str(tmp_path / "sub"),)

    def test_roots_use_platform_separator(self, tmp_path):
        first, second = tmp_path / "a", tmp_path / "b"
        config = load_config(["--roots", f"{first}{os.pathsep}{second}"], environ={})

        assert config.roots == (str(first), str(second))

    def test_repos_parse_alias_pairs(self, tmp_path):
        app = tmp_path / "app"
        config = load_config(["--repos", f"app={app},lib={tmp_path / 'lib'}"], environ={})

        assert config.repos == (("app", str(app)), ("lib", str(tmp_path / "lib")))

    def test_repos_only_mode_uses_repo_paths_as_roots(self, tmp_path):
        app = tmp_path / "app"
        config = load_config(["--repos", f"app={app}"], environ={})

        assert config.roots == (str(app),)

    def test_read_only_accepts_flag_without_value(self):
        assert load_config(["--read-only"], environ={}).read_only is True
        assert load_config(["--read-only=true"], environ={}).read_only is True
        assert load_config(["--read-only=false"], environ={}).read_only is False

    @pytest.mark.parametrize("raw", ["0", "no", "OFF", "False"])
    def test_false_forms(self, raw):
        config = load_config(["--read-only", raw], environ={})

        assert config.read_only is False

    @pytest.mark.parametrize("raw", ["1", "yes", "ON", "True"])
    def test_true_forms(self, raw):
        config = load_config([f"--read-only={raw}"], environ={})

        assert config.read_only is True

    def test_remote_and_destructive_flags(self):
        config = load_config(["--allow-remote", "--allow-destructive=false"], environ={})

        assert config.allow_remote is True
        assert config.allow_destructive is False

    def test_protected_branches_custom_and_empty(self):
        custom = load_config(["--protected-branches", "main, release, "], environ={})
        empty = load_config(["--protected-branches", ""], environ={})

        assert custom.protected_branches == ("main", "release")
        assert empty.protected_branches == ()

    def test_timeout_and_output_limit(self):
        config = load_config(["--timeout", "1.5", "--max-output-bytes", "2048"], environ={})

        assert config.timeout_seconds == 1.5
        assert config.max_output_bytes == 2048

    def test_audit_and_lock_keys_accepted_for_later_milestones(self, tmp_path):
        config = load_config(
            ["--audit-log", str(tmp_path / "audit.jsonl"), "--lock-dir", str(tmp_path / "locks")],
            environ={},
        )

        assert config.audit_log == str(tmp_path / "audit.jsonl")
        assert config.lock_dir == str(tmp_path / "locks")

    def test_log_level_normalized(self):
        assert load_config(["--log-level", "debug"], environ={}).log_level == "DEBUG"


class TestEnvParsing:
    def test_env_used_when_cli_absent(self, tmp_path):
        config = load_config([], environ={"MCP_GIT_ROOT": str(tmp_path), "MCP_GIT_READ_ONLY": "false"})

        assert config.roots == (str(tmp_path),)
        assert config.read_only is False

    def test_cli_overrides_env(self, tmp_path):
        config = load_config(
            ["--root", str(tmp_path / "cli")],
            environ={"MCP_GIT_ROOT": str(tmp_path / "env"), "MCP_GIT_TIMEOUT": "5"},
        )

        assert config.roots == (str(tmp_path / "cli"),)
        assert config.timeout_seconds == 5

    def test_env_repos_and_protected_branches(self, tmp_path):
        config = load_config(
            [],
            environ={
                "MCP_GIT_REPOS": f"app={tmp_path / 'app'}",
                "MCP_GIT_PROTECTED_BRANCHES": "trunk",
                "MCP_GIT_LOG_LEVEL": "warning",
            },
        )

        assert config.repos == (("app", str(tmp_path / "app")),)
        assert config.protected_branches == ("trunk",)
        assert config.log_level == "WARNING"

    def test_env_read_only_invalid(self):
        with pytest.raises(ConfigError):
            load_config([], environ={"MCP_GIT_READ_ONLY": "maybe"})


class TestInvalidConfig:
    @pytest.mark.parametrize(
        "argv",
        [
            ["--read-only=maybe"],
            ["--timeout", "0"],
            ["--timeout", "abc"],
            ["--max-output-bytes", "-1"],
            ["--max-output-bytes", "1.5"],
            ["--log-level", "verbose"],
            ["--repos", "app"],
            ["--repos", "a=/x,a=/y"],
            ["--unknown-flag"],
        ],
    )
    def test_invalid_arguments_raise_config_error(self, argv):
        with pytest.raises(ConfigError):
            load_config(argv, environ={})

    def test_invalid_env_timeout_mentions_key(self):
        with pytest.raises(ConfigError) as excinfo:
            load_config([], environ={"MCP_GIT_TIMEOUT": "-3"})

        assert "--timeout" in str(excinfo.value)
