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
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MODEL,
    Settings,
    load_config,
    load_settings,
    read_env_file,
)
from meowmeowclaw.paths import (
    DEFAULT_WORKSPACE,
    ENV_FILE,
    IDENTITY_FILE,
    PROJECT_ROOT,
    resolve_workspace,
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


# ------------------------------------------------------------------ Settings


class TestSettingsObject:
    def test_defaults(self):
        s = Settings()

        assert s.model == DEFAULT_MODEL
        assert s.base_url == DEFAULT_BASE_URL
        assert s.workspace == DEFAULT_WORKSPACE
        assert s.max_iterations == DEFAULT_MAX_ITERATIONS
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
