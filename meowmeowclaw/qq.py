"""QQ 渠道(NapCat / OneBot 11)进程入口.

用法::

    python -m meowmeowclaw.qq                  # 启动 QQ 渠道网关
    meowmeowclaw-qq                            # 安装后等价入口
    python -m meowmeowclaw.qq --check          # 只校验配置并打印将使用的参数
    python -m meowmeowclaw.qq --log-level DEBUG

前置条件:

1. ``.env`` 里 ``gateway_enabled=true`` 且配置好 ``qq_*`` 组(见 ``.env.example``);
2. NapCat 已登录机器人 QQ, 并在「网络配置 → OneBot 11」里配好 WS 网络
   (``qq_ws_mode=reverse`` 时 NapCat 作为客户端连到本进程监听地址)。

行为: 仅响应 ``qq_owner_id`` 的私聊消息; 其他人/群聊由 ``QqPolicy`` 忽略;
``/help /new /clear /sessions`` 走策略直回, 不消耗模型调用。
"""

import argparse
import asyncio
import logging
import signal
from contextlib import suppress
from typing import Optional, Sequence

from meowmeowclaw.bootstrap import Application, ConfigError, build_application
from meowmeowclaw.channels.onebot11 import OneBot11Adapter, OneBot11Transport
from meowmeowclaw.channels.qq_policy import QqPolicy
from meowmeowclaw.channels.qq_private import QqPrivateActiveStore
from meowmeowclaw.config import Settings
from meowmeowclaw.gateway import Gateway
from meowmeowclaw.memory import MemoryStoreError
from meowmeowclaw.skills import SkillConfigError

logger = logging.getLogger(__name__)

BANNER = r"""
    /\_/\   MeowMeowClaw · QQ 渠道 (NapCat / OneBot 11)
   ( • w • ) 仅响应主人私聊, Ctrl+C 优雅退出
    >     <
"""

# 运行期状态上报间隔(秒); 便于长时间运行时观察队列/死信
STATS_INTERVAL_SECONDS = 300.0


def qq_config_problems(config: Settings) -> list[str]:
    """校验 QQ 渠道启动配置; 返回人类可读的问题列表(空列表 = 可启动)."""
    problems: list[str] = []
    if not config.gateway_enabled:
        problems.append("gateway_enabled=false; 请在 .env 中设置 gateway_enabled=true")
    owner = str(config.qq_owner_id or "").strip()
    if not owner:
        problems.append("qq_owner_id 未配置; 请在 .env 中填写主人 QQ 号(仅该号私聊会被响应)")
    elif not owner.isdigit():
        problems.append(f"qq_owner_id 不是纯数字 QQ 号: {owner!r}")
    return problems


def build_transport(config: Settings) -> OneBot11Transport:
    """按配置构造 OneBot11 传输(不启动)."""
    return OneBot11Transport(
        mode=config.qq_ws_mode,
        host=config.qq_ws_host,
        port=config.qq_ws_port,
        path=config.qq_ws_path,
        token=config.qq_token,
        action_timeout=config.qq_action_timeout,
        max_text_chars=config.qq_message_chunk_chars,
    )


def _mask_token(token: str) -> str:
    text = str(token or "")
    if not text:
        return "(未设置)"
    return f"{text[:4]}***" if len(text) > 4 else "***"


def _print_startup(config: Settings, transport: OneBot11Transport) -> None:
    direction = "反向(本进程监听, NapCat 主动连入)" if transport.mode == "reverse" else "正向(本进程连接 NapCat)"
    print(f"  QQ 渠道   : NapCat / OneBot 11 · {direction}")
    print(f"  端点      : {transport.endpoint}")
    print(f"  令牌      : {_mask_token(transport.token)}")
    print(f"  主人 QQ   : {config.qq_owner_id}")
    print(f"  QQ 流式   : {config.qq_stream_mode} (typing={'开' if config.qq_typing_indicator else '关'})")
    print(f"  模型      : {config.model}")
    print(f"  记忆目录  : {config.memory_dir}")
    print("-" * 62)
    if transport.mode == "reverse":
        print("等待 NapCat 连接(Ctrl+C 退出)...")
    else:
        print("正在连接 NapCat(Ctrl+C 退出)...")


