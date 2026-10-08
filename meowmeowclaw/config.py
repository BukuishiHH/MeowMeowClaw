"""项目配置加载.
约定:
    - 配置文件为项目根目录下的 ``.env``(与 ``meowmeowclaw/`` 同级)
    - 取值优先级: **系统环境变量 > .env 文件 > 代码默认值**
    - 工作目录默认预设为与 ``meowmeowclaw/`` 同级的 ``workspace/``, 加载时自动创建.
用法::
    from meowmeowclaw.config import settings
    print(settings.workspace, settings.max_iterations)
"""
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union
from dotenv import dotenv_values

logger = logging.getLogger(__name__)

# 项目根目录: meowmeowclaw/ 的上一级(config.py 位于 meowmeowclaw/ 下)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"

# 默认值
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"
PRESET_WORKSPACE = PROJECT_ROOT / "workspace"
DEFAULT_MAX_ITERATIONS = 32
DEFAULT_IDENTITY_FILE = "identity.md"

# 键别名映射
_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "model": ("model", "model_name"),
    "api_key": ("api_key", "apikey", "llm_api_key", "openai_api_key", "deepseek_api_key"),
    "base_url": ("base_url", "openai_base_url", "llm_base_url", "api_base"),
    "workspace": ("workspace", "workspace_dir", "work_dir"),
    "max_iterations": ("max_iterations", "agent_max_iterations"),
    "identity_file": ("identity_file", "persona_file"),
}


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
    """合并来源: 系统环境变量覆盖.env, 键统一小写"""
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


def resolve_workspace(value: Optional[str]) -> Path:
    """解析工作目录, 相对路径基于项目根目录"""
    if value is None or value.strip() in (".", "./", ""):
        return PRESET_WORKSPACE
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return Path(os.path.normpath(path))


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


@dataclass(frozen=True)
class Settings:
    model: str = DEFAULT_MODEL
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    workspace: str = str(PRESET_WORKSPACE)
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    identity_file: str = DEFAULT_IDENTITY_FILE
    source: str = str(ENV_FILE)

    def __repr__(self) -> str:
        masked_key = "***" if self.api_key else None
        return (
            f"Settings(model={self.model!r}, base_url={self.base_url!r}, "
            f"workspace={self.workspace!r}, max_iterations={self.max_iterations}, "
            f"identity_file={self.identity_file!r}, api_key={masked_key}, source={self.source!r})"
        )

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)


def load_settings(env_file: Optional[Union[str, Path]] = None) -> Settings:
    path = Path(env_file) if env_file is not None else ENV_FILE
    raw = _merge_sources(read_env_file(path))

    settings = Settings(
        model=_get(raw, *_KEY_ALIASES["model"]) or DEFAULT_MODEL,
        api_key=_get(raw, *_KEY_ALIASES["api_key"]) or "",
        base_url=_get(raw, *_KEY_ALIASES["base_url"]) or DEFAULT_BASE_URL,
        workspace=str(resolve_workspace(_get(raw, *_KEY_ALIASES["workspace"]))),
        max_iterations=_resolve_max_iterations(_get(raw, *_KEY_ALIASES["max_iterations"])),
        identity_file=_get(raw, *_KEY_ALIASES["identity_file"]) or DEFAULT_IDENTITY_FILE,
        source=str(path),
    )

    if not settings.has_api_key:
        logger.warning("未读取到 api_key, 调用模型时会认证失败")

    # 自动创建工作目录
    try:
        Path(settings.workspace).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("创建工作目录失败 %s (%r)",  settings.workspace, exc)
    return settings


def load_config(env_file: Optional[Union[str, Path]] = None) -> Settings:
    """加载项目配置(对外入口名, 语义与 load_settings 完全一致).

    供 main.py 等入口在启动时调用; 每次调用都会重新读盘, 因此改完 .env 立即生效.
    """
    return load_settings(env_file)


# 全局单例: 各处 import 即用, 例如 from meowmeowclaw.config import settings
settings = load_settings()