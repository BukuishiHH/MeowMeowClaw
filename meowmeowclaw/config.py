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

# 多渠道网关默认值(设计见 docs/GATEWAY_DESIGN.md §10)
DEFAULT_GATEWAY_ENABLED = False
DEFAULT_GATEWAY_BUS_MAXSIZE = 1000
DEFAULT_GATEWAY_PUBLISH_TIMEOUT = 5.0
DEFAULT_GATEWAY_SHUTDOWN_TIMEOUT = 10.0

# QQ 渠道(NapCat / OneBot 11)默认值(见 README「接入 NapCat」)
DEFAULT_QQ_OWNER_ID = ""
DEFAULT_QQ_WS_MODE = "reverse"          # reverse: 本进程监听, NapCat 连入; forward: 本进程连接 NapCat
DEFAULT_QQ_WS_HOST = "127.0.0.1"
DEFAULT_QQ_WS_PORT = 3100
DEFAULT_QQ_WS_PATH = "/onebot/v11/ws"
DEFAULT_QQ_TOKEN = ""
DEFAULT_QQ_ACTION_TIMEOUT = 10.0
DEFAULT_QQ_MESSAGE_CHUNK_CHARS = 3000

# QQ 流式呈现默认值(设计见 docs/STREAMING_DESIGN.md §6)
DEFAULT_QQ_STREAM_MODE = "typing"          # off | typing | block(S5 实现)
DEFAULT_QQ_TYPING_INDICATOR = True
DEFAULT_QQ_TYPING_REFRESH_SECONDS = 5.0

# 流式输出默认值(设计见 docs/STREAMING_DESIGN.md §6)
DEFAULT_STREAM_ENABLED = False
DEFAULT_CLI_STREAM = True
DEFAULT_STREAM_PREAMBLE_GRACE_MS = 1500
DEFAULT_STREAM_PREAMBLE_GRACE_CHARS = 40
DEFAULT_STREAM_SUPPRESS_TOOL_PREAMBLE = True

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
    "gateway_enabled": ("gateway_enabled",),
    "gateway_bus_maxsize": ("gateway_bus_maxsize",),
    "gateway_publish_timeout": ("gateway_publish_timeout",),
    "gateway_shutdown_timeout": ("gateway_shutdown_timeout",),
    "qq_owner_id": ("qq_owner_id", "qq_owner", "owner_qq"),
    "qq_ws_mode": ("qq_ws_mode", "napcat_ws_mode"),
    "qq_ws_host": ("qq_ws_host", "napcat_ws_host"),
    "qq_ws_port": ("qq_ws_port", "napcat_ws_port"),
    "qq_ws_path": ("qq_ws_path", "napcat_ws_path"),
    "qq_token": ("qq_token", "napcat_token"),
    "qq_action_timeout": ("qq_action_timeout", "qq_api_timeout"),
    "qq_message_chunk_chars": ("qq_message_chunk_chars", "qq_max_message_chars"),
    "qq_stream_mode": ("qq_stream_mode", "qq_stream"),
    "qq_typing_indicator": ("qq_typing_indicator", "qq_typing"),
    "qq_typing_refresh_seconds": ("qq_typing_refresh_seconds", "qq_typing_interval"),
    "stream_enabled": ("stream_enabled",),
    "cli_stream": ("cli_stream",),
    "stream_preamble_grace_ms": ("stream_preamble_grace_ms",),
    "stream_preamble_grace_chars": ("stream_preamble_grace_chars",),
    "stream_suppress_tool_preamble": ("stream_suppress_tool_preamble",),
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


def _resolve_non_negative_int(value: Optional[str], default: int, name: str) -> int:
    """解析非负整数(流式宽限等, 0 合法); 非法值回退默认并告警."""
    if value is None:
        return default
    try:
        num = int(str(value).strip())
    except (TypeError, ValueError):
        logger.warning("%s 不是整数(%r), 回退默认值 %d", name, value, default)
        return default
    if num < 0:
        logger.warning("%s 不能为负数(%r), 回退默认值 %d", name, value, default)
        return default
    return num


def _resolve_qq_stream_mode(value: Optional[str]) -> str:
    """解析 QQ 流式呈现档位: off/typing/block; 非法或未实现的 draft 回退默认并告警."""
    if value is None:
        return DEFAULT_QQ_STREAM_MODE
    text = str(value).strip().lower()
    if text in ("off", "typing", "block"):
        return text
    logger.warning(
        "qq_stream_mode 取值不支持(%r), 回退默认值 %s(draft 档位按设计不实现)",
        value,
        DEFAULT_QQ_STREAM_MODE,
    )
    return DEFAULT_QQ_STREAM_MODE


