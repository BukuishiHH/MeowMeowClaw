"""meowmeowclaw/config.py 的 Mock 单元测试.

测试策略:
- 全部用例都在**临时 .env 文件 + 清理过的进程环境变量**下运行, 与开发者本地的 .env 完全隔离,
  保证结果可复现;
- 用 ``monkeypatch`` 伪造环境变量/当前工作目录, 覆盖"环境变量覆盖 .env""相对路径不随 cwd 漂移"等分支;
- 用 ``mock`` 注入解析失败等异常路径;
- 验收: 配置解析是纯函数 —— 不创建目录、没有全局单例、import 不产生副作用.

运行: pytest tests/test_config.py -v
"""

import logging
from pathlib import Path

import pytest

from meowmeowclaw.config import (
    DEFAULT_BASE_URL,
    DEFAULT_COMPRESSION_ENABLED,
    DEFAULT_GATEWAY_BUS_MAXSIZE,
    DEFAULT_GATEWAY_ENABLED,
    DEFAULT_GATEWAY_PUBLISH_TIMEOUT,
    DEFAULT_GATEWAY_SHUTDOWN_TIMEOUT,
    DEFAULT_HISTORY_LOG_MAX_BYTES,
    DEFAULT_HISTORY_LOG_ORIGINAL_CHARS,
    DEFAULT_KEEP_RECENT_TURNS,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MEMORY_MAX_CHARS,
    DEFAULT_MEMORY_MAX_TURNS,
    DEFAULT_MODEL,
    DEFAULT_SUMMARY_MAX_TOKENS,
    DEFAULT_SUMMARY_TIMEOUT,
    DEFAULT_TOKEN_BUDGET,
    DEFAULT_TOKENIZER,
    Settings,
    load_config,
    load_settings,
    read_env_file,
)
from meowmeowclaw.paths import (
    DEFAULT_TOKENIZER_DIR,
    DEFAULT_WORKSPACE,
    ENV_FILE,
    IDENTITY_FILE,
    PROJECT_ROOT,
    resolve_memory_dir,
    resolve_tokenizer_path,
    resolve_workspace,
    sanitize_model_name,
)

# 可能影响取值来源的进程环境变量, 测试期间一律清空
ENV_KEYS = (
    "MODEL",
    "MODEL_NAME",
    "API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "BASE_URL",
    "OPENAI_BASE_URL",
    "WORKSPACE",
    "WORK_DIR",
    "MAX_ITERATIONS",
    "IDENTITY_FILE",
    "PERSONA_FILE",
    "MEMORY_DIR",
    "MEMORY_PATH",
    "SESSION_DIR",
    "MEMORY_MAX_TURNS",
    "HISTORY_MAX_TURNS",
    "MEMORY_MAX_CHARS",
    "HISTORY_MAX_CHARS",
    "COMPRESSION_ENABLED",
    "TOKEN_BUDGET",
    "TOKENIZER",
    "HF_TOKENIZER_PATH",
    "KEEP_RECENT_TURNS",
    "SUMMARY_MODEL",
    "SUMMARY_MAX_TOKENS",
    "SUMMARY_TIMEOUT",
    "HISTORY_LOG_MAX_BYTES",
    "HISTORY_LOG_ORIGINAL_CHARS",
    "GATEWAY_ENABLED",
    "GATEWAY_BUS_MAXSIZE",
    "GATEWAY_PUBLISH_TIMEOUT",
    "GATEWAY_SHUTDOWN_TIMEOUT",
)


