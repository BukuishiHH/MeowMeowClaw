"""工具调用审计(GM6, §8): 可选 JSONL, 只存摘要不存输出全文.

- 由 ``--audit-log PATH`` / ``MCP_GIT_AUDIT_LOG`` 开启, 未配置则不写;
- 每次**工具调用**追加一行(紧凑 JSON):
  ``{"ts","tool","repo","args_digest","status","error_code","exit_code","elapsed_ms","truncated"}``
  - ``args_digest``: 参数规范 JSON 的 sha256 前 16 位, **不落盘参数值**(message/paths 等不入库);
  - ``status``: ``ok``/``error``; ``exit_code``/``truncated`` 取工具信封 meta/data;
  - prompts/resources 属于只读查询, 不在此审计(需要时可查 stderr 日志);
- 新建文件权限 0o600(§6.4); 初始化失败在启动期抛 :class:`ConfigError`(退出码 2);
  运行期写失败 fail-soft(只 warning, 不影响工具返回);
- 不做轮转(最小实现); 建议路径不与仓库 ``.git`` 混放(§8)。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Mapping, Union

from .errors import ConfigError

logger = logging.getLogger(__name__)


def digest_arguments(arguments: Any) -> str:
    """参数摘要(sha256 前 16 位); 非 JSON 对象退化为 repr, 永不抛异常."""
    try:
        canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):  # pragma: no cover - default=str 已兜底
        canonical = repr(arguments)
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()[:16]


class AuditSink:
    """JSONL 审计写入器(线程安全, fail-soft)."""

    def __init__(self, path: Union[str, Path]) -> None:
        if not str(path).strip():
            raise ConfigError("审计路径不能为空")
        self.path = Path(path)
        self._lock = threading.Lock()
        self._prepare()

    def __repr__(self) -> str:
        return f"<AuditSink path={str(self.path)!r}>"

    def _prepare(self) -> None:
        """启动期校验: 建父目录、按 0600 创建/收紧权限、确认可写."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise ConfigError(f"无法初始化审计文件: {self.path} ({exc})") from exc
        if not os.access(self.path, os.W_OK):
            raise ConfigError(f"审计文件不可写: {self.path}")

    def record(self, event: Mapping[str, Any]) -> None:
        """追加一条审计事件; 任何写入失败只 warning(不影响工具主流程)."""
        try:
            line = json.dumps(dict(event), ensure_ascii=False, default=str, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            logger.warning("审计事件序列化失败(已忽略): %r", exc)
            return
        try:
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError as exc:
            logger.warning("审计写入失败(已忽略): %s (%r)", self.path, exc)
