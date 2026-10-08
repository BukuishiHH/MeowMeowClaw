"""项目路径的唯一事实来源.

项目根 / .env / 默认 workspace / identity.md 只在这里定义与解析:
其他模块不得再自行用 ``Path(__file__).resolve().parents[...]`` 推导路径,
也不得耦合进程当前工作目录(CWD).

用法::

    from meowmeowclaw.paths import IDENTITY_FILE, resolve_workspace
    workspace = resolve_workspace(raw_value)
"""

import os
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
