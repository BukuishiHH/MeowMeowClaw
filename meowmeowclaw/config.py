"""项目配置加载.

约定:
    - 配置文件为项目根目录下的 ``.env``(由 ``paths.ENV_FILE`` 唯一定位)
    - 取值优先级: **系统环境变量 > .env 文件 > 代码默认值**
    - 本模块只做"解析", **不产生副作用**: 不创建目录、不打印;
      工作目录创建由装配层(``bootstrap.build_application()``)负责

用法::

    from meowmeowclaw.config import load_config
    config = load_config()
    print(config.workspace, config.max_iterations)
"""
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from dotenv import dotenv_values

from meowmeowclaw.paths import (
    DEFAULT_WORKSPACE,
    ENV_FILE,
    IDENTITY_FILE,
    resolve_memory_dir,
    resolve_workspace,
)

logger = logging.getLogger(__name__)

# 默认值
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MAX_ITERATIONS = 32
# 历史装载窗口: 50 轮 / 120k 字符; 与 token 压缩联动
# (120k 中文字符 ≈ 60k token, 可触达默认 48000 输入预算, 使 L1 摘要可用)
DEFAULT_MEMORY_MAX_TURNS = 50
DEFAULT_MEMORY_MAX_CHARS = 120_000

# 上下文 token 压缩默认值(设计见 docs/CONTEXT_COMPRESSION_DESIGN.md §8)
DEFAULT_COMPRESSION_ENABLED = True
DEFAULT_TOKEN_BUDGET = 48_000
DEFAULT_TOKENIZER = "auto"
DEFAULT_KEEP_RECENT_TURNS = 2
DEFAULT_SUMMARY_MAX_TOKENS = 768
DEFAULT_SUMMARY_TIMEOUT = 15.0
DEFAULT_HISTORY_LOG_MAX_BYTES = 2_097_152
DEFAULT_HISTORY_LOG_ORIGINAL_CHARS = 32_000

# 键别名映射
_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "model": ("model", "model_name"),
    "api_key": ("api_key", "apikey", "llm_api_key", "openai_api_key", "deepseek_api_key"),
    "base_url": ("base_url", "openai_base_url", "llm_base_url", "api_base"),
    "workspace": ("workspace", "workspace_dir", "work_dir"),
    "max_iterations": ("max_iterations", "agent_max_iterations"),
    "memory_dir": ("memory_dir", "memory_path", "session_dir"),
    "memory_max_turns": ("memory_max_turns", "history_max_turns"),
    "memory_max_chars": ("memory_max_chars", "history_max_chars"),
    "compression_enabled": ("compression_enabled",),
    "token_budget": ("token_budget",),
    "tokenizer": ("tokenizer",),
    "hf_tokenizer_path": ("hf_tokenizer_path",),
    "keep_recent_turns": ("keep_recent_turns",),
    "summary_model": ("summary_model",),
    "summary_max_tokens": ("summary_max_tokens",),
    "summary_timeout": ("summary_timeout",),
    "history_log_max_bytes": ("history_log_max_bytes",),
    "history_log_original_chars": ("history_log_original_chars",),
}

# 已废弃配置键: 人设已固定为 <项目根>/identity.md, 出现时警告并忽略
DEPRECATED_IDENTITY_KEYS = ("identity_file", "persona_file")


def read_env_file(path: Path) -> dict[str, str]:
    """读取 .env, 文件不存在返回空字典"""
    if not path.is_file():
        logger.warning("未找到配置文件 %s, 将使用环境变量与默认值", path)
        return {}
    try:
        return {k: v for k, v in dotenv_values(path).items() if v is not None}
    except Exception as exc:
        logger.warning("解析配置文件失败 %s (%r)", path, exc)
        return {}


def _merge_sources(file_values: dict[str, str]) -> dict[str, str]:
    """合并来源: 系统环境变量覆盖 .env, 键统一小写"""
    merged = {k.lower(): v for k, v in file_values.items()}
    for k, v in os.environ.items():
        if v:
            merged[k.lower()] = v
    return merged


def _get(raw: dict[str, str], *alternatives: str) -> Optional[str]:
    """按候选别名取值, 空串视为未设置"""
    for name in alternatives:
        val = raw.get(name.lower())
        if val and str(val).strip():
            return str(val).strip()
    return None


