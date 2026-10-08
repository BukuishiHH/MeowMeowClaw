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

from meowmeowclaw.paths import DEFAULT_WORKSPACE, ENV_FILE, IDENTITY_FILE, resolve_workspace

logger = logging.getLogger(__name__)

# 默认值
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MAX_ITERATIONS = 32

# 键别名映射
_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "model": ("model", "model_name"),
    "api_key": ("api_key", "apikey", "llm_api_key", "openai_api_key", "deepseek_api_key"),
    "base_url": ("base_url", "openai_base_url", "llm_base_url", "api_base"),
    "workspace": ("workspace", "workspace_dir", "work_dir"),
    "max_iterations": ("max_iterations", "agent_max_iterations"),
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
    source: str = str(ENV_FILE)

    def __repr__(self) -> str:
        masked_key = "***" if self.api_key else None
        return (
            f"Settings(model={self.model!r}, base_url={self.base_url!r}, "
            f"workspace={str(self.workspace)!r}, max_iterations={self.max_iterations}, "
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

    settings = Settings(
        model=_get(raw, *_KEY_ALIASES["model"]) or DEFAULT_MODEL,
        api_key=_get(raw, *_KEY_ALIASES["api_key"]) or "",
        base_url=_get(raw, *_KEY_ALIASES["base_url"]) or DEFAULT_BASE_URL,
        workspace=resolve_workspace(_get(raw, *_KEY_ALIASES["workspace"])),
        max_iterations=_resolve_max_iterations(_get(raw, *_KEY_ALIASES["max_iterations"])),
        source=str(path),
    )

    if not settings.has_api_key:
        logger.warning("未读取到 api_key, 调用模型时会认证失败")
    return settings


# 兼容旧调用名的别名: 与 load_config 语义完全一致; 模块不再提供全局单例
load_settings = load_config