@pytest.fixture(autouse=True)
def clean_process_env(monkeypatch):
    """让 .env 文件成为唯一取值来源, 避免被宿主环境干扰."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def write_env(tmp_path, content: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(content, encoding="utf-8")
    return path


# ------------------------------------------------------------------ .env 读取


class TestEnvFileLoading:
    def test_reads_all_fields(self, tmp_path):
        env = write_env(
            tmp_path,
            "model=deepseek-flash\napi_key=sk-abc\nbase_url=https://api.deepseek.com\n"
            "max_iterations=7\nworkspace=my_ws\n",
        )

        s = load_settings(env)

        assert s.model == "deepseek-flash"
        assert s.api_key == "sk-abc"
        assert s.base_url == "https://api.deepseek.com"
        assert s.max_iterations == 7
        assert s.workspace == PROJECT_ROOT / "my_ws"
        assert s.source == str(env)

    def test_comments_and_blank_lines_are_ignored(self, tmp_path):
        env = write_env(tmp_path, "# 注释\n\nmodel=abc\n\n# api_key=sk-被注释掉\n")

        s = load_settings(env)

        assert s.model == "abc"
        assert s.api_key == ""

    def test_quotes_and_spaces_are_stripped(self, tmp_path):
        env = write_env(tmp_path, 'model = "  spaced-model  "\napi_key=\'sk-quoted\'\n')

        s = load_settings(env)

        assert s.model == "spaced-model"
        assert s.api_key == "sk-quoted"

    def test_missing_env_file_falls_back_to_defaults(self, tmp_path):
        s = load_settings(tmp_path / "not-exist.env")

        assert s.model == DEFAULT_MODEL
        assert s.api_key == ""
        assert s.base_url == DEFAULT_BASE_URL
        assert s.max_iterations == DEFAULT_MAX_ITERATIONS
        assert s.workspace == DEFAULT_WORKSPACE

    def test_uppercase_keys_are_accepted(self, tmp_path):
        # .env 里混用 BASE_URL / base_url 都能认
        env = write_env(tmp_path, "BASE_URL=https://example.com/v1\nMODEL=up-model\n")

        s = load_settings(env)

        assert s.base_url == "https://example.com/v1"
        assert s.model == "up-model"

    @pytest.mark.parametrize(
        ("alias_line", "expected"),
        [
            ("deepseek_api_key=sk-deepseek", "sk-deepseek"),
            ("openai_api_key=sk-openai", "sk-openai"),
            ("apikey=sk-short", "sk-short"),
        ],
    )
    def test_api_key_aliases(self, tmp_path, alias_line, expected):
        env = write_env(tmp_path, f"{alias_line}\n")

        assert load_settings(env).api_key == expected

    def test_os_env_overrides_env_file(self, tmp_path, monkeypatch):
        env = write_env(tmp_path, "max_iterations=7\nmodel=file-model\n")
        monkeypatch.setenv("MAX_ITERATIONS", "5")
        monkeypatch.setenv("MODEL", "env-model")

        s = load_settings(env)

        assert s.max_iterations == 5          # 系统环境变量优先级更高
        assert s.model == "env-model"

    def test_malformed_file_does_not_raise(self, tmp_path, monkeypatch):
        env = write_env(tmp_path, "model=x\n")
        monkeypatch.setattr(
            "meowmeowclaw.config.dotenv_values",
            lambda path: (_ for _ in ()).throw(ValueError("解析炸了")),
        )

        s = load_settings(env)  # 不抛异常, 回退默认值

        assert s.model == DEFAULT_MODEL

    def test_read_env_file_returns_empty_dict_when_missing(self, tmp_path):
        assert read_env_file(tmp_path / "nope.env") == {}


# --------------------------------------------- 已废弃的人设配置键(警告并忽略)


class TestDeprecatedIdentityKeys:
    @pytest.mark.parametrize("key", ["identity_file", "persona_file"])
    def test_deprecated_key_warns_and_is_ignored(self, tmp_path, caplog, key):
        env = write_env(tmp_path, f"{key}=meow.md\nmodel=m\n")

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.config"):
            s = load_settings(env)

        assert any("已废弃" in record.message for record in caplog.records)
        assert not hasattr(s, "identity_file")
        # 人设路径固定由装配层使用 paths.IDENTITY_FILE
        assert IDENTITY_FILE == PROJECT_ROOT / "identity.md"

    def test_no_warning_when_keys_absent(self, tmp_path, caplog):
        env = write_env(tmp_path, "model=m\n")

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.config"):
            load_settings(env)

        assert not any("已废弃" in record.message for record in caplog.records)


# --------------------------------------------- 工作目录预设(路径解析)


class TestWorkspaceResolution:
    def test_preset_is_package_sibling(self):
        assert (PROJECT_ROOT / "meowmeowclaw").parent == PROJECT_ROOT
        assert DEFAULT_WORKSPACE.parent == PROJECT_ROOT
        assert DEFAULT_WORKSPACE.name == "workspace"
        assert DEFAULT_WORKSPACE == PROJECT_ROOT / "workspace"

    @pytest.mark.parametrize("raw", [None, "", ".", "./", "   "])
    def test_default_and_dot_mean_preset(self, raw):
        assert resolve_workspace(raw) == DEFAULT_WORKSPACE

    def test_relative_path_resolves_against_project_root_not_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # 故意切走当前工作目录

        assert resolve_workspace("sub/dir") == PROJECT_ROOT / "sub" / "dir"
        assert resolve_workspace(".") == DEFAULT_WORKSPACE  # 不会变成 tmp_path

    def test_absolute_path_is_kept(self, tmp_path):
        assert resolve_workspace(str(tmp_path)) == tmp_path

    def test_tilde_is_expanded(self):
        assert str(Path("~").expanduser()) in str(resolve_workspace("~/my_ws"))

    def test_normalizes_dot_segments(self):
        assert resolve_workspace("a/../b") == PROJECT_ROOT / "b"

    def test_load_config_does_not_create_workspace(self, tmp_path):
        """配置层是纯解析: 创建目录属于装配层职责, 这里必须没有副作用."""
        target = tmp_path / "auto" / "created"
        env = write_env(tmp_path, f"workspace={target}\n")

        s = load_settings(env)

        assert s.workspace == target
        assert not target.exists()

    def test_shipped_env_resolves_to_preset_workspace(self):
        """验收: 仓库自带 .env 解析出来的工作目录, 必须是与 meowmeowclaw 同级的 workspace/."""
        s = load_settings()

        assert s.workspace == DEFAULT_WORKSPACE, (
            f"配置文件里的工作目录应预设为与 meowmeowclaw 同级的 {DEFAULT_WORKSPACE}, "
            f"实际为 {s.workspace}"
        )

    def test_default_env_file_points_to_project_root(self):
        assert ENV_FILE == PROJECT_ROOT / ".env"


# ------------------------------------------------------------ 记忆配置


class TestMemorySettings:
    def test_default_memory_dir_is_workspace_memory(self, tmp_path):
        env = write_env(tmp_path, f"workspace={tmp_path / 'ws'}\n")

        s = load_settings(env)

        assert s.memory_dir == (tmp_path / "ws" / "memory")

    def test_absolute_memory_dir_is_kept(self, tmp_path):
        target = tmp_path / "custom-memory"
        env = write_env(tmp_path, f"memory_dir={target}\n")

        assert load_settings(env).memory_dir == target

    def test_relative_memory_dir_resolves_against_project_root(self, tmp_path):
        env = write_env(tmp_path, "memory_dir=custom-memory\n")

        assert load_settings(env).memory_dir == PROJECT_ROOT / "custom-memory"

    def test_memory_window_defaults(self, tmp_path):
        env = write_env(tmp_path, "model=m\n")

        s = load_settings(env)

        assert s.memory_max_turns == DEFAULT_MEMORY_MAX_TURNS
        assert s.memory_max_chars == DEFAULT_MEMORY_MAX_CHARS

    def test_memory_window_values(self, tmp_path):
        env = write_env(tmp_path, "memory_max_turns=5\nmemory_max_chars=1234\n")

        s = load_settings(env)

        assert s.memory_max_turns == 5
        assert s.memory_max_chars == 1234

    @pytest.mark.parametrize("raw", ["abc", "0", "-1", "3.5"])
    def test_invalid_memory_window_falls_back(self, tmp_path, caplog, raw):
        env = write_env(tmp_path, f"memory_max_turns={raw}\nmemory_max_chars={raw}\n")

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.config"):
            s = load_settings(env)

        assert s.memory_max_turns == DEFAULT_MEMORY_MAX_TURNS
        assert s.memory_max_chars == DEFAULT_MEMORY_MAX_CHARS
        assert any("memory_max" in record.message for record in caplog.records)

    def test_resolve_memory_dir_helper(self, tmp_path):
        assert resolve_memory_dir(None, tmp_path) == tmp_path / "memory"
        assert resolve_memory_dir("~", tmp_path) == Path("~").expanduser()


# ------------------------------------------------------------ max_iterations


class TestMaxIterations:
    @pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("0", 0), ("64", 64)])
    def test_valid_values(self, tmp_path, raw, expected):
        env = write_env(tmp_path, f"max_iterations={raw}\n")

        assert load_settings(env).max_iterations == expected

    @pytest.mark.parametrize("raw", ["max_iterations", "abc", "3.5", "-1", " "])
    def test_invalid_values_fall_back_with_warning(self, tmp_path, raw, caplog):
        env = write_env(tmp_path, f"max_iterations={raw}\n")

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.config"):
            s = load_settings(env)

        assert s.max_iterations == DEFAULT_MAX_ITERATIONS

    def test_missing_key_uses_default(self, tmp_path):
        env = write_env(tmp_path, "model=m\n")

        assert load_settings(env).max_iterations == DEFAULT_MAX_ITERATIONS


# ---------------------------------------------------------------- api_key 兼容


class TestApiKeyHandling:
    def test_plain_value(self, tmp_path):
        env = write_env(tmp_path, "api_key=sk-plain\n")

        assert load_settings(env).api_key == "sk-plain"

    def test_placeholder_value_passes_through_unchanged(self, tmp_path):
        # 配置层不校验密钥格式: 直接抄 .env.example 忘了替换时会原样透传,
        # 直到真正调用模型才会暴露认证失败
        env = write_env(tmp_path, "api_key=your_api_key\n")

        assert load_settings(env).api_key == "your_api_key"
        assert load_settings(env).has_api_key is True

    def test_dotenv_expands_variable_reference(self, tmp_path, monkeypatch):
        # 推荐写法: api_key=${DEEPSEEK_API_KEY}
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
        env = write_env(tmp_path, "api_key=${DEEPSEEK_API_KEY}\n")

        assert load_settings(env).api_key == "sk-from-env"

    def test_missing_api_key_warns_but_does_not_raise(self, tmp_path, caplog):
        env = write_env(tmp_path, "model=m\n")

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.config"):
            s = load_settings(env)

        assert s.has_api_key is False
        assert any("api_key" in r.message for r in caplog.records)


# ------------------------------------------------------------ token 压缩配置


class TestCompressionSettings:
    def test_defaults(self, tmp_path):
        s = load_settings(write_env(tmp_path, "model=m\n"))

        assert s.compression_enabled is DEFAULT_COMPRESSION_ENABLED
        assert s.token_budget == DEFAULT_TOKEN_BUDGET
        assert s.tokenizer == DEFAULT_TOKENIZER
        assert s.hf_tokenizer_path == ""
        assert s.keep_recent_turns == DEFAULT_KEEP_RECENT_TURNS
        assert s.summary_model == ""
        assert s.summary_max_tokens == DEFAULT_SUMMARY_MAX_TOKENS
        assert s.summary_timeout == DEFAULT_SUMMARY_TIMEOUT
        assert s.history_log_max_bytes == DEFAULT_HISTORY_LOG_MAX_BYTES
        assert s.history_log_original_chars == DEFAULT_HISTORY_LOG_ORIGINAL_CHARS

    def test_values(self, tmp_path):
        env = write_env(
            tmp_path,
            "compression_enabled=false\n"
            "token_budget=32000\n"
            "tokenizer=tiktoken:o200k_base\n"
            "hf_tokenizer_path=tokenizers/my-model/tokenizer.json\n"
            "keep_recent_turns=3\n"
            "summary_model=deepseek-chat\n"
            "summary_max_tokens=512\n"
            "summary_timeout=8.5\n"
            "history_log_max_bytes=1048576\n"
            "history_log_original_chars=16000\n",
        )

        s = load_settings(env)

        assert s.compression_enabled is False
        assert s.token_budget == 32000
        assert s.tokenizer == "tiktoken:o200k_base"
        assert s.hf_tokenizer_path == "tokenizers/my-model/tokenizer.json"
        assert s.keep_recent_turns == 3
        assert s.summary_model == "deepseek-chat"
        assert s.summary_max_tokens == 512
        assert s.summary_timeout == 8.5
        assert s.history_log_max_bytes == 1048576
        assert s.history_log_original_chars == 16000

    @pytest.mark.parametrize("raw", ["true", "1", "YES", "On"])
    def test_bool_true_values(self, tmp_path, raw):
        assert load_settings(write_env(tmp_path, f"compression_enabled={raw}\n")).compression_enabled is True

    @pytest.mark.parametrize("raw", ["false", "0", "NO", "off"])
    def test_bool_false_values(self, tmp_path, raw):
        assert load_settings(write_env(tmp_path, f"compression_enabled={raw}\n")).compression_enabled is False

    @pytest.mark.parametrize("raw", ["abc", "3.5", "0", "-1", "999"])
    def test_invalid_token_budget_falls_back(self, tmp_path, caplog, raw):
        env = write_env(tmp_path, f"token_budget={raw}\n")

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.config"):
            s = load_settings(env)

        assert s.token_budget == DEFAULT_TOKEN_BUDGET
        assert any("token_budget" in record.message for record in caplog.records)

    def test_invalid_bool_falls_back(self, tmp_path, caplog):
        env = write_env(tmp_path, "compression_enabled=maybe\n")

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.config"):
            s = load_settings(env)

        assert s.compression_enabled is DEFAULT_COMPRESSION_ENABLED
        assert any("compression_enabled" in record.message for record in caplog.records)

    @pytest.mark.parametrize("raw", ["auto", "heuristic", "tiktoken", "tiktoken:cl100k_base", "hf"])
    def test_valid_tokenizer_modes(self, tmp_path, raw):
        assert load_settings(write_env(tmp_path, f"tokenizer={raw}\n")).tokenizer == raw

    def test_invalid_tokenizer_falls_back(self, tmp_path, caplog):
        env = write_env(tmp_path, "tokenizer=magic\n")

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.config"):
            s = load_settings(env)

        assert s.tokenizer == DEFAULT_TOKENIZER
        assert any("tokenizer" in record.message for record in caplog.records)

    @pytest.mark.parametrize(
        "line",
        [
            "keep_recent_turns=0\n",
            "summary_max_tokens=abc\n",
            "summary_timeout=-1\n",
            "history_log_max_bytes=0\n",
            "history_log_original_chars=xyz\n",
        ],
    )
    def test_invalid_positive_values_fall_back(self, tmp_path, caplog, line):
        env = write_env(tmp_path, line)

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.config"):
            s = load_settings(env)

        name = line.split("=")[0]
        defaults = {
            "keep_recent_turns": DEFAULT_KEEP_RECENT_TURNS,
            "summary_max_tokens": DEFAULT_SUMMARY_MAX_TOKENS,
            "summary_timeout": DEFAULT_SUMMARY_TIMEOUT,
            "history_log_max_bytes": DEFAULT_HISTORY_LOG_MAX_BYTES,
            "history_log_original_chars": DEFAULT_HISTORY_LOG_ORIGINAL_CHARS,
        }
        assert getattr(s, name) == defaults[name]
        assert any(name in record.message for record in caplog.records)


# ------------------------------------------------------------ 网关配置


class TestGatewaySettings:
    def test_defaults(self, tmp_path):
        s = load_settings(write_env(tmp_path, "model=m\n"))

        assert s.gateway_enabled is DEFAULT_GATEWAY_ENABLED
        assert s.gateway_bus_maxsize == DEFAULT_GATEWAY_BUS_MAXSIZE
        assert s.gateway_publish_timeout == DEFAULT_GATEWAY_PUBLISH_TIMEOUT
        assert s.gateway_shutdown_timeout == DEFAULT_GATEWAY_SHUTDOWN_TIMEOUT

    def test_values(self, tmp_path):
        env = write_env(
            tmp_path,
            "gateway_enabled=true\n"
            "gateway_bus_maxsize=7\n"
            "gateway_publish_timeout=0.5\n"
            "gateway_shutdown_timeout=3\n",
        )

        s = load_settings(env)

        assert s.gateway_enabled is True
        assert s.gateway_bus_maxsize == 7
        assert s.gateway_publish_timeout == 0.5
        assert s.gateway_shutdown_timeout == 3.0

    def test_invalid_bool_falls_back(self, tmp_path, caplog):
        env = write_env(tmp_path, "gateway_enabled=maybe\n")

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.config"):
            s = load_settings(env)

        assert s.gateway_enabled is DEFAULT_GATEWAY_ENABLED
        assert any("gateway_enabled" in record.message for record in caplog.records)

    @pytest.mark.parametrize(
        "line",
        [
            "gateway_bus_maxsize=0\n",
            "gateway_publish_timeout=-1\n",
            "gateway_shutdown_timeout=abc\n",
        ],
    )
    def test_invalid_positive_values_fall_back(self, tmp_path, caplog, line):
        env = write_env(tmp_path, line)

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.config"):
            s = load_settings(env)

        name = line.split("=")[0]
        defaults = {
            "gateway_bus_maxsize": DEFAULT_GATEWAY_BUS_MAXSIZE,
            "gateway_publish_timeout": DEFAULT_GATEWAY_PUBLISH_TIMEOUT,
            "gateway_shutdown_timeout": DEFAULT_GATEWAY_SHUTDOWN_TIMEOUT,
        }
        assert getattr(s, name) == defaults[name]
        assert any(name in record.message for record in caplog.records)


# --------------------------------------------------------- tokenizer 路径解析


class TestTokenizerPathResolution:
    def test_sanitize_model_name(self):
        assert sanitize_model_name("deepseek-ai/DeepSeek-V4-Flash") == (
            "deepseek-ai_DeepSeek-V4-Flash"
        )
        assert sanitize_model_name("a b:c/d") == "a_b_c_d"
        assert sanitize_model_name("") == "unknown"
        assert sanitize_model_name(None) == "unknown"

    def test_default_subdir_candidate(self, tmp_path, monkeypatch):
        monkeypatch.setattr("meowmeowclaw.paths.DEFAULT_TOKENIZER_DIR", tmp_path)
        target = tmp_path / "deepseek-ai_DeepSeek-V4-Flash" / "tokenizer.json"
        target.parent.mkdir(parents=True)
        target.write_text("{}", encoding="utf-8")

        assert resolve_tokenizer_path(None, "deepseek-ai/DeepSeek-V4-Flash") == target

    def test_default_single_file_candidate(self, tmp_path, monkeypatch):
        monkeypatch.setattr("meowmeowclaw.paths.DEFAULT_TOKENIZER_DIR", tmp_path)
        target = tmp_path / "my-model.json"
        target.write_text("{}", encoding="utf-8")

        assert resolve_tokenizer_path(None, "my-model") == target

    def test_subdir_candidate_wins(self, tmp_path, monkeypatch):
        monkeypatch.setattr("meowmeowclaw.paths.DEFAULT_TOKENIZER_DIR", tmp_path)
        subdir = tmp_path / "m" / "tokenizer.json"
        subdir.parent.mkdir(parents=True)
        subdir.write_text("{}", encoding="utf-8")
        single = tmp_path / "m.json"
        single.write_text("{}", encoding="utf-8")

        assert resolve_tokenizer_path(None, "m") == subdir

    def test_missing_candidates_return_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("meowmeowclaw.paths.DEFAULT_TOKENIZER_DIR", tmp_path)

        assert resolve_tokenizer_path(None, "m") is None
        assert resolve_tokenizer_path("", "m") is None

    def test_explicit_relative_path_resolves_against_project_root(self):
        # 用仓库内真实存在的根级文件验证"相对路径按项目根解析"的约定
        assert resolve_tokenizer_path(".env.example", "m") == PROJECT_ROOT / ".env.example"

    def test_explicit_absolute_path(self, tmp_path):
        target = tmp_path / "tokenizer.json"
        target.write_text("{}", encoding="utf-8")

        assert resolve_tokenizer_path(str(target), "m") == target

    def test_explicit_missing_path_ignores_default_candidates(self, tmp_path, monkeypatch):
        monkeypatch.setattr("meowmeowclaw.paths.DEFAULT_TOKENIZER_DIR", tmp_path)
        fallback = tmp_path / "m" / "tokenizer.json"
        fallback.parent.mkdir(parents=True)
        fallback.write_text("{}", encoding="utf-8")

        assert resolve_tokenizer_path("no-such-file.json", "m") is None

    def test_default_dir_constant_points_to_project_root(self):
        assert DEFAULT_TOKENIZER_DIR == PROJECT_ROOT / "tokenizers"


# ------------------------------------------------------------------ Settings


class TestSettingsObject:
    def test_defaults(self):
        s = Settings()

        assert s.model == DEFAULT_MODEL
        assert s.base_url == DEFAULT_BASE_URL
        assert s.workspace == DEFAULT_WORKSPACE
        assert s.max_iterations == DEFAULT_MAX_ITERATIONS
        assert s.memory_dir == DEFAULT_WORKSPACE / "memory"
        assert s.memory_max_turns == DEFAULT_MEMORY_MAX_TURNS
        assert s.memory_max_chars == DEFAULT_MEMORY_MAX_CHARS
        assert s.has_api_key is False

    def test_repr_masks_api_key(self):
        text = repr(Settings(api_key="sk-super-secret"))

        assert "sk-super-secret" not in text
        assert "***" in text

    def test_repr_without_api_key(self):
        assert "api_key=None" in repr(Settings())

    def test_is_frozen(self):
        with pytest.raises(Exception):
            Settings().model = "changed"  # type: ignore[misc]

    def test_source_records_loaded_file(self, tmp_path):
        env = write_env(tmp_path, "model=m\n")

        assert load_settings(env).source == str(env)


# ------------------------------------------------- 模块级副作用护栏


class TestNoImportSideEffects:
    def test_module_has_no_global_settings_singleton(self):
        import meowmeowclaw.config as config_module

        assert not hasattr(config_module, "settings")

    def test_load_config_is_same_as_load_settings(self):
        assert load_config is load_settings
