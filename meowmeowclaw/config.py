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
from dataclasses import dataclass, replace
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
# A+ A4/A5: 会话运行时缓存(agent + lock)的容量与空闲 TTL
DEFAULT_MEMORY_MAX_CACHED_SESSIONS = 256
DEFAULT_MEMORY_CACHE_IDLE_SECONDS = 0.0

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
# block 分段呈现默认值(保守: 防刷屏 + 防风控; S5.1 修订为"大小可控 + 收尾均分")
DEFAULT_QQ_STREAM_FIRST_CHARS = 150     # 首块触发下限(默认=target, 保证块大小均匀)
DEFAULT_QQ_STREAM_MIN_CHARS = 80        # 单块下限
DEFAULT_QQ_STREAM_TARGET_CHARS = 150    # 理想块大小
DEFAULT_QQ_STREAM_MAX_BLOCK_CHARS = 260 # 单块硬上限
DEFAULT_QQ_STREAM_MIN_INTERVAL = 2.5    # 中间气泡最小间隔(秒)
DEFAULT_QQ_STREAM_MAX_MESSAGES = 5      # 单轮总气泡上限(含收尾补发)
DEFAULT_QQ_STREAM_FLUSH_INTERVAL = 0.3  # 收尾补发块之间的间隔(秒)
# 已废弃: 旧键 qq_stream_hard_chars 现在作为 qq_stream_max_block_chars 的别名读取
DEFAULT_QQ_STREAM_HARD_CHARS = DEFAULT_QQ_STREAM_MAX_BLOCK_CHARS

# 流式输出默认值(设计见 docs/STREAMING_DESIGN.md §6)
DEFAULT_STREAM_ENABLED = False
DEFAULT_CLI_STREAM = True
DEFAULT_STREAM_PREAMBLE_GRACE_MS = 1500
DEFAULT_STREAM_PREAMBLE_GRACE_CHARS = 40
DEFAULT_STREAM_SUPPRESS_TOOL_PREAMBLE = True

