"""子代理领域模型(SA1, 设计见 ``docs/SUBAGENT_DESIGN.md``).

本模块只提供纯数据类型与状态映射, 不依赖 AgentLoop / ToolRegistry / bootstrap,
因此可以独立测试, 也不会在 import 时产生副作用。SA2+ 的 factory 会复用这些类型。
"""

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

# 子代理结果状态(见设计文档 §4.5)
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"
STATUS_LIMIT = "limit"
VALID_STATUSES = frozenset({STATUS_OK, STATUS_ERROR, STATUS_TIMEOUT, STATUS_CANCELLED, STATUS_LIMIT})

# 错误码: 供 tool 结果 JSON 使用(见设计文档 §4.5/§5)
ERROR_ENABLED = "enabled"
ERROR_DEPTH_EXCEEDED = "depth_exceeded"
ERROR_BUDGET_EXCEEDED = "budget_exceeded"
ERROR_CHILDREN_EXCEEDED = "children_exceeded"
ERROR_TOOL_NOT_ALLOWED = "tool_not_allowed"
ERROR_TIMEOUT = "timeout"
ERROR_CANCELLED = "cancelled"
ERROR_MAX_ITERATIONS = "max_iterations"
ERROR_CONTEXT_OVERFLOW = "context_overflow"
ERROR_CIRCUIT_BREAK = "circuit_break"
ERROR_PROVIDER = "provider_error"
ERROR_INTERNAL = "internal_error"