def _resolve_max_iterations(value: Optional[str]) -> int:
    """解析最大迭代次数, 非整数回退默认值"""
    if value is None:
        return DEFAULT_MAX_ITERATIONS
    try:
        num = int(str(value).strip())
        return num if num >= 0 else DEFAULT_MAX_ITERATIONS
    except (TypeError, ValueError):
        logger.warning("max_iterations 不是整数(%r), 回退默认值 %d", value, DEFAULT_MAX_ITERATIONS)
        return DEFAULT_MAX_ITERATIONS


def _resolve_positive_int(value: Optional[str], default: int, name: str) -> int:
    """解析正整数值(记忆窗口等), 非法/<=0 回退默认值."""
    if value is None:
        return default
    try:
        num = int(str(value).strip())
    except (TypeError, ValueError):
        logger.warning("%s 不是整数(%r), 回退默认值 %d", name, value, default)
        return default
    if num <= 0:
        logger.warning("%s 必须为正整数(%r), 回退默认值 %d", name, value, default)
        return default
    return num


def _resolve_bool(value: Optional[str], default: bool, name: str) -> bool:
    """解析布尔开关; 非法值回退默认值并告警."""
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    logger.warning("%s 不是合法布尔值(%r), 回退默认值 %s", name, value, default)
    return default


def _resolve_positive_float(value: Optional[str], default: float, name: str) -> float:
    """解析正浮点值(摘要超时等); 非法/<=0 回退默认值."""
    if value is None:
        return default
    try:
        num = float(str(value).strip())
    except (TypeError, ValueError):
        logger.warning("%s 不是数字(%r), 回退默认值 %s", name, value, default)
        return default
    if num <= 0:
        logger.warning("%s 必须为正数(%r), 回退默认值 %s", name, value, default)
        return default
    return num


def _resolve_token_budget(value: Optional[str]) -> int:
    """解析输入 token 预算; 非法或过小(<1000)回退默认值, 避免把正常对话全压光."""
    if value is None:
        return DEFAULT_TOKEN_BUDGET
    try:
        num = int(str(value).strip())
    except (TypeError, ValueError):
        logger.warning("token_budget 不是整数(%r), 回退默认值 %d", value, DEFAULT_TOKEN_BUDGET)
        return DEFAULT_TOKEN_BUDGET
    if num < 1000:
        logger.warning(
            "token_budget 过小(%r), 回退默认值 %d", value, DEFAULT_TOKEN_BUDGET
        )
        return DEFAULT_TOKEN_BUDGET
    return num


def _resolve_tokenizer(value: Optional[str]) -> str:
    """解析分词器模式: auto/heuristic/tiktoken[:encoding]/hf; 非法值回退默认."""
    if value is None:
        return DEFAULT_TOKENIZER
    text = str(value).strip()
    lowered = text.lower()
    if lowered in ("auto", "heuristic", "tiktoken", "hf") or lowered.startswith(
        "tiktoken:"
    ):
        return text
    logger.warning("tokenizer 取值不合法(%r), 回退默认值 %s", value, DEFAULT_TOKENIZER)
    return DEFAULT_TOKENIZER


def _warn_deprecated_identity_keys(raw: dict[str, str]) -> None:
    """检测已废弃的人设配置键: 警告并忽略(人设固定为项目根 identity.md)."""
    present = [key for key in DEPRECATED_IDENTITY_KEYS if key in raw]
    if present:
        logger.warning(
            "%s 已废弃, 人设固定为 %s, 该配置将被忽略",
            "/".join(present),
            IDENTITY_FILE,
        )


@dataclass(frozen=True)
class Settings:
    model: str = DEFAULT_MODEL
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    workspace: Path = DEFAULT_WORKSPACE
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    memory_dir: Path = DEFAULT_WORKSPACE / "memory"
    memory_max_turns: int = DEFAULT_MEMORY_MAX_TURNS
    memory_max_chars: int = DEFAULT_MEMORY_MAX_CHARS
    # 上下文 token 压缩(§8)
    compression_enabled: bool = DEFAULT_COMPRESSION_ENABLED
    token_budget: int = DEFAULT_TOKEN_BUDGET
    tokenizer: str = DEFAULT_TOKENIZER
    hf_tokenizer_path: str = ""
    keep_recent_turns: int = DEFAULT_KEEP_RECENT_TURNS
    summary_model: str = ""
    summary_max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS
    summary_timeout: float = DEFAULT_SUMMARY_TIMEOUT
    history_log_max_bytes: int = DEFAULT_HISTORY_LOG_MAX_BYTES
    history_log_original_chars: int = DEFAULT_HISTORY_LOG_ORIGINAL_CHARS
    source: str = str(ENV_FILE)

    def __repr__(self) -> str:
        masked_key = "***" if self.api_key else None
        return (
            f"Settings(model={self.model!r}, base_url={self.base_url!r}, "
            f"workspace={str(self.workspace)!r}, max_iterations={self.max_iterations}, "
            f"memory_dir={str(self.memory_dir)!r}, memory_max_turns={self.memory_max_turns}, "
            f"memory_max_chars={self.memory_max_chars}, "
            f"compression_enabled={self.compression_enabled}, token_budget={self.token_budget}, "
            f"tokenizer={self.tokenizer!r}, hf_tokenizer_path={self.hf_tokenizer_path!r}, "
            f"keep_recent_turns={self.keep_recent_turns}, summary_model={self.summary_model!r}, "
            f"summary_max_tokens={self.summary_max_tokens}, summary_timeout={self.summary_timeout}, "
            f"history_log_max_bytes={self.history_log_max_bytes}, "
            f"history_log_original_chars={self.history_log_original_chars}, "
            f"api_key={masked_key}, source={self.source!r})"
        )

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)