# 子代理默认值(设计见 docs/SUBAGENT_DESIGN.md §3.5; SA1 只做配置解析, 默认关闭)
DEFAULT_SUBAGENT_ENABLED = False
DEFAULT_SUBAGENT_MAX_DEPTH = 3
DEFAULT_SUBAGENT_MODEL = ""
DEFAULT_SUBAGENT_MAX_ITERATIONS = 8
DEFAULT_SUBAGENT_TIMEOUT_SECONDS = 120.0
DEFAULT_SUBAGENT_MAX_CHILDREN_PER_TURN = 3
DEFAULT_SUBAGENT_MAX_TOTAL_PER_TURN = 6
DEFAULT_SUBAGENT_RESULT_MAX_CHARS = 8000
DEFAULT_SUBAGENT_TOOLS = (
    "read_file",
    "list_dir",
    "web_search",
    "web_fetch",
    "load_skill",
    "write_file",
    "exec",
)
DEFAULT_SUBAGENT_EXEC_POLICY = "guarded"
DEFAULT_SUBAGENT_EXEC_ALLOWLIST: tuple[str, ...] = ()
DEFAULT_SUBAGENT_EXEC_ENV_ALLOWLIST: tuple[str, ...] = ()
DEFAULT_SUBAGENT_WRITE_REQUIRES_READ = True
DEFAULT_SUBAGENT_SENSITIVE_PATHS = (".env", "*.pem", "*.key", "id_rsa*", ".git/config")
DEFAULT_SUBAGENT_AUDIT_ENABLED = True
DEFAULT_SUBAGENT_AUDIT_MAX_BYTES = 4_194_304
DEFAULT_WORKSPACE_MUTATION_LOCK_ENABLED = True
DEFAULT_WORKSPACE_MUTATION_LOCK_TIMEOUT = 90.0

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
    "memory_max_cached_sessions": (
        "memory_max_cached_sessions",
        "max_cached_sessions",
    ),
    "memory_cache_idle_seconds": (
        "memory_cache_idle_seconds",
        "cache_idle_seconds",
    ),
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
    "qq_stream_first_chars": ("qq_stream_first_chars",),
    "qq_stream_min_chars": ("qq_stream_min_chars",),
    "qq_stream_min_interval": ("qq_stream_min_interval",),
    "qq_stream_max_messages": ("qq_stream_max_messages",),
    "qq_stream_target_chars": ("qq_stream_target_chars",),
    "qq_stream_max_block_chars": ("qq_stream_max_block_chars",),
    "qq_stream_flush_interval": ("qq_stream_flush_interval",),
    # 已废弃(S5.1): 作为 qq_stream_max_block_chars 的别名读取
    "qq_stream_hard_chars": ("qq_stream_hard_chars",),
    "stream_enabled": ("stream_enabled",),
    "cli_stream": ("cli_stream",),
    "stream_preamble_grace_ms": ("stream_preamble_grace_ms",),
    "stream_preamble_grace_chars": ("stream_preamble_grace_chars",),
    "stream_suppress_tool_preamble": ("stream_suppress_tool_preamble",),
    # 子代理(SA1: 只解析配置, 默认关闭)
    "subagent_enabled": ("subagent_enabled",),
    "subagent_max_depth": ("subagent_max_depth", "max_subagent_depth"),
    "subagent_model": ("subagent_model", "subagent_model_name"),
    "subagent_max_iterations": ("subagent_max_iterations",),
    "subagent_timeout_seconds": ("subagent_timeout_seconds", "subagent_timeout"),
    "subagent_max_children_per_turn": ("subagent_max_children_per_turn",),
    "subagent_max_total_per_turn": ("subagent_max_total_per_turn",),
    "subagent_result_max_chars": ("subagent_result_max_chars",),
    "subagent_tools": ("subagent_tools", "subagent_tool_names"),
    "subagent_exec_policy": ("subagent_exec_policy", "subagent_exec_mode"),
    "subagent_exec_allowlist": ("subagent_exec_allowlist", "subagent_exec_allow"),
    "subagent_exec_env_allowlist": ("subagent_exec_env_allowlist",),
    "subagent_write_requires_read": ("subagent_write_requires_read",),
    "subagent_sensitive_paths": ("subagent_sensitive_paths",),
    "subagent_audit_enabled": ("subagent_audit_enabled",),
    "subagent_audit_max_bytes": ("subagent_audit_max_bytes",),
    "workspace_mutation_lock_enabled": ("workspace_mutation_lock_enabled",),
    "workspace_mutation_lock_timeout": ("workspace_mutation_lock_timeout",),
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


def _resolve_session_cache_size(value: Optional[str], default: int, name: str) -> int:
    """解析会话运行时缓存上限(A4): 任意整数合法, <=0 表示不限制; 非法回退默认."""
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        logger.warning("%s 不是整数(%r), 回退默认值 %d", name, value, default)
        return default


def _resolve_subagent_max_depth(value: Optional[str], default: int) -> int:
    """解析子代理总层数: 必须 >= 2; 非法/过小回退默认并告警."""
    if value is None:
        return default
    try:
        num = int(str(value).strip())
    except (TypeError, ValueError):
        logger.warning("subagent_max_depth 不是整数(%r), 回退默认值 %d", value, default)
        return default
    if num < 2:
        logger.warning(
            "subagent_max_depth 必须 >= 2(%r), 回退默认值 %d", value, default
        )
        return default
    return num


def _resolve_csv_tuple(
    value: Optional[str],
    default: tuple[str, ...],
    name: str,
) -> tuple[str, ...]:
    """解析逗号分隔字符串为去重 tuple; None/空值返回默认; 保持输入顺序."""
    if value is None:
        return default
    items = [item.strip() for item in str(value).split(",")]
    items = [item for item in items if item]
    if not items:
        return default
    result: list[str] = []
    for item in items:
        if item not in result:
            result.append(item)
    if result == list(default):
        return default
    return tuple(result)


