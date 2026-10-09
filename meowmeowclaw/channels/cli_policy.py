"""CLI 渠道策略(见 docs/GATEWAY_DESIGN.md §5.1/§6).

与 QQ 一样走总线, 但 CLI 是进程内单用户交互:
- 会话生命周期 = 进程生命周期(启动新建; /new//clear 换新), 无空闲轮换;
- 业务指令(/help /new /clear /sessions /tools /skills)在此裁决为直发回复;
- /exit /quit /q 属 REPL 本地命令, 由 CliAdapter 处理, 不进总线。
"""

import uuid
from typing import Any, Callable, Optional

from meowmeowclaw.gateway import Envelope, PolicyDecision, make_reply
from meowmeowclaw.memory import SessionKey, SessionSummary

from .commands import SessionCommandHelper

CLI_APP_NAME = "MeowMeowClaw"
CLI_PROMPT = "\n你 > "
CLI_EXIT_COMMANDS = {"/exit", "/quit", "/q"}

CLI_HELP_TEXT = """可用命令:
  /help                       查看本帮助
  /new                        新建会话(旧会话保留在 sessions/)
  /clear                      归档当前会话并新建
  /clear <会话ID>             归档指定会话
  /clear <会话ID> --purge     永久删除指定会话
  /sessions [--active|--archived]  列出会话
  /tools                      查看已注册工具
  /skills                     查看已发现技能
  /exit (/quit /q)            退出"""


def new_cli_session() -> SessionKey:
    """生成一个新的 CLI 进程级会话键(每次启动 /new /clear 后调用)."""
    return SessionKey(channel="cli", scope="session", conversation_id=uuid.uuid4().hex)


def session_short_id(session: SessionKey) -> str:
    return session.storage_id[:8]


class CliPolicy:
    """CLI 渠道策略; 每个进程一个实例, 由 Gateway 按 channel="cli" 路由."""

    channel = "cli"

    def __init__(
        self,
        conversation: Any,
        *,
        tools: Optional[Any] = None,
        catalog: Optional[Any] = None,
        session_factory: Optional[Callable[[], SessionKey]] = None,
    ) -> None:
        self.conversation = conversation
        self.tools = tools
        self.catalog = catalog
        self._session_factory = session_factory or new_cli_session
        self._current = self._session_factory()
        self._commands = SessionCommandHelper(conversation)

    def __repr__(self) -> str:
        return f"<CliPolicy session={self._current.canonical!r}>"

    @property
    def current_session(self) -> SessionKey:
        return self._current

    def new_session(self) -> SessionKey:
        """切换到一个全新会话(旧会话保持原状)."""
        self._current = self._session_factory()
        return self._current

    # ------------------------------------------------------------------ 入口

    async def resolve(self, envelope: Envelope) -> PolicyDecision:
        text = (envelope.text or "").strip()
        if not text:
            return PolicyDecision.ignore()
        if text.startswith("/"):
            replies = await self._handle_command(envelope, text)
            if not replies:
                return PolicyDecision.ignore()
            return PolicyDecision.reply(*replies)
        return PolicyDecision.agent(
            self._current,
            text,
            meta={"channel": "cli", "scope": "session"},
        )

    # ------------------------------------------------------------------ 命令

    @staticmethod
    def _usage_error(command: str) -> str:
        return f"未知命令或参数: {command}. 输入 /help 查看用法."

    async def _handle_command(self, request: Envelope, command: str) -> list[Envelope]:
        parts = command.strip().split()
        name = parts[0].lower()
        args = parts[1:]

        if name == "/help":
            return [make_reply(request, CLI_HELP_TEXT)]

        if name == "/tools":
            return [make_reply(request, self._format_tools())]

        if name == "/skills":
            return [make_reply(request, self._format_skills())]

        if name == "/new":
            if args:
                return [make_reply(request, self._usage_error(command))]
            new_session = self.new_session()
            return [
                make_reply(
                    request,
                    f"已开始新会话: {session_short_id(new_session)} ({new_session.canonical})",
                )
            ]

        if name == "/sessions":
            include_archived = "--active" not in args
            sessions = await self.conversation.list_sessions(include_archived=include_archived)
            if "--active" in args:
                sessions = [item for item in sessions if not item.archived]
            if "--archived" in args:
                sessions = [item for item in sessions if item.archived]
            return [make_reply(request, self._format_sessions(sessions))]

        if name == "/clear":
            purge = "--purge" in args
            tokens = [item for item in args if item != "--purge"]
            if len(tokens) > 1:
                return [make_reply(request, self._usage_error(command))]

            target: Optional[SessionSummary]
            if tokens:
                target, error = await self._commands.resolve_summary(tokens[0])
                if target is None:
                    return [make_reply(request, error or "会话不存在")]
            else:
                current_summaries = [
                    item
                    for item in await self.conversation.list_sessions(include_archived=True)
                    if item.storage_id == self._current.storage_id
                ]
                target = current_summaries[0] if current_summaries else None

            if target is None:
                # 当前会话尚未写入任何消息(空会话): 直接切换新会话即可
                new_session = self.new_session()
                return [
                    make_reply(
                        request,
                        f"当前会话为空, 已开始新会话: {session_short_id(new_session)}",
                    )
                ]

            action = await self._commands.archive_or_purge(target, purge=purge)
            if target.storage_id == self._current.storage_id:
                new_session = self.new_session()
                return [
                    make_reply(
                        request,
                        f"{action}\n已开始新会话: {session_short_id(new_session)} "
                        f"({new_session.canonical})",
                    )
                ]
            return [make_reply(request, action)]

        return [make_reply(request, self._usage_error(command))]

    # ------------------------------------------------------------------ 展示

    def _format_tools(self) -> str:
        if self.tools is None:
            return "未注册工具."
        definitions = self.tools.get_definitions()
        lines = [f"已注册工具({len(definitions)} 个):"]
        for definition in definitions:
            function = definition.get("function", {})
            lines.append(f"  - {function.get('name')}: {function.get('description', '')}")
        return "\n".join(lines)

    def _format_skills(self) -> str:
        if self.catalog is None:
            return "未发现技能."
        skills = list(self.catalog.skills())
        lines = [f"已发现技能({len(skills)} 个):"]
        for skill in skills:
            lines.append(f"  - {skill.name} ({skill.dir_name}/SKILL.md): {skill.description}")
        return "\n".join(lines)

    def _format_sessions(self, summaries: list[SessionSummary]) -> str:
        if not summaries:
            return "暂无会话."
        lines = [
            "短ID      状态      渠道   创建时间(UTC)           最后活动(UTC)           轮数  当前"
        ]
        for summary in summaries:
            status = "archived" if summary.archived else "active"
            marker = "  *" if summary.storage_id == self._current.storage_id else ""
            lines.append(
                f"{summary.short_id:<8}  {status:<8}  {summary.channel:<5}  "
                f"{summary.created_at_iso:<22}  {summary.updated_at_iso:<22}  "
                f"{summary.turn_count:>4}{marker}"
            )
        return "\n".join(lines)
