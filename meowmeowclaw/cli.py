"""命令行交付层: banner / 启动信息 / REPL / 命令 / 用户可见输出.

只依赖 ``bootstrap`` 与契约类型, 不直接 import 任何具体 Provider / 工具实现;
会话与短期记忆通过 ``ConversationService`` 编排。
"""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Optional

from meowmeowclaw.bootstrap import Application, ConfigError, build_application
from meowmeowclaw.config import Settings
from meowmeowclaw.memory import (
    MemoryStoreError,
    SessionKey,
    SessionSummary,
)
from meowmeowclaw.paths import IDENTITY_FILE
from meowmeowclaw.skills import SkillCatalog, SkillConfigError
from meowmeowclaw.tools.registry import ToolRegistry

# 应用显示名(项目名见 README 与人设文件)
APP_NAME = "MeowMeowClaw"
# 交互提示符
PROMPT = "\n你 > "
# 退出命令
EXIT_COMMANDS = {"/exit", "/quit", "/q"}

BANNER = r"""
    /\\_/\\
   ( • w • )    MeowMeowClaw
    >     <     一个会用工具的小猫
                输入内容开始对话, /exit 退出
"""

HELP_TEXT = """可用命令:
  /help                       查看本帮助
  /new                        新建会话(旧会话保留在 sessions/)
  /clear                      归档当前会话并新建
  /clear <会话ID>             归档指定会话
  /clear <会话ID> --purge     永久删除指定会话
  /sessions [--active|--archived]  列出会话
  /tools                      查看已注册工具
  /skills                     查看已发现技能
  /exit (/quit /q)            退出"""


# ------------------------------------------------------------------ 会话


def new_cli_session() -> SessionKey:
    """生成一个新的 CLI 进程级会话键(每次启动 /new /clear 后调用)."""
    return SessionKey(channel="cli", scope="session", conversation_id=uuid.uuid4().hex)


def session_short_id(session: SessionKey) -> str:
    return session.storage_id[:8]


# ------------------------------------------------------------------ 启动信息


def _print_startup_info(config: Settings, session: SessionKey) -> None:
    """打印一行式启动信息(api_key 已由 Settings.__repr__ 掩码, 这里不打印密钥)."""
    print(f"  模型      : {config.model}")
    print(f"  接口地址  : {config.base_url}")
    print(f"  工作目录  : {config.workspace}")
    print(f"  记忆目录  : {config.memory_dir}")
    print(f"  人设文件  : {IDENTITY_FILE}")
    print(f"  最大迭代  : {config.max_iterations}")
    print(f"  会话      : {session.canonical}")
    print(f"  会话短ID  : {session_short_id(session)}")
    print(f"  配置文件  : {config.source}")


def _print_tools(tools: ToolRegistry) -> None:
    """打印已注册工具(名称 + 描述, 描述给模型看, 这里也方便人排查)."""
    definitions = tools.get_definitions()
    print(f"已注册工具({len(definitions)} 个):")
    for definition in definitions:
        function = definition.get("function", {})
        print(f"  - {function.get('name')}: {function.get('description', '')}")


def _print_skills(catalog: SkillCatalog) -> None:
    """打印已发现技能(名 + 描述 + 来源), 供人排查."""
    print(f"已发现技能({len(catalog)} 个):")
    for skill in catalog.skills():
        print(f"  - {skill.name} ({skill.dir_name}/SKILL.md): {skill.description}")


def _print_sessions(summaries: list[SessionSummary], current: SessionKey) -> None:
    """打印 /sessions 列表."""
    if not summaries:
        print("暂无会话.")
        return
    print("短ID      状态      渠道   创建时间(UTC)           最后活动(UTC)           轮数  当前")
    for summary in summaries:
        status = "archived" if summary.archived else "active"
        marker = "  *" if summary.storage_id == current.storage_id else ""
        print(
            f"{summary.short_id:<8}  {status:<8}  {summary.channel:<5}  "
            f"{summary.created_at_iso:<22}  {summary.updated_at_iso:<22}  "
            f"{summary.turn_count:>4}{marker}"
        )


# ------------------------------------------------------------------ 命令


@dataclass(frozen=True)
class CommandOutcome:
    """命令处理结果: 是否退出 + 后续使用的当前会话."""

    exit_requested: bool
    session: SessionKey


def _usage_error(command: str) -> None:
    print(f"未知命令或参数: {command}. 输入 /help 查看用法.")


async def _resolve_session(app: Application, token: str) -> Optional[SessionSummary]:
    """按短 ID 前缀解析会话; 无匹配/歧义时打印提示并返回 None."""
    sessions = await app.conversation.list_sessions(include_archived=True)
    matches = [item for item in sessions if item.storage_id.startswith(token)]
    if not matches:
        print(f"未找到会话: {token}. 可用 /sessions 查看.")
        return None
    if len(matches) > 1:
        candidates = ", ".join(f"{item.short_id}({item.channel})" for item in matches)
        print(f"会话 ID 前缀不唯一: {token}. 候选: {candidates}")
        return None
    return matches[0]


