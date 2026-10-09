"""跨渠道会话指令公共逻辑(见 docs/GATEWAY_DESIGN.md §6).

QQ / CLI 的 ``/sessions``、``/clear`` 行为保持一致; 各自只负责把结果包成
平台回复信封与执行渠道特有的"新建会话"动作。
"""

from typing import TYPE_CHECKING, Optional, Sequence

from meowmeowclaw.memory import SessionSummary

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示
    from meowmeowclaw.conversation import ConversationService


class SessionCommandHelper:
    """会话类指令的共享实现(不含渠道展示差异)."""

    def __init__(self, conversation: "ConversationService") -> None:
        self.conversation = conversation

    async def resolve_summary(
        self, token: str
    ) -> tuple[Optional[SessionSummary], Optional[str]]:
        """按短 ID 前缀解析会话; 返回 (summary, 错误文本)."""
        sessions = await self.conversation.list_sessions(include_archived=True)
        matches = [item for item in sessions if item.storage_id.startswith(token)]
        if not matches:
            return None, f"未找到会话: {token}. 可用 /sessions 查看."
        if len(matches) > 1:
            candidates = ", ".join(f"{item.short_id}({item.channel})" for item in matches)
            return None, f"会话 ID 前缀不唯一: {token}. 候选: {candidates}"
        return matches[0], None

    async def archive_or_purge(self, summary: SessionSummary, *, purge: bool) -> str:
        """归档/删除指定会话, 返回用户可见的动作文案(跨渠道可用)."""
        label = f"{summary.short_id}({summary.channel})"
        if purge:
            await self.conversation.purge_session(summary.session_key)
            return f"已永久删除会话: {label}"
        if summary.archived:
            return f"会话已处于归档状态: {label}"
        await self.conversation.archive_session(summary.session_key)
        return f"已归档会话: {label}"

    @staticmethod
    def format_sessions(
        sessions: Sequence[SessionSummary],
        *,
        active_session_id: Optional[str] = None,
    ) -> str:
        """统一的会话列表展示; ``active_session_id`` 命中时追加 "<- 当前"."""
        if not sessions:
            return "暂无会话."
        lines = ["会话列表:"]
        for summary in sessions:
            status = "archived" if summary.archived else "active"
            marker = (
                "  <- 当前"
                if active_session_id is not None
                and summary.session_key.session_id == active_session_id
                else ""
            )
            lines.append(
                f"- {summary.short_id} [{status}] {summary.channel} "
                f"轮数={summary.turn_count} 最后活动={summary.updated_at_iso}{marker}"
            )
        return "\n".join(lines)
