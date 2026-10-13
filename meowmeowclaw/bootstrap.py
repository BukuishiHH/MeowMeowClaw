"""组合根: 读取配置并装配出可直接运行的 Application.

设计约束:
    - 本模块是**唯一** import 具体实现(Provider / SkillCatalog / 具体工具)的地方;
    - 只使用 logging, 不做用户可见输出(print 属于交付层 cli);
    - 缺少启动必需配置时抛 ``ConfigError``; 技能资源冲突抛 ``SkillConfigError``,
      两者都由交付层统一展示并决定退出码.
"""

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional, Union

from meowmeowclaw.agent.audit import HistoryAuditLog
from meowmeowclaw.agent.compression import ContextCompressor
from meowmeowclaw.agent.context import ContextBuilder
from meowmeowclaw.agent.loop import AgentLoop
from meowmeowclaw.config import Settings, load_config
from meowmeowclaw.conversation import ConversationService
from meowmeowclaw.gateway import Gateway
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
    gateway: Optional[Gateway] = None

    async def close(self) -> None:
        """释放网关/会话缓存与存储资源(交付层退出时调用)."""
        if self.gateway is not None:
            await self.gateway.stop()
        await self.conversation.close()
        await self.session_store.close()


def build_registry(
    config: Settings, *, mutation_guard: Optional[Any] = None
) -> ToolRegistry:
    """集中注册清单: 唯一知道"有哪些具体工具"的地方.

    具体工具只在函数内 import, 因此 ``import meowmeowclaw.bootstrap`` 不会拉起全部工具实现。
    ``mutation_guard`` 仅在 SA3 子代理开启时注入 WriteFileTool。
    """
    from meowmeowclaw.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
    from meowmeowclaw.tools.shell import ExecTool
    from meowmeowclaw.tools.web_fetch import WebFetchTool
    from meowmeowclaw.tools.web_search import WebSearchTool

    registry = ToolRegistry()
    registry.register(ReadFileTool(config.workspace, config.memory_dir))
    registry.register(
        WriteFileTool(
            config.workspace,
            config.memory_dir,
            mutation_guard=mutation_guard,
        )
    )
    registry.register(ListDirTool(config.workspace, config.memory_dir))
    registry.register(ExecTool(config.workspace, mutation_guard=mutation_guard))
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


def build_gateway(application: Application) -> Optional[Gateway]:
    """按配置构造 Gateway(不启动, 需在事件循环中 ``await gateway.start()``).

    - ``application.gateway`` 已存在时直接复用;
    - ``gateway_enabled=false`` 时返回 None, 交付层行为与旧直连路径一致。
    """
    if application.gateway is not None:
        return application.gateway
    if not application.config.gateway_enabled:
        return None
    return Gateway(
        conversation=application.conversation,
        bus_maxsize=application.config.gateway_bus_maxsize,
        publish_timeout=application.config.gateway_publish_timeout,
        shutdown_timeout=application.config.gateway_shutdown_timeout,
        stream_enabled=application.config.stream_enabled,
    )


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

    # SA3: 子代理开启且未关闭锁时, 为主/子代理的 write 注入同一个 workspace 变更锁;
    # 关闭子代理时 mutation_guard=None, 主代理行为与旧版逐字节一致。
    mutation_guard: Optional[Any] = None
    if config.subagent_enabled and config.workspace_mutation_lock_enabled:
        from meowmeowclaw.tools.mutation import WorkspaceMutationGuard

        mutation_guard = WorkspaceMutationGuard(
            config.memory_dir / "subagents" / "workspace.lock",
            timeout=config.workspace_mutation_lock_timeout,
        )

    registry = build_registry(config, mutation_guard=mutation_guard)
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

    # 子代理(SA2–SA5): 默认关闭; 开启时构造 factory 并把 depth0 spawn 工具注册给主代理。
    # write OCC + exec 策略/审计均已装配; 子代理统计通过 ConversationService.stats() 暴露。
    subagent_stats = None
    if config.subagent_enabled:
        from meowmeowclaw.subagent.audit import SubagentAuditWriter
        from meowmeowclaw.subagent.factory import SubagentFactory
        from meowmeowclaw.subagent.models import SubagentStats

        audit_writer = (
            SubagentAuditWriter(
                config.memory_dir / "subagents" / "audit.jsonl",
                max_bytes=config.subagent_audit_max_bytes,
            )
            if config.subagent_audit_enabled
            else None
        )
        subagent_stats = SubagentStats(enabled=True)
        subagent_factory = SubagentFactory(
            provider=provider,
            base_registry=registry,
            workspace=config.workspace,
            enabled=True,
            max_depth=config.subagent_max_depth,
            model=config.subagent_model or None,
            max_iterations=config.subagent_max_iterations,
            timeout_seconds=config.subagent_timeout_seconds,
            result_max_chars=config.subagent_result_max_chars,
            max_children_per_turn=config.subagent_max_children_per_turn,
            max_total_per_turn=config.subagent_max_total_per_turn,
            tool_names=config.subagent_tools,
            read_only=False,
            allow_exec=True,  # SA4: exec 在 guarded/allowlist 策略下开放
            exec_policy=config.subagent_exec_policy,
            exec_allowlist=config.subagent_exec_allowlist,
            exec_env_allowlist=config.subagent_exec_env_allowlist,
            sensitive_paths=config.subagent_sensitive_paths,
            audit_writer=audit_writer,
            stats=subagent_stats,
        )
        registry.register(subagent_factory.make_root_tool())
        logger.info("子代理已装配: %r (SA4: write OCC + exec 策略/审计)", subagent_factory)

    # token 计数器无状态, 全局构造一次即可(精确档 lazy 加载, 失败自动回退启发式);
    # 关闭压缩时不构造, 避免无谓的可选依赖探测
    counter = None
    audit_log = None
    if config.compression_enabled:
        counter = build_counter(
            config.model,
            tokenizer=config.tokenizer,
            hf_tokenizer_path=config.hf_tokenizer_path,
        )
        # HISTORY.md 只为人工校验, 不读回、不注入 Prompt; 写入失败 fail-soft
        audit_log = HistoryAuditLog(
            config.memory_dir / "HISTORY.md",
            max_bytes=config.history_log_max_bytes,
            original_chars=config.history_log_original_chars,
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
                storage_id=session_key.storage_id,
                audit_log=audit_log,
            )
        return AgentLoop(
            provider=provider,
            tools=registry,
            context=context,
            model=config.model,
            max_iterations=config.max_iterations,
            compressor=compressor,
            # 流式前导抑制参数(生产路径由 .env 注入, 见 docs/STREAMING_DESIGN.md §4.3)
            stream_preamble_grace_ms=config.stream_preamble_grace_ms,
            stream_preamble_grace_chars=config.stream_preamble_grace_chars,
            suppress_tool_preamble=config.stream_suppress_tool_preamble,
        )

    session_store = JsonlSessionStore(config.memory_dir)
    conversation = ConversationService(
        session_store,
        agent_factory,
        max_turns=config.memory_max_turns,
        max_chars=config.memory_max_chars,
        max_cached_sessions=config.memory_max_cached_sessions,
        cache_idle_seconds=config.memory_cache_idle_seconds,
        subagent_stats=subagent_stats,
    )

    application = Application(
        config=config,
        provider=provider,
        registry=registry,
        catalog=catalog,
        context=context,
        session_store=session_store,
        conversation=conversation,
    )
    gateway = build_gateway(application)
    if gateway is not None:
        application = replace(application, gateway=gateway)
    return application