def _resolve_subagent_exec_policy(value: Optional[str]) -> str:
    """解析子代理 exec 档位: guarded/allowlist; sandbox 预留未实现时回退 guarded."""
    if value is None:
        return DEFAULT_SUBAGENT_EXEC_POLICY
    text = str(value).strip().lower()
    if text in ("guarded", "allowlist"):
        return text
    if text == "sandbox":
        logger.warning(
            "subagent_exec_policy=sandbox 为预留档位(未实现), 本次回退 guarded"
        )
        return DEFAULT_SUBAGENT_EXEC_POLICY
    logger.warning(
        "subagent_exec_policy 取值不合法(%r), 回退默认值 %s",
        value,
        DEFAULT_SUBAGENT_EXEC_POLICY,
    )
    return DEFAULT_SUBAGENT_EXEC_POLICY


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


def _resolve_non_negative_float(value: Optional[str], default: float, name: str) -> float:
    """解析非负浮点(收尾间隔等, 0 合法); 非法值回退默认并告警."""
    if value is None:
        return default
    try:
        num = float(str(value).strip())
    except (TypeError, ValueError):
        logger.warning("%s 不是数字(%r), 回退默认值 %s", name, value, default)
        return default
    if num < 0:
        logger.warning("%s 不能为负数(%r), 回退默认值 %s", name, value, default)
        return default
    return num


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
    # A+ A4/A5: 会话运行时(agent + lock)缓存容量与空闲 TTL
    memory_max_cached_sessions: int = DEFAULT_MEMORY_MAX_CACHED_SESSIONS
    memory_cache_idle_seconds: float = DEFAULT_MEMORY_CACHE_IDLE_SECONDS
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
    qq_stream_first_chars: int = DEFAULT_QQ_STREAM_FIRST_CHARS
    qq_stream_min_chars: int = DEFAULT_QQ_STREAM_MIN_CHARS
    qq_stream_target_chars: int = DEFAULT_QQ_STREAM_TARGET_CHARS
    qq_stream_max_block_chars: int = DEFAULT_QQ_STREAM_MAX_BLOCK_CHARS
    qq_stream_min_interval: float = DEFAULT_QQ_STREAM_MIN_INTERVAL
    qq_stream_max_messages: int = DEFAULT_QQ_STREAM_MAX_MESSAGES
    qq_stream_flush_interval: float = DEFAULT_QQ_STREAM_FLUSH_INTERVAL
    # 已废弃: qq_stream_max_block_chars 的旧名(兼容保留, 值与之相同)
    qq_stream_hard_chars: int = DEFAULT_QQ_STREAM_HARD_CHARS
    # 流式输出(§6)
    stream_enabled: bool = DEFAULT_STREAM_ENABLED
    cli_stream: bool = DEFAULT_CLI_STREAM
    stream_preamble_grace_ms: int = DEFAULT_STREAM_PREAMBLE_GRACE_MS
    stream_preamble_grace_chars: int = DEFAULT_STREAM_PREAMBLE_GRACE_CHARS
    stream_suppress_tool_preamble: bool = DEFAULT_STREAM_SUPPRESS_TOOL_PREAMBLE
    # 子代理(SA1: 纯配置, 默认关闭; 设计见 docs/SUBAGENT_DESIGN.md §3.5)
    subagent_enabled: bool = DEFAULT_SUBAGENT_ENABLED
    subagent_max_depth: int = DEFAULT_SUBAGENT_MAX_DEPTH
    subagent_model: str = DEFAULT_SUBAGENT_MODEL
    subagent_max_iterations: int = DEFAULT_SUBAGENT_MAX_ITERATIONS
    subagent_timeout_seconds: float = DEFAULT_SUBAGENT_TIMEOUT_SECONDS
    subagent_max_children_per_turn: int = DEFAULT_SUBAGENT_MAX_CHILDREN_PER_TURN
    subagent_max_total_per_turn: int = DEFAULT_SUBAGENT_MAX_TOTAL_PER_TURN
    subagent_result_max_chars: int = DEFAULT_SUBAGENT_RESULT_MAX_CHARS
    subagent_tools: tuple[str, ...] = DEFAULT_SUBAGENT_TOOLS
    subagent_exec_policy: str = DEFAULT_SUBAGENT_EXEC_POLICY
    subagent_exec_allowlist: tuple[str, ...] = DEFAULT_SUBAGENT_EXEC_ALLOWLIST
    subagent_exec_env_allowlist: tuple[str, ...] = DEFAULT_SUBAGENT_EXEC_ENV_ALLOWLIST
    subagent_write_requires_read: bool = DEFAULT_SUBAGENT_WRITE_REQUIRES_READ
    subagent_sensitive_paths: tuple[str, ...] = DEFAULT_SUBAGENT_SENSITIVE_PATHS
    subagent_audit_enabled: bool = DEFAULT_SUBAGENT_AUDIT_ENABLED
    subagent_audit_max_bytes: int = DEFAULT_SUBAGENT_AUDIT_MAX_BYTES
    workspace_mutation_lock_enabled: bool = DEFAULT_WORKSPACE_MUTATION_LOCK_ENABLED
    workspace_mutation_lock_timeout: float = DEFAULT_WORKSPACE_MUTATION_LOCK_TIMEOUT
    source: str = str(ENV_FILE)

    def __repr__(self) -> str:
        masked_key = "***" if self.api_key else None
        masked_qq_token = "***" if self.qq_token else None
        return (
            f"Settings(model={self.model!r}, base_url={self.base_url!r}, "
            f"workspace={str(self.workspace)!r}, max_iterations={self.max_iterations}, "
            f"memory_dir={str(self.memory_dir)!r}, memory_max_turns={self.memory_max_turns}, "
            f"memory_max_chars={self.memory_max_chars}, "
            f"memory_max_cached_sessions={self.memory_max_cached_sessions}, "
            f"memory_cache_idle_seconds={self.memory_cache_idle_seconds}, "
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
            f"qq_stream_first_chars={self.qq_stream_first_chars}, "
            f"qq_stream_min_chars={self.qq_stream_min_chars}, "
            f"qq_stream_min_interval={self.qq_stream_min_interval}, "
            f"qq_stream_target_chars={self.qq_stream_target_chars}, "
            f"qq_stream_max_block_chars={self.qq_stream_max_block_chars}, "
            f"qq_stream_max_messages={self.qq_stream_max_messages}, "
            f"qq_stream_flush_interval={self.qq_stream_flush_interval}, "
            f"stream_enabled={self.stream_enabled}, cli_stream={self.cli_stream}, "
            f"stream_preamble_grace_ms={self.stream_preamble_grace_ms}, "
            f"stream_preamble_grace_chars={self.stream_preamble_grace_chars}, "
            f"stream_suppress_tool_preamble={self.stream_suppress_tool_preamble}, "
            f"subagent_enabled={self.subagent_enabled}, "
            f"subagent_max_depth={self.subagent_max_depth}, "
            f"subagent_model={self.subagent_model!r}, "
            f"subagent_max_iterations={self.subagent_max_iterations}, "
            f"subagent_timeout_seconds={self.subagent_timeout_seconds}, "
            f"subagent_max_children_per_turn={self.subagent_max_children_per_turn}, "
            f"subagent_max_total_per_turn={self.subagent_max_total_per_turn}, "
            f"subagent_result_max_chars={self.subagent_result_max_chars}, "
            f"subagent_tools={self.subagent_tools!r}, "
            f"subagent_exec_policy={self.subagent_exec_policy!r}, "
            f"subagent_exec_allowlist={self.subagent_exec_allowlist!r}, "
            f"subagent_exec_env_allowlist={self.subagent_exec_env_allowlist!r}, "
            f"subagent_write_requires_read={self.subagent_write_requires_read}, "
            f"subagent_sensitive_paths={self.subagent_sensitive_paths!r}, "
            f"subagent_audit_enabled={self.subagent_audit_enabled}, "
            f"subagent_audit_max_bytes={self.subagent_audit_max_bytes}, "
            f"workspace_mutation_lock_enabled={self.workspace_mutation_lock_enabled}, "
            f"workspace_mutation_lock_timeout={self.workspace_mutation_lock_timeout}, "
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

    # S5.1: 旧键 qq_stream_hard_chars 作为 qq_stream_max_block_chars 的别名(废弃)
    max_block_raw = _get(raw, *_KEY_ALIASES["qq_stream_max_block_chars"])
    if max_block_raw is None:
        hard_alias = _get(raw, *_KEY_ALIASES["qq_stream_hard_chars"])
        if hard_alias is not None:
            logger.warning(
                "qq_stream_hard_chars 已废弃, 请改用 qq_stream_max_block_chars; 本次按别名生效"
            )
            max_block_raw = hard_alias

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
        memory_max_cached_sessions=_resolve_session_cache_size(
            _get(raw, *_KEY_ALIASES["memory_max_cached_sessions"]),
            DEFAULT_MEMORY_MAX_CACHED_SESSIONS,
            "memory_max_cached_sessions",
        ),
        memory_cache_idle_seconds=_resolve_non_negative_float(
            _get(raw, *_KEY_ALIASES["memory_cache_idle_seconds"]),
            DEFAULT_MEMORY_CACHE_IDLE_SECONDS,
            "memory_cache_idle_seconds",
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
        qq_stream_first_chars=_resolve_positive_int(
            _get(raw, *_KEY_ALIASES["qq_stream_first_chars"]),
            DEFAULT_QQ_STREAM_FIRST_CHARS,
            "qq_stream_first_chars",
        ),
        qq_stream_min_chars=_resolve_positive_int(
            _get(raw, *_KEY_ALIASES["qq_stream_min_chars"]),
            DEFAULT_QQ_STREAM_MIN_CHARS,
            "qq_stream_min_chars",
        ),
        qq_stream_min_interval=_resolve_positive_float(
            _get(raw, *_KEY_ALIASES["qq_stream_min_interval"]),
            DEFAULT_QQ_STREAM_MIN_INTERVAL,
            "qq_stream_min_interval",
        ),
        qq_stream_max_messages=_resolve_positive_int(
            _get(raw, *_KEY_ALIASES["qq_stream_max_messages"]),
            DEFAULT_QQ_STREAM_MAX_MESSAGES,
            "qq_stream_max_messages",
        ),
        qq_stream_target_chars=_resolve_positive_int(
            _get(raw, *_KEY_ALIASES["qq_stream_target_chars"]),
            DEFAULT_QQ_STREAM_TARGET_CHARS,
            "qq_stream_target_chars",
        ),
        qq_stream_max_block_chars=_resolve_positive_int(
            max_block_raw,
            DEFAULT_QQ_STREAM_MAX_BLOCK_CHARS,
            "qq_stream_max_block_chars",
        ),
        qq_stream_flush_interval=_resolve_non_negative_float(
            _get(raw, *_KEY_ALIASES["qq_stream_flush_interval"]),
            DEFAULT_QQ_STREAM_FLUSH_INTERVAL,
            "qq_stream_flush_interval",
        ),
        qq_stream_hard_chars=0,  # 占位, 下一行由 max_block 回填(见下方 replace)
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
        # 子代理配置(SA1)
        subagent_enabled=_resolve_bool(
            _get(raw, *_KEY_ALIASES["subagent_enabled"]),
            DEFAULT_SUBAGENT_ENABLED,
            "subagent_enabled",
        ),
        subagent_max_depth=_resolve_subagent_max_depth(
            _get(raw, *_KEY_ALIASES["subagent_max_depth"]),
            DEFAULT_SUBAGENT_MAX_DEPTH,
        ),
        subagent_model=_get(raw, *_KEY_ALIASES["subagent_model"]) or "",
        subagent_max_iterations=_resolve_positive_int(
            _get(raw, *_KEY_ALIASES["subagent_max_iterations"]),
            DEFAULT_SUBAGENT_MAX_ITERATIONS,
            "subagent_max_iterations",
        ),
        subagent_timeout_seconds=_resolve_positive_float(
            _get(raw, *_KEY_ALIASES["subagent_timeout_seconds"]),
            DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
            "subagent_timeout_seconds",
        ),
        subagent_max_children_per_turn=_resolve_positive_int(
            _get(raw, *_KEY_ALIASES["subagent_max_children_per_turn"]),
            DEFAULT_SUBAGENT_MAX_CHILDREN_PER_TURN,
            "subagent_max_children_per_turn",
        ),
        subagent_max_total_per_turn=_resolve_positive_int(
            _get(raw, *_KEY_ALIASES["subagent_max_total_per_turn"]),
            DEFAULT_SUBAGENT_MAX_TOTAL_PER_TURN,
            "subagent_max_total_per_turn",
        ),
        subagent_result_max_chars=_resolve_positive_int(
            _get(raw, *_KEY_ALIASES["subagent_result_max_chars"]),
            DEFAULT_SUBAGENT_RESULT_MAX_CHARS,
            "subagent_result_max_chars",
        ),
        subagent_tools=_resolve_csv_tuple(
            _get(raw, *_KEY_ALIASES["subagent_tools"]),
            DEFAULT_SUBAGENT_TOOLS,
            "subagent_tools",
        ),
        subagent_exec_policy=_resolve_subagent_exec_policy(
            _get(raw, *_KEY_ALIASES["subagent_exec_policy"])
        ),
        subagent_exec_allowlist=_resolve_csv_tuple(
            _get(raw, *_KEY_ALIASES["subagent_exec_allowlist"]),
            DEFAULT_SUBAGENT_EXEC_ALLOWLIST,
            "subagent_exec_allowlist",
        ),
        subagent_exec_env_allowlist=_resolve_csv_tuple(
            _get(raw, *_KEY_ALIASES["subagent_exec_env_allowlist"]),
            DEFAULT_SUBAGENT_EXEC_ENV_ALLOWLIST,
            "subagent_exec_env_allowlist",
        ),
        subagent_write_requires_read=_resolve_bool(
            _get(raw, *_KEY_ALIASES["subagent_write_requires_read"]),
            DEFAULT_SUBAGENT_WRITE_REQUIRES_READ,
            "subagent_write_requires_read",
        ),
        subagent_sensitive_paths=_resolve_csv_tuple(
            _get(raw, *_KEY_ALIASES["subagent_sensitive_paths"]),
            DEFAULT_SUBAGENT_SENSITIVE_PATHS,
            "subagent_sensitive_paths",
        ),
        subagent_audit_enabled=_resolve_bool(
            _get(raw, *_KEY_ALIASES["subagent_audit_enabled"]),
            DEFAULT_SUBAGENT_AUDIT_ENABLED,
            "subagent_audit_enabled",
        ),
        subagent_audit_max_bytes=_resolve_positive_int(
            _get(raw, *_KEY_ALIASES["subagent_audit_max_bytes"]),
            DEFAULT_SUBAGENT_AUDIT_MAX_BYTES,
            "subagent_audit_max_bytes",
        ),
        workspace_mutation_lock_enabled=_resolve_bool(
            _get(raw, *_KEY_ALIASES["workspace_mutation_lock_enabled"]),
            DEFAULT_WORKSPACE_MUTATION_LOCK_ENABLED,
            "workspace_mutation_lock_enabled",
        ),
        workspace_mutation_lock_timeout=_resolve_positive_float(
            _get(raw, *_KEY_ALIASES["workspace_mutation_lock_timeout"]),
            DEFAULT_WORKSPACE_MUTATION_LOCK_TIMEOUT,
            "workspace_mutation_lock_timeout",
        ),
        source=str(path),
    )

    if settings.subagent_max_total_per_turn < settings.subagent_max_children_per_turn:
        logger.warning(
            "subagent_max_total_per_turn(%d) < subagent_max_children_per_turn(%d), "
            "实际并发/扇出以更小的总预算为准",
            settings.subagent_max_total_per_turn,
            settings.subagent_max_children_per_turn,
        )

    # 废弃字段 qq_stream_hard_chars 与 max_block_chars 保持一致(兼容旧读者)
    if settings.qq_stream_hard_chars != settings.qq_stream_max_block_chars:
        settings = replace(settings, qq_stream_hard_chars=settings.qq_stream_max_block_chars)

    if not settings.has_api_key:
        logger.warning("未读取到 api_key, 调用模型时会认证失败")
    return settings


# 兼容旧调用名的别名: 与 load_config 语义完全一致; 模块不再提供全局单例
load_settings = load_config
