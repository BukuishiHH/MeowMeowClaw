"""命令行交付层: banner / 启动信息 / REPL / 命令 / 用户可见输出.

只依赖 ``bootstrap`` 与契约类型, 不直接 import 任何具体 Provider / 工具实现;
因此更换交付形态(如将来加 Web API)时, 组合根可以原样复用。
"""

import asyncio
from typing import Optional

from meowmeowclaw.bootstrap import Application, ConfigError, build_application
from meowmeowclaw.config import Settings
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


# ------------------------------------------------------------------ 启动信息


def _print_startup_info(config: Settings) -> None:
    """打印一行式启动信息(api_key 已由 Settings.__repr__ 掩码, 这里不打印密钥)."""
    print(f"  模型      : {config.model}")
    print(f"  接口地址  : {config.base_url}")
    print(f"  工作目录  : {config.workspace}")
    print(f"  人设文件  : {IDENTITY_FILE}")
    print(f"  最大迭代  : {config.max_iterations}")
    print(f"  配置文件  : {config.source}")


def _print_tools(tools: ToolRegistry) -> None:
    """打印已注册工具(名称 + 描述, 描述给模型看, 这里也方便人排查)."""
    definitions = tools.get_definitions()
    print(f"已注册工具({len(definitions)} 个):")
    for definition in definitions:
        function = definition.get("function", {})
        print(f"  - {function.get('name')}: {function.get('description', '')}")


def _print_skills(catalog: SkillCatalog) -> None:
    """打印已发现技能(名 + 描述 + 来源), 供人排查. """
    print(f"已发现技能({len(catalog)} 个):")
    for skill in catalog.skills():
        print(f"  - {skill.name} ({skill.dir_name}/SKILL.md): {skill.description}")


# ------------------------------------------------------------------ 交互循环


def _handle_command(command: str, app: Application) -> bool:
    """
    处理 "/" 开头的命令

    :return: True 表示需要退出交互循环
    """
    name = command.strip().lower()

    if name in EXIT_COMMANDS:
        print("再见喵!")
        return True
    if name == "/clear":
        app.agent.clear_history()
        print("已清空对话历史与工具调用记录.")
        return False
    if name == "/tools":
        _print_tools(app.registry)
        return False
    if name == "/skills":
        _print_skills(app.catalog)
        return False

    print(
        f"未知命令: {command}. 可用命令: "
        "/exit 退出, /clear 清空历史, /tools 查看工具, /skills 查看技能"
    )
    return False


async def interactive_loop(app: Application) -> None:
    """
    命令行交互循环

    :param app: 已装配好的 Application
    """
    print("输入内容开始对话. 命令: /exit 退出, /clear 清空历史, /tools 查看工具, /skills 查看技能")
    print("-" * 62)

    while True:
        try:
            user_input = input(PROMPT).strip()
        except KeyboardInterrupt:  # 提示符处 Ctrl+C: 直接优雅退出
            print("\n(已按下 Ctrl+C) 再见喵!")
            return
        except EOFError:  # Ctrl+D / 管道输入结束
            print("\n(输入已结束) 再见喵!")
            return

        if not user_input:
            continue

        if user_input.startswith("/"):
            if _handle_command(user_input, app):
                return
            continue

        try:
            answer = await app.agent.run(user_input)
        except Exception as exc:  # noqa: BLE001 交互层兜底, 单次失败不该终止会话
            print(f"\n[异常] 本轮处理失败: {exc!r}")
            continue
        except KeyboardInterrupt:  # pragma: no cover - 由 main() 兜底
            print("\n(已中断本轮回答)")
            continue

        print(f"\n{APP_NAME} > {answer}")


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

    if not IDENTITY_FILE.is_file():
        print(f"[启动警告] 未找到人设文件 {IDENTITY_FILE}, 将使用内置默认人设")
    if len(app.catalog):
        print(f"发现 {len(app.catalog)} 个技能: {app.catalog.root}")
    else:
        print("[启动警告] 未发现内置技能, load_skill 不会注册")

    _print_startup_info(app.config)
    _print_tools(app.registry)

    try:
        asyncio.run(interactive_loop(app))
    except KeyboardInterrupt:  # 回答生成过程中 Ctrl+C: 不打印堆栈
        print("\n已中断, 再见喵!")
    return 0