def _resolve_ws_mode(value: Optional[str]) -> str:
    """解析 OneBot11 连接方向: reverse(监听)/forward(主动连); 非法值回退默认."""
    if value is None:
        return DEFAULT_QQ_WS_MODE
    text = str(value).strip().lower()
    if text in ("reverse", "forward"):
        return text
    logger.warning("qq_ws_mode 取值不合法(%r), 回退默认值 %s", value, DEFAULT_QQ_WS_MODE)
    return DEFAULT_QQ_WS_MODE


def _resolve_port(value: Optional[str], default: int, name: str) -> int:
    """解析端口(1-65535); 非法值回退默认并告警."""
    if value is None:
        return default
    try:
        num = int(str(value).strip())
    except (TypeError, ValueError):
        logger.warning("%s 不是整数(%r), 回退默认值 %d", name, value, default)
        return default
    if not 1 <= num <= 65535:
        logger.warning("%s 不在 1-65535 范围内(%r), 回退默认值 %d", name, value, default)
        return default
    return num


def _resolve_ws_path(value: Optional[str]) -> str:
    """解析 WS 路径: 空白回退默认, 缺少前导斜杠时补齐."""
    if value is None:
        return DEFAULT_QQ_WS_PATH
    text = str(value).strip()
    if not text:
        return DEFAULT_QQ_WS_PATH
    return text if text.startswith("/") else f"/{text}"


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
    # 多渠道网关(§10)
    gateway_enabled: bool = DEFAULT_GATEWAY_ENABLED
    gateway_bus_maxsize: int = DEFAULT_GATEWAY_BUS_MAXSIZE
    gateway_publish_timeout: float = DEFAULT_GATEWAY_PUBLISH_TIMEOUT
    gateway_shutdown_timeout: float = DEFAULT_GATEWAY_SHUTDOWN_TIMEOUT
    # QQ 渠道(NapCat / OneBot 11)
    qq_owner_id: str = DEFAULT_QQ_OWNER_ID
    qq_ws_mode: str = DEFAULT_QQ_WS_MODE
    qq_ws_host: str = DEFAULT_QQ_WS_HOST
    qq_ws_port: int = DEFAULT_QQ_WS_PORT
    qq_ws_path: str = DEFAULT_QQ_WS_PATH
    qq_token: str = DEFAULT_QQ_TOKEN
    qq_action_timeout: float = DEFAULT_QQ_ACTION_TIMEOUT
    qq_message_chunk_chars: int = DEFAULT_QQ_MESSAGE_CHUNK_CHARS
    # QQ 流式呈现(§6)
    qq_stream_mode: str = DEFAULT_QQ_STREAM_MODE
    qq_typing_indicator: bool = DEFAULT_QQ_TYPING_INDICATOR
    qq_typing_refresh_seconds: float = DEFAULT_QQ_TYPING_REFRESH_SECONDS
    # 流式输出(§6)
    stream_enabled: bool = DEFAULT_STREAM_ENABLED
    cli_stream: bool = DEFAULT_CLI_STREAM
    stream_preamble_grace_ms: int = DEFAULT_STREAM_PREAMBLE_GRACE_MS
    stream_preamble_grace_chars: int = DEFAULT_STREAM_PREAMBLE_GRACE_CHARS
    stream_suppress_tool_preamble: bool = DEFAULT_STREAM_SUPPRESS_TOOL_PREAMBLE
    source: str = str(ENV_FILE)

    def __repr__(self) -> str:
        masked_key = "***" if self.api_key else None
        masked_qq_token = "***" if self.qq_token else None
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
            f"gateway_enabled={self.gateway_enabled}, "
            f"gateway_bus_maxsize={self.gateway_bus_maxsize}, "
            f"gateway_publish_timeout={self.gateway_publish_timeout}, "
            f"gateway_shutdown_timeout={self.gateway_shutdown_timeout}, "
            f"qq_owner_id={self.qq_owner_id!r}, qq_ws_mode={self.qq_ws_mode!r}, "
            f"qq_ws_host={self.qq_ws_host!r}, qq_ws_port={self.qq_ws_port}, "
            f"qq_ws_path={self.qq_ws_path!r}, qq_token={masked_qq_token}, "
            f"qq_action_timeout={self.qq_action_timeout}, "
            f"qq_message_chunk_chars={self.qq_message_chunk_chars}, "
            f"qq_stream_mode={self.qq_stream_mode!r}, "
            f"qq_typing_indicator={self.qq_typing_indicator}, "
            f"qq_typing_refresh_seconds={self.qq_typing_refresh_seconds}, "
            f"stream_enabled={self.stream_enabled}, cli_stream={self.cli_stream}, "
            f"stream_preamble_grace_ms={self.stream_preamble_grace_ms}, "
            f"stream_preamble_grace_chars={self.stream_preamble_grace_chars}, "
            f"stream_suppress_tool_preamble={self.stream_suppress_tool_preamble}, "
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
        gateway_enabled=_resolve_bool(
            _get(raw, *_KEY_ALIASES["gateway_enabled"]),
            DEFAULT_GATEWAY_ENABLED,
            "gateway_enabled",
        ),
        gateway_bus_maxsize=_resolve_positive_int(
            _get(raw, *_KEY_ALIASES["gateway_bus_maxsize"]),
            DEFAULT_GATEWAY_BUS_MAXSIZE,
            "gateway_bus_maxsize",
        ),
        gateway_publish_timeout=_resolve_positive_float(
            _get(raw, *_KEY_ALIASES["gateway_publish_timeout"]),
            DEFAULT_GATEWAY_PUBLISH_TIMEOUT,
            "gateway_publish_timeout",
        ),
        gateway_shutdown_timeout=_resolve_positive_float(
            _get(raw, *_KEY_ALIASES["gateway_shutdown_timeout"]),
            DEFAULT_GATEWAY_SHUTDOWN_TIMEOUT,
            "gateway_shutdown_timeout",
        ),
        qq_owner_id=_get(raw, *_KEY_ALIASES["qq_owner_id"]) or "",
        qq_ws_mode=_resolve_ws_mode(_get(raw, *_KEY_ALIASES["qq_ws_mode"])),
        qq_ws_host=_get(raw, *_KEY_ALIASES["qq_ws_host"]) or DEFAULT_QQ_WS_HOST,
        qq_ws_port=_resolve_port(
            _get(raw, *_KEY_ALIASES["qq_ws_port"]),
            DEFAULT_QQ_WS_PORT,
            "qq_ws_port",
        ),
        qq_ws_path=_resolve_ws_path(_get(raw, *_KEY_ALIASES["qq_ws_path"])),
        qq_token=_get(raw, *_KEY_ALIASES["qq_token"]) or "",
        qq_action_timeout=_resolve_positive_float(
            _get(raw, *_KEY_ALIASES["qq_action_timeout"]),
            DEFAULT_QQ_ACTION_TIMEOUT,
            "qq_action_timeout",
        ),
        qq_message_chunk_chars=_resolve_positive_int(
            _get(raw, *_KEY_ALIASES["qq_message_chunk_chars"]),
            DEFAULT_QQ_MESSAGE_CHUNK_CHARS,
            "qq_message_chunk_chars",
        ),
        qq_stream_mode=_resolve_qq_stream_mode(_get(raw, *_KEY_ALIASES["qq_stream_mode"])),
        qq_typing_indicator=_resolve_bool(
            _get(raw, *_KEY_ALIASES["qq_typing_indicator"]),
            DEFAULT_QQ_TYPING_INDICATOR,
            "qq_typing_indicator",
        ),
        qq_typing_refresh_seconds=_resolve_positive_float(
            _get(raw, *_KEY_ALIASES["qq_typing_refresh_seconds"]),
            DEFAULT_QQ_TYPING_REFRESH_SECONDS,
            "qq_typing_refresh_seconds",
        ),
        stream_enabled=_resolve_bool(
            _get(raw, *_KEY_ALIASES["stream_enabled"]),
            DEFAULT_STREAM_ENABLED,
            "stream_enabled",
        ),
        cli_stream=_resolve_bool(
            _get(raw, *_KEY_ALIASES["cli_stream"]),
            DEFAULT_CLI_STREAM,
            "cli_stream",
        ),
        stream_preamble_grace_ms=_resolve_non_negative_int(
            _get(raw, *_KEY_ALIASES["stream_preamble_grace_ms"]),
            DEFAULT_STREAM_PREAMBLE_GRACE_MS,
            "stream_preamble_grace_ms",
        ),
        stream_preamble_grace_chars=_resolve_non_negative_int(
            _get(raw, *_KEY_ALIASES["stream_preamble_grace_chars"]),
            DEFAULT_STREAM_PREAMBLE_GRACE_CHARS,
            "stream_preamble_grace_chars",
        ),
        stream_suppress_tool_preamble=_resolve_bool(
            _get(raw, *_KEY_ALIASES["stream_suppress_tool_preamble"]),
            DEFAULT_STREAM_SUPPRESS_TOOL_PREAMBLE,
            "stream_suppress_tool_preamble",
        ),
        source=str(path),
    )

    if not settings.has_api_key:
        logger.warning("未读取到 api_key, 调用模型时会认证失败")
    return settings


# 兼容旧调用名的别名: 与 load_config 语义完全一致; 模块不再提供全局单例
load_settings = load_config
