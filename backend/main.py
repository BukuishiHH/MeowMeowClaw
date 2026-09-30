"""项目入口: 组装 Provider / 工具 / Context / AgentLoop, 并提供命令行交互循环.

运行方式(二选一)::

    python -m backend.main      # 推荐
    python backend/main.py      # 直接运行也可以(文件内已补齐 sys.path)

交互命令::

    /exit   退出(也可 /quit)
    /clear  清空对话历史与工具调用记录
    /tools  查看已注册工具
"""

import asyncio
import sys
from pathlib import Path

# 支持 `python backend/main.py` 直接运行: 把项目根目录加入 sys.path,
# 使下面的 `from backend.xxx import ...` 能被解析(推荐用法仍是 python -m backend.main)
if __package__ in (None, ""):  # pragma: no cover - 仅直接运行脚本时进入
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.agent.context import ContextBuilder  # noqa: E402
from backend.agent.loop import AgentLoop  # noqa: E402
from backend.agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool  # noqa: E402
from backend.agent.tools.shell import ExecTool
from backend.agent.tools.web_search import WebSearchTool
from backend.agent.tools.web_fetch import WebFetchTool
from backend.agent.tools.registry import ToolRegistry  # noqa: E402
from backend.config import Settings, load_config  # noqa: E402
from backend.providers.openai_compat import OpenAICompatProvider  # noqa: E402

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


# ------------------------------------------------------------------ 组装


def build_agent() -> AgentLoop:
    """
    读取配置并组装出可用的 AgentLoop

    :return: 装配完成的 AgentLoop 实例; 缺少 api_key 时直接退出进程(退出码 1)
    """
    config: Settings = load_config()

    if not config.api_key:
        print("[启动失败] 未读取到 api_key, 无法调用模型.")
        print(f"  1) 在 {config.source} 里设置: api_key=${{DEEPSEEK_API_KEY}}")
        print("  2) 或直接导出系统环境变量: export DEEPSEEK_API_KEY=sk-xxx")
        sys.exit(1)

    provider = OpenAICompatProvider(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
    )

    tools = ToolRegistry()
    tools.register(ReadFileTool(config.workspace))
    tools.register(WriteFileTool(config.workspace))
    tools.register(ListDirTool(config.workspace))
    tools.register(ExecTool(config.workspace))
    tools.register(WebSearchTool())
    tools.register(WebFetchTool())

    context = ContextBuilder(config.workspace, config.identity_file)

    agent = AgentLoop(
        provider=provider,
        tools=tools,
        context=context,
        model=config.model,
        max_iterations=config.max_iterations,
    )

    _print_startup_info(config)
    _print_tools(tools)
    return agent


def _print_startup_info(config: Settings) -> None:
    """打印一行式启动信息(api_key 已由 Settings.__repr__ 掩码, 这里不打印密钥)."""
    print(f"  模型      : {config.model}")
    print(f"  接口地址  : {config.base_url}")
    print(f"  工作目录  : {config.workspace}")
    print(f"  人设文件  : {config.identity_file}")
    print(f"  最大迭代  : {config.max_iterations}")
    print(f"  配置文件  : {config.source}")


def _print_tools(tools: ToolRegistry) -> None:
    """打印已注册工具(名称 + 描述, 描述给模型看, 这里也方便人排查)."""
    definitions = tools.get_definitions()
    print(f"已注册工具({len(definitions)} 个):")
    for definition in definitions:
        function = definition.get("function", {})
        print(f"  - {function.get('name')}: {function.get('description', '')}")


# ------------------------------------------------------------------ 交互循环


def _handle_command(command: str, agent: AgentLoop) -> bool:
    """
    处理 "/" 开头的命令

    :return: True 表示需要退出交互循环
    """
    name = command.strip().lower()

    if name in EXIT_COMMANDS:
        print("再见!")
        return True
    if name == "/clear":
        agent.clear_history()
        print("已清空对话历史与工具调用记录.")
        return False
    if name == "/tools":
        _print_tools(agent.tools)
        return False

    print(f"未知命令: {command}. 可用命令: /exit 退出, /clear 清空历史, /tools 查看工具")
    return False


async def interactive_loop(agent: AgentLoop) -> None:
    """
    命令行交互循环

    :param agent: 已组装好的 AgentLoop
    """
    print("输入内容开始对话. 命令: /exit 退出, /clear 清空历史, /tools 查看工具")
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
            if _handle_command(user_input, agent):
                return
            continue

        try:
            answer = await agent.run(user_input)
        except Exception as exc:  # noqa: BLE001 交互层兜底, 单次失败不该终止会话
            print(f"\n[异常] 本轮处理失败: {exc!r}")
            continue
        except KeyboardInterrupt:  # pragma: no cover - 由 main() 兜底
            print("\n(已中断本轮回答)")
            continue

        print(f"\n{APP_NAME} > {answer}")


# ------------------------------------------------------------------ 入口


def main() -> None:
    """程序入口: 打印 banner -> 组装 Agent -> 启动交互循环."""
    print(BANNER)
    agent = build_agent()
    try:
        asyncio.run(interactive_loop(agent))
    except KeyboardInterrupt:  # 回答生成过程中 Ctrl+C: 不打印堆栈
        print("\n已中断, 再见!")


if __name__ == "__main__":
    main()
