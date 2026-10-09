"""组合根: 读取配置并装配出可直接运行的 Application.

设计约束:
    - 本模块是**唯一** import 具体实现(Provider / SkillCatalog / 具体工具)的地方;
    - 只使用 logging, 不做用户可见输出(print 属于交付层 cli);
    - 缺少启动必需配置时抛 ``ConfigError``; 技能资源冲突抛 ``SkillConfigError``,
      两者都由交付层统一展示并决定退出码.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from meowmeowclaw.agent.compression import ContextCompressor
from meowmeowclaw.agent.context import ContextBuilder
from meowmeowclaw.agent.loop import AgentLoop
from meowmeowclaw.config import Settings, load_config
from meowmeowclaw.conversation import ConversationService
from meowmeowclaw.llm.base import LLMProvider
from meowmeowclaw.llm.openai_compat import OpenAICompatProvider
from meowmeowclaw.llm.tokenizer import build_counter
from meowmeowclaw.memory import JsonlSessionStore, SessionKey
from meowmeowclaw.paths import IDENTITY_FILE
from meowmeowclaw.skills import LoadSkillTool, SkillCatalog
from meowmeowclaw.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """启动所需配置缺失或不可用(如 api_key 为空)."""


@dataclass(frozen=True)
class Application:
    """装配完成、可供交付层直接使用的运行时对象集合."""

    config: Settings
    provider: LLMProvider
    registry: ToolRegistry
    catalog: SkillCatalog
    context: ContextBuilder
    session_store: JsonlSessionStore
    conversation: ConversationService

    async def close(self) -> None:
        """释放会话缓存与存储资源(交付层退出时调用)."""
        await self.conversation.close()
        await self.session_store.close()


def build_registry(config: Settings) -> ToolRegistry:
    """集中注册清单: 唯一知道"有哪些具体工具"的地方.

    具体工具只在函数内 import, 因此 ``import meowmeowclaw.bootstrap`` 不会拉起全部工具实现。
    """
    from meowmeowclaw.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
    from meowmeowclaw.tools.shell import ExecTool
    from meowmeowclaw.tools.web_fetch import WebFetchTool
    from meowmeowclaw.tools.web_search import WebSearchTool

    registry = ToolRegistry()
    registry.register(ReadFileTool(config.workspace, config.memory_dir))
    registry.register(WriteFileTool(config.workspace, config.memory_dir))
    registry.register(ListDirTool(config.workspace, config.memory_dir))
    registry.register(ExecTool(config.workspace))
    registry.register(WebSearchTool())
    registry.register(WebFetchTool())
    return registry


def ensure_workspace(workspace: Path) -> bool:
    """创建工作目录; 失败只记录 warning 并返回 False, 不阻断启动."""
    try:
        Path(workspace).mkdir(parents=True, exist_ok=True)
        return True
    except OSError as exc:
        logger.warning("创建工作目录失败: %s (%r)", workspace, exc)
        return False


def build_application(env_file: Optional[Union[str, Path]] = None) -> Application:
    """
    读取配置并完成全部装配

    :param env_file: 可选的自定义 .env 路径(默认 ``paths.ENV_FILE``)
    :return: 装配完成的 Application
    :raises ConfigError: 缺少 api_key
    :raises SkillConfigError: 内置技能重名等资源错误
    """
    config = load_config(env_file)
    if not config.api_key:
        raise ConfigError(f"未读取到 api_key (配置文件: {config.source})")

    ensure_workspace(config.workspace)

    provider = OpenAICompatProvider(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
    )
    registry = build_registry(config)
    catalog = SkillCatalog()

    skills_summary = catalog.summary()
    if skills_summary:
        # 有技能才注册 load_skill: 没技能时不该给模型一个必然失败的工具
        registry.register(LoadSkillTool(catalog))
        logger.info("发现 %d 个技能: %s", len(catalog), catalog.root)
    else:
        logger.warning("未发现内置技能, load_skill 不会注册")

    context = ContextBuilder(
        config.workspace,
        IDENTITY_FILE,
        skills_summary=skills_summary,
        memory_path=config.memory_dir / "MEMORY.md",
    )

    # token 计数器无状态, 全局构造一次即可(精确档 lazy 加载, 失败自动回退启发式);
    # 关闭压缩时不构造, 避免无谓的可选依赖探测
    counter = None
    if config.compression_enabled:
        counter = build_counter(
            config.model,
            tokenizer=config.tokenizer,
            hf_tokenizer_path=config.hf_tokenizer_path,
        )

    # 每个会话一个 AgentLoop: 工具调用护栏与摘要缓存按会话隔离; 共享 Provider/Registry/Context
    def agent_factory(session_key: SessionKey) -> AgentLoop:
        compressor = None
        if config.compression_enabled:
            compressor = ContextCompressor(
                counter=counter,
                token_budget=config.token_budget,
                keep_recent_turns=config.keep_recent_turns,
                provider=provider,
                model=config.model,
                summary_model=config.summary_model,
                summary_max_tokens=config.summary_max_tokens,
                summary_timeout=config.summary_timeout,
                session=session_key.canonical,
                # audit_log: P4 接入 HistoryAuditLog
            )
        return AgentLoop(
            provider=provider,
            tools=registry,
            context=context,
            model=config.model,
            max_iterations=config.max_iterations,
            compressor=compressor,
        )

    session_store = JsonlSessionStore(config.memory_dir)
    conversation = ConversationService(
        session_store,
        agent_factory,
        max_turns=config.memory_max_turns,
        max_chars=config.memory_max_chars,
    )

    return Application(
        config=config,
        provider=provider,
        registry=registry,
        catalog=catalog,
        context=context,
        session_store=session_store,
        conversation=conversation,
    )
