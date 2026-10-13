"""子代理审计写入(SA4, 见 docs/SUBAGENT_DESIGN.md §7.6/§8.3).

- 审计文件: ``<memory_dir>/subagents/audit.jsonl``, 模型工具不可见;
- 单份轮转(``audit.jsonl`` → ``audit.jsonl.1``), 阈值由配置控制;
- 写入失败 fail-soft: 只 warning, 不影响子代理主流程。
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Union

logger = logging.getLogger(__name__)


class SubagentAuditWriter:
    """JSONL 追加审计; 每个事件一行, 字段由调用方提供."""

    def __init__(
        self,
        path: Union[str, Path],
        *,
        max_bytes: int = 4_194_304,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes 必须 > 0")
        self.path = Path(path)
        self.max_bytes = int(max_bytes)

    def __repr__(self) -> str:
        return f"<SubagentAuditWriter path={str(self.path)!r} max_bytes={self.max_bytes}>"

    def record(self, event: dict[str, Any]) -> None:
        """追加一条审计事件; 失败只 warning."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed()
            line = json.dumps(event, ensure_ascii=False, default=str)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            logger.warning("子代理审计写入失败(已忽略): %s (%r)", self.path, exc)
        except (TypeError, ValueError) as exc:
            logger.warning("子代理审计序列化失败(已忽略): %r", exc)

    def _rotate_if_needed(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.max_bytes:
            return
        rotated = self.path.with_name(self.path.name + ".1")
        try:
            os.replace(self.path, rotated)
        except OSError as exc:
            logger.warning("子代理审计轮转失败(已忽略): %s (%r)", self.path, exc)


__all__ = ["SubagentAuditWriter"]