async def _handle_command(command: str, app: Application, current: SessionKey) -> CommandOutcome:
    """处理 "/" 开头的命令; 返回是否退出与需要继续使用的会话."""
    parts = command.strip().split()
    name = parts[0].lower()
    args = parts[1:]

    if name in EXIT_COMMANDS:
        print("再见!")
        return CommandOutcome(exit_requested=True, session=current)

    if name == "/help":
        print(HELP_TEXT)
        return CommandOutcome(exit_requested=False, session=current)

    if name == "/tools":
        _print_tools(app.registry)
        return CommandOutcome(exit_requested=False, session=current)

    if name == "/skills":
        _print_skills(app.catalog)
        return CommandOutcome(exit_requested=False, session=current)

    if name == "/new":
        if args:
            _usage_error(command)
            return CommandOutcome(exit_requested=False, session=current)
        new_session = new_cli_session()
        print(f"已开始新会话: {session_short_id(new_session)} ({new_session.canonical})")
        return CommandOutcome(exit_requested=False, session=new_session)

    if name == "/sessions":
        include_archived = "--active" not in args
        sessions = await app.conversation.list_sessions(include_archived=include_archived)
        if "--active" in args:
            sessions = [item for item in sessions if not item.archived]
        if "--archived" in args:
            sessions = [item for item in sessions if item.archived]
        _print_sessions(sessions, current)
        return CommandOutcome(exit_requested=False, session=current)

    if name == "/clear":
        purge = "--purge" in args
        tokens = [item for item in args if item != "--purge"]
        if len(tokens) > 1:
            _usage_error(command)
            return CommandOutcome(exit_requested=False, session=current)

        target: Optional[SessionSummary]
        if tokens:
            target = await _resolve_session(app, tokens[0])
            if target is None:
                return CommandOutcome(exit_requested=False, session=current)
        else:
            current_summaries = [
                item
                for item in await app.conversation.list_sessions(include_archived=True)
                if item.storage_id == current.storage_id
            ]
            target = current_summaries[0] if current_summaries else None

        if target is None:
            # 当前会话尚未写入任何消息(空会话): 直接切换新会话即可
            new_session = new_cli_session()
            print(f"当前会话为空, 已开始新会话: {session_short_id(new_session)}")
            return CommandOutcome(exit_requested=False, session=new_session)

        label = f"{target.short_id}({target.channel})"
        if purge:
            await app.conversation.purge_session(target.session_key)
            print(f"已永久删除会话: {label}")
        else:
            if target.archived:
                print(f"会话已处于归档状态: {label}")
            else:
                await app.conversation.archive_session(target.session_key)
                print(f"已归档会话: {label}")

        if target.storage_id == current.storage_id:
            new_session = new_cli_session()
            print(f"已开始新会话: {session_short_id(new_session)} ({new_session.canonical})")
            return CommandOutcome(exit_requested=False, session=new_session)
        return CommandOutcome(exit_requested=False, session=current)

    _usage_error(command)
    return CommandOutcome(exit_requested=False, session=current)


# ------------------------------------------------------------------ 交互循环


async def interactive_loop(app: Application, session_key: Optional[SessionKey] = None) -> None:
    """
    命令行交互循环

    :param app: 已装配好的 Application
    :param session_key: 当前会话; None 时新建一个进程级 CLI 会话
    """
    current = session_key or new_cli_session()
    print(
        "输入内容开始对话. 命令: /help 帮助, /sessions 会话, /new 新会话, /clear 归档, /exit 退出"
    )
    print("-" * 62)

    while True:
        try:
            user_input = input(PROMPT).strip()
        except KeyboardInterrupt:  # 提示符处 Ctrl+C: 直接优雅退出
            print("\n(已按下 Ctrl+C) 再见!")
            return
        except EOFError:  # Ctrl+D / 管道输入结束
            print("\n(输入已结束) 再见!")
            return

        if not user_input:
            continue

        if user_input.startswith("/"):
            outcome = await _handle_command(user_input, app, current)
            current = outcome.session
            if outcome.exit_requested:
                return
            continue

        result = await app.conversation.handle_message(
            current,
            user_input,
            meta={"channel": "cli", "scope": "session"},
        )
        if result.completed and not result.persisted:
            print("\n[提示] 本轮回答未能写入记忆(fail-soft), 下次上下文可能不包含本轮.")
        print(f"\n{APP_NAME} > {result.answer}")


# ------------------------------------------------------------------ 入口


def main(argv: Optional[list[str]] = None) -> int:
    """
    程序入口: 打印 banner -> 组装 Application -> 启动交互循环

    :param argv: 预留的命令行参数(当前无参数, 便于测试与后续扩展)
    :return: 进程退出码
    """
    _ = argv
    print(BANNER)

    try:
        app = build_application()
    except ConfigError as exc:
        print(f"[启动失败] {exc}")
        print("  1) 在 .env 里设置: api_key=${DEEPSEEK_API_KEY}")
        print("  2) 或直接导出系统环境变量: export DEEPSEEK_API_KEY=sk-xxx")
        return 1
    except SkillConfigError as exc:
        print(f"[启动失败] 技能配置错误: {exc}")
        return 1
    except MemoryStoreError as exc:
        print(f"[启动失败] 记忆存储初始化失败: {exc}")
        return 1

    if not IDENTITY_FILE.is_file():
        print(f"[启动警告] 未找到人设文件 {IDENTITY_FILE}, 将使用内置默认人设")
    if len(app.catalog):
        print(f"发现 {len(app.catalog)} 个技能: {app.catalog.root}")
    else:
        print("[启动警告] 未发现内置技能, load_skill 不会注册")

    session = new_cli_session()
    _print_startup_info(app.config, session)
    _print_tools(app.registry)

    try:
        asyncio.run(interactive_loop(app, session))
    except KeyboardInterrupt:  # 回答生成过程中 Ctrl+C: 不打印堆栈
        print("\n已中断, 再见!")
    return 0