def _clean_optional_text(value: Optional[str], name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串或 None")
    cleaned = value.strip()
    return cleaned or None


@dataclass(frozen=True)
class SubagentRequest:
    """一次 spawn 的输入(工具参数投影).

    - ``task``: 自包含任务描述(必填);
    - ``role``: 可选角色短标签, 只作为固定模板的角色提示;
    - ``context``: 父代理显式提供的背景资料(数据, 不是指令)。
    """

    task: str
    role: Optional[str] = None
    context: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.task, str) or not self.task.strip():
            raise ValueError("子代理 task 不能为空")
        object.__setattr__(self, "task", self.task.strip())
        object.__setattr__(self, "role", _clean_optional_text(self.role, "role"))
        object.__setattr__(self, "context", _clean_optional_text(self.context, "context"))

    @property
    def task_digest(self) -> str:
        """任务摘要(审计/统计用, 不暴露全文)."""
        return hashlib.sha256(self.task.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class SubagentResult:
    """一次 spawn 的结果, 会被序列化为 JSON 工具结果(见设计文档 §4.5)."""

    status: str
    summary: str
    finish_reason: str
    iterations: int
    elapsed_ms: int
    subagent_id: str
    depth: int = 0
    model: str = ""
    error_code: Optional[str] = None
    truncated: bool = False

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"非法子代理状态: {self.status!r}")
        if not isinstance(self.summary, str):
            raise ValueError("summary 必须是字符串")
        if not isinstance(self.finish_reason, str):
            raise ValueError("finish_reason 必须是字符串")
        if not isinstance(self.subagent_id, str) or not self.subagent_id:
            raise ValueError("subagent_id 不能为空")
        if self.iterations < 0:
            raise ValueError("iterations 不能为负数")
        if self.elapsed_ms < 0:
            raise ValueError("elapsed_ms 不能为负数")
        if self.depth < 0:
            raise ValueError("depth 不能为负数")

    def to_dict(self) -> dict[str, Any]:
        """JSON 友好 dict; None 的 error_code 省略, 保持结果紧凑."""
        data: dict[str, Any] = {
            "status": self.status,
            "summary": self.summary,
            "finish_reason": self.finish_reason,
            "iterations": self.iterations,
            "elapsed_ms": self.elapsed_ms,
            "subagent_id": self.subagent_id,
            "depth": self.depth,
            "model": self.model,
            "truncated": self.truncated,
        }
        if self.error_code is not None:
            data["error_code"] = self.error_code
        return data

    def to_json(self, *, max_chars: Optional[int] = None) -> str:
        """序列化为 JSON 字符串; 超过 ``max_chars`` 时优先截断 summary.

        截断后 ``truncated=True`` 且仍保证是合法 JSON; 若连最小 JSON 都放不下,
        则原样返回最小 JSON(此时由调用方保证 max_chars 配置合理)。
        """
        text = json.dumps(self.to_dict(), ensure_ascii=False)
        if max_chars is None or max_chars <= 0 or len(text) <= max_chars:
            return text

        summary = self.summary
        low, high = 0, len(summary)
        best: Optional[str] = None
        while low <= high:
            mid = (low + high) // 2
            candidate_data = self.to_dict()
            candidate_data["summary"] = summary[:mid]
            candidate_data["truncated"] = True
            candidate = json.dumps(candidate_data, ensure_ascii=False)
            if len(candidate) <= max_chars:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        if best is not None:
            return best

        # 兜底: 只保留必需字段, 避免非法 JSON
        minimal = {
            "status": self.status,
            "summary": "",
            "truncated": True,
        }
        if self.error_code is not None:
            minimal["error_code"] = self.error_code
        return json.dumps(minimal, ensure_ascii=False)

    @classmethod
    def from_turn(
        cls,
        turn: Any,
        *,
        subagent_id: str,
        depth: int,
        model: str,
        elapsed_ms: int,
    ) -> "SubagentResult":
        """把 ``AgentTurn``(duck type)折叠为结构化结果.

        状态映射(与设计文档 §4.5 一致):
        - completed=True 且 finish_reason=stop → ok;
        - max_iterations/context_overflow/circuit_break → limit/error(带具体 error_code);
        - finish_reason=error → error/provider_error;
        - 其它未完成 → error/internal_error;
        - 其它已完成 → ok。
        """
        completed = bool(getattr(turn, "completed", False))
        finish_reason = str(getattr(turn, "finish_reason", "") or "")
        summary = str(getattr(turn, "answer", "") or "")
        iterations = int(getattr(turn, "iterations", 0) or 0)

        status = STATUS_OK
        error_code: Optional[str] = None
        if completed and finish_reason in ("stop", ""):
            status = STATUS_OK
        elif finish_reason == "max_iterations":
            status = STATUS_LIMIT
            error_code = ERROR_MAX_ITERATIONS
        elif finish_reason == "context_overflow":
            status = STATUS_ERROR
            error_code = ERROR_CONTEXT_OVERFLOW
        elif finish_reason == "circuit_break":
            status = STATUS_ERROR
            error_code = ERROR_CIRCUIT_BREAK
        elif finish_reason == "error":
            status = STATUS_ERROR
            error_code = ERROR_PROVIDER
        elif completed:
            status = STATUS_OK
        else:
            status = STATUS_ERROR
            error_code = ERROR_INTERNAL

        return cls(
            status=status,
            summary=summary,
            finish_reason=finish_reason or "unknown",
            iterations=iterations,
            elapsed_ms=max(0, int(elapsed_ms)),
            subagent_id=subagent_id,
            depth=depth,
            model=model,
            error_code=error_code,
            truncated=False,
        )


@dataclass
class SubagentBudget:
    """一棵 root turn 共享的子代理预算(children + total).

    计数在 asyncio.Lock 内完成; root 与其所有后代共享同一实例。
    """

    max_children_per_turn: int = 3
    max_total_per_turn: int = 6
    total_spawned: int = 0
    _children: dict[str, int] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.max_children_per_turn < 1:
            raise ValueError("max_children_per_turn 必须 >= 1")
        if self.max_total_per_turn < 1:
            raise ValueError("max_total_per_turn 必须 >= 1")

    async def reserve_child(self, parent_id: str) -> Optional[str]:
        """为 ``parent_id`` 预留一个直接子代理名额.

        :return: None 表示成功; 否则返回错误码
                 (``children_exceeded`` / ``budget_exceeded``)。
        """
        if not isinstance(parent_id, str) or not parent_id:
            return ERROR_INTERNAL
        async with self._lock:
            if self.total_spawned >= self.max_total_per_turn:
                return ERROR_BUDGET_EXCEEDED
            used = self._children.get(parent_id, 0)
            if used >= self.max_children_per_turn:
                return ERROR_CHILDREN_EXCEEDED
            self.total_spawned += 1
            self._children[parent_id] = used + 1
            return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "max_children_per_turn": self.max_children_per_turn,
            "max_total_per_turn": self.max_total_per_turn,
            "total_spawned": self.total_spawned,
            "per_parent": dict(self._children),
        }