def _print_check(config: Settings, transport: OneBot11Transport) -> None:
    print("配置检查通过, 将使用以下参数:")
    print(f"  gateway_enabled        : {config.gateway_enabled}")
    print(f"  qq_owner_id            : {config.qq_owner_id}")
    print(f"  qq_ws_mode             : {config.qq_ws_mode}")
    print(f"  qq_ws_host / port      : {config.qq_ws_host} / {config.qq_ws_port}")
    print(f"  qq_ws_path             : {config.qq_ws_path}")
    print(f"  qq_token               : {_mask_token(config.qq_token)}")
    print(f"  qq_action_timeout      : {config.qq_action_timeout}")
    print(f"  qq_message_chunk_chars : {config.qq_message_chunk_chars}")
    print(f"  qq_stream_mode         : {config.qq_stream_mode}")
    print(f"  qq_typing_indicator    : {config.qq_typing_indicator}")
    print(f"  qq_typing_refresh_s    : {config.qq_typing_refresh_seconds}")
    print(
        "  qq_stream_*            : "
        f"first={config.qq_stream_first_chars} min={config.qq_stream_min_chars} "
        f"interval={config.qq_stream_min_interval} max={config.qq_stream_max_messages} "
        f"hard={config.qq_stream_hard_chars}"
    )
    print(f"  stream_enabled         : {config.stream_enabled}")
    print(f"  端点                   : {transport.endpoint}")
    print(f"  记忆目录               : {config.memory_dir}")


async def _stats_reporter(gateway: Gateway, interval: float = STATS_INTERVAL_SECONDS) -> None:
    """定期把网关健康快照写进日志(队列积压/在途/死信)."""
    while True:
        await asyncio.sleep(interval)
        logger.info("网关状态: %s", gateway.stats())


async def run_qq(
    app: Application,
    *,
    transport: Optional[OneBot11Transport] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """装配 QQ 渠道并运行到收到 SIGINT/SIGTERM; 退出时优雅关闭网关与记忆存储."""
    gateway = app.gateway
    if gateway is None:  # 调用方已用 qq_config_problems 拦截
        raise RuntimeError("gateway 未装配: gateway_enabled 必须为 true")

    config = app.config
    transport = transport or build_transport(config)
    adapter = OneBot11Adapter(
        transport,
        stream_mode=config.qq_stream_mode,
        typing_enabled=config.qq_typing_indicator,
        typing_refresh_seconds=config.qq_typing_refresh_seconds,
        block_first_chars=config.qq_stream_first_chars,
        block_min_chars=config.qq_stream_min_chars,
        block_min_interval=config.qq_stream_min_interval,
        block_max_messages=config.qq_stream_max_messages,
        block_hard_chars=config.qq_stream_hard_chars,
    )
    policy = QqPolicy(app.conversation, QqPrivateActiveStore(config.memory_dir), config.qq_owner_id)

    stop_event = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)

    gateway.add_policy(policy)
    try:
        await gateway.start(adapters=[adapter])
        _print_startup(config, transport)
        stats_task = asyncio.create_task(_stats_reporter(gateway), name="qq-stats")
        try:
            await stop_event.wait()
        finally:
            stop_event.set()
            stats_task.cancel()
            with suppress(asyncio.CancelledError):
                await stats_task
    finally:
        await app.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(sig)
    print("已停止, 再见喵!")


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="meowmeowclaw-qq",
        description="MeowMeowClaw QQ 渠道入口(NapCat / OneBot 11)",
    )
    parser.add_argument("--check", action="store_true", help="只校验配置并打印参数, 不连接 NapCat")
    parser.add_argument(
        "--log-level",
        default="INFO",
        type=str.upper,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别(默认 INFO)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """QQ 渠道入口: 配置校验 -> 装配 -> 启动网关 -> 等待信号 -> 优雅关闭."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
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

    problems = qq_config_problems(app.config)
    if problems:
        print("[启动失败] QQ 渠道配置不完整:")
        for problem in problems:
            print(f"  - {problem}")
        print("  参考 .env.example 的「QQ 渠道(NapCat / OneBot 11)」小节。")
        return 2

    transport = build_transport(app.config)
    if not app.config.qq_token:
        print("[启动警告] qq_token 为空: 任何能访问监听端口的本机进程都可冒充 NapCat。")

    if args.check:
        _print_check(app.config, transport)
        return 0

    try:
        asyncio.run(run_qq(app, transport=transport))
    except KeyboardInterrupt:  # 信号处理器安装前按下 Ctrl+C 的兜底
        print("\n已中断, 再见喵!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
