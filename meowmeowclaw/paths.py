"""项目路径的唯一事实来源.

项目根 / .env / 默认 workspace / identity.md 只在这里定义与解析:
其他模块不得再自行用 ``Path(__file__).resolve().parents[...]`` 推导路径,
也不得耦合进程当前工作目录(CWD).

用法::

    from meowmeowclaw.paths import IDENTITY_FILE, resolve_workspace
    workspace = resolve_workspace(raw_value)
"""

import os
import re
from pathlib import Path
from typing import Optional

# 项目根目录: 包目录的上一级(paths.py 位于 <项目根>/meowmeowclaw/ 下)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 配置文件
ENV_FILE = PROJECT_ROOT / ".env"
# 默认工作区: 运行时数据目录(与包同级); .env 可用绝对路径覆盖
DEFAULT_WORKSPACE = PROJECT_ROOT / "workspace"
# 人设文件: 固定放项目根, 随仓库提供
IDENTITY_FILE = PROJECT_ROOT / "identity.md"
# 本地 tokenizer 资产目录(模型分词器文件, 体积较大, 默认不入库; 见 .gitignore)
DEFAULT_TOKENIZER_DIR = PROJECT_ROOT / "tokenizers"


def resolve_workspace(value: Optional[str]) -> Path:
    """
    解析 ``workspace`` 配置取值:

    - 未设置 / 空白 / ``.`` / ``./`` -> 默认 ``<项目根>/workspace``
    - 绝对路径(支持 ``~``)             -> 原样使用
    - 相对路径                        -> 基于项目根解析, 不随当前工作目录漂移
    """
    if value is None or value.strip() in (".", "./", ""):
        return DEFAULT_WORKSPACE
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return Path(os.path.normpath(str(path)))


def resolve_memory_dir(value: Optional[str], workspace: Path) -> Path:
    """
    解析 ``memory_dir`` 配置取值:

    - 未设置 / 空白 / ``.`` / ``./`` -> ``<workspace>/memory``(随 workspace 迁移)
    - 绝对路径(支持 ``~``)             -> 原样使用
    - 相对路径                        -> 基于项目根解析(与 workspace 配置同一约定)
    """
    if value is None or value.strip() in (".", "./", ""):
        return Path(workspace) / "memory"
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return Path(os.path.normpath(str(path)))


_MODEL_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_model_name(model: str) -> str:
    """
    把模型名净化成可用作目录/文件名的形式.

    例: ``deepseek-ai/DeepSeek-V4-Flash`` -> ``deepseek-ai_DeepSeek-V4-Flash``;
    空名统一回退 ``unknown``.
    """
    text = str(model or "").strip()
    if not text:
        return "unknown"
    return _MODEL_NAME_SAFE_RE.sub("_", text)


def resolve_tokenizer_path(raw_value: Optional[str], model: str) -> Optional[Path]:
    """
    解析本地 ``tokenizer.json`` 路径(唯一入口, 其他模块不得自行拼路径).

    - ``raw_value`` 非空: 绝对路径原样使用, 相对路径按**项目根**解析;
      文件不存在返回 ``None``;
    - ``raw_value`` 为空: 依次查找
        1. ``<PROJECT_ROOT>/tokenizers/<sanitized_model>/tokenizer.json``
        2. ``<PROJECT_ROOT>/tokenizers/<sanitized_model>.json``

    :return: 存在的文件路径; 未配置且候选均不存在返回 ``None``
    """
    if raw_value is not None and str(raw_value).strip():
        path = Path(str(raw_value).strip()).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path = Path(os.path.normpath(str(path)))
        return path if path.is_file() else None

    sanitized = sanitize_model_name(model)
    candidates = (
        DEFAULT_TOKENIZER_DIR / sanitized / "tokenizer.json",
        DEFAULT_TOKENIZER_DIR / f"{sanitized}.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