def load_config(env_file: Optional[Union[str, Path]] = None) -> Settings:
    """解析配置并返回 Settings; 纯函数, 每次调用重新读盘(改完 .env 立即生效)."""
    path = Path(env_file) if env_file is not None else ENV_FILE
    raw = _merge_sources(read_env_file(path))
    _warn_deprecated_identity_keys(raw)

    workspace = resolve_workspace(_get(raw, *_KEY_ALIASES["workspace"]))
    settings = Settings(
        model=_get(raw, *_KEY_ALIASES["model"]) or DEFAULT_MODEL,
        api_key=_get(raw, *_KEY_ALIASES["api_key"]) or "",
        base_url=_get(raw, *_KEY_ALIASES["base_url"]) or DEFAULT_BASE_URL,
        workspace=workspace,
        max_iterations=_resolve_max_iterations(_get(raw, *_KEY_ALIASES["max_iterations"])),
        memory_dir=resolve_memory_dir(_get(raw, *_KEY_ALIASES["memory_dir"]), workspace),
        memory_max_turns=_resolve_positive_int(
            _get(raw, *_KEY_ALIASES["memory_max_turns"]),
            DEFAULT_MEMORY_MAX_TURNS,
            "memory_max_turns",
        ),
        memory_max_chars=_resolve_positive_int(
            _get(raw, *_KEY_ALIASES["memory_max_chars"]),
            DEFAULT_MEMORY_MAX_CHARS,
            "memory_max_chars",
        ),
        compression_enabled=_resolve_bool(
            _get(raw, *_KEY_ALIASES["compression_enabled"]),
            DEFAULT_COMPRESSION_ENABLED,
            "compression_enabled",
        ),
        token_budget=_resolve_token_budget(_get(raw, *_KEY_ALIASES["token_budget"])),
        tokenizer=_resolve_tokenizer(_get(raw, *_KEY_ALIASES["tokenizer"])),
        hf_tokenizer_path=_get(raw, *_KEY_ALIASES["hf_tokenizer_path"]) or "",
        keep_recent_turns=_resolve_positive_int(
            _get(raw, *_KEY_ALIASES["keep_recent_turns"]),
            DEFAULT_KEEP_RECENT_TURNS,
            "keep_recent_turns",
        ),
        summary_model=_get(raw, *_KEY_ALIASES["summary_model"]) or "",
        summary_max_tokens=_resolve_positive_int(
            _get(raw, *_KEY_ALIASES["summary_max_tokens"]),
            DEFAULT_SUMMARY_MAX_TOKENS,
            "summary_max_tokens",
        ),
        summary_timeout=_resolve_positive_float(
            _get(raw, *_KEY_ALIASES["summary_timeout"]),
            DEFAULT_SUMMARY_TIMEOUT,
            "summary_timeout",
        ),
        history_log_max_bytes=_resolve_positive_int(
            _get(raw, *_KEY_ALIASES["history_log_max_bytes"]),
            DEFAULT_HISTORY_LOG_MAX_BYTES,
            "history_log_max_bytes",
        ),
        history_log_original_chars=_resolve_positive_int(
            _get(raw, *_KEY_ALIASES["history_log_original_chars"]),
            DEFAULT_HISTORY_LOG_ORIGINAL_CHARS,
            "history_log_original_chars",
        ),
        source=str(path),
    )

    if not settings.has_api_key:
        logger.warning("未读取到 api_key, 调用模型时会认证失败")
    return settings


# 兼容旧调用名的别名: 与 load_config 语义完全一致; 模块不再提供全局单例
load_settings = load_config