@dataclass(frozen=True)
class SpawnContext:
    """factory 构造子代理时的冻结上下文(见设计文档 §3.2).

    ``depth`` 是**发起 spawn 的当前代理深度**; 子代理 depth = ``depth + 1``。
    """

    root_id: str
    parent_id: str
    depth: int
    budget: SubagentBudget
    tool_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.root_id, str) or not self.root_id:
            raise ValueError("root_id 不能为空")
        if not isinstance(self.parent_id, str) or not self.parent_id:
            raise ValueError("parent_id 不能为空")
        if self.depth < 0:
            raise ValueError("depth 不能为负数")


@dataclass
class SubagentStats:
    """子代理观测计数(见设计文档 §8.1); 单事件循环内同步累加, snapshot 无副作用."""

    enabled: bool = False
    spawned_total: int = 0
    active: int = 0
    ok: int = 0
    error: int = 0
    timeout: int = 0
    cancelled: int = 0
    limited: int = 0
    max_depth_seen: int = 0
    conflicts_detected: int = 0
    write_conflicts: int = 0
    exec_denied: int = 0
    lock_wait_total_ms: float = 0.0
    lock_wait_max_ms: float = 0.0

    def record_spawn(self, depth: int = 0) -> None:
        self.spawned_total += 1
        self.active += 1
        self.max_depth_seen = max(self.max_depth_seen, int(depth))

    def record_finish(self, status: str, *, lock_wait_ms: float = 0.0) -> None:
        self.active = max(0, self.active - 1)
        if status == STATUS_OK:
            self.ok += 1
        elif status == STATUS_TIMEOUT:
            self.timeout += 1
        elif status == STATUS_CANCELLED:
            self.cancelled += 1
        elif status == STATUS_LIMIT:
            self.limited += 1
        else:
            self.error += 1
        if lock_wait_ms > 0:
            self.lock_wait_total_ms += float(lock_wait_ms)
            self.lock_wait_max_ms = max(self.lock_wait_max_ms, float(lock_wait_ms))

    def record_conflict(self, *, write: bool = False) -> None:
        self.conflicts_detected += 1
        if write:
            self.write_conflicts += 1

    def record_exec_denied(self) -> None:
        self.exec_denied += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "spawned_total": self.spawned_total,
            "active": self.active,
            "ok": self.ok,
            "error": self.error,
            "timeout": self.timeout,
            "cancelled": self.cancelled,
            "limited": self.limited,
            "max_depth_seen": self.max_depth_seen,
            "conflicts_detected": self.conflicts_detected,
            "write_conflicts": self.write_conflicts,
            "exec_denied": self.exec_denied,
            "lock_wait_total_ms": round(self.lock_wait_total_ms, 3),
            "lock_wait_max_ms": round(self.lock_wait_max_ms, 3),
        }

    def reset(self) -> None:
        """仅测试/排障使用: 重置全部计数, 不改变 enabled."""
        self.spawned_total = 0
        self.active = 0
        self.ok = 0
        self.error = 0
        self.timeout = 0
        self.cancelled = 0
        self.limited = 0
        self.max_depth_seen = 0
        self.conflicts_detected = 0
        self.write_conflicts = 0
        self.exec_denied = 0
        self.lock_wait_total_ms = 0.0
        self.lock_wait_max_ms = 0.0


__all__ = [
    "ERROR_BUDGET_EXCEEDED",
    "ERROR_CANCELLED",
    "ERROR_CHILDREN_EXCEEDED",
    "ERROR_CIRCUIT_BREAK",
    "ERROR_CONTEXT_OVERFLOW",
    "ERROR_DEPTH_EXCEEDED",
    "ERROR_ENABLED",
    "ERROR_INTERNAL",
    "ERROR_MAX_ITERATIONS",
    "ERROR_PROVIDER",
    "ERROR_TIMEOUT",
    "ERROR_TOOL_NOT_ALLOWED",
    "STATUS_CANCELLED",
    "STATUS_ERROR",
    "STATUS_LIMIT",
    "STATUS_OK",
    "STATUS_TIMEOUT",
    "VALID_STATUSES",
    "SpawnContext",
    "SubagentBudget",
    "SubagentRequest",
    "SubagentResult",
    "SubagentStats",
]
