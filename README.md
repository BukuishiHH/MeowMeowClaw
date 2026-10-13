# MeowMeowClaw

[![CI](https://github.com/BukuishiHH/MeowMeowClaw/actions/workflows/ci.yml/badge.svg)](https://github.com/BukuishiHH/MeowMeowClaw/actions/workflows/ci.yml)

依据 OpenClaw 思路实现的自定义 Agent -- **不依赖 LangChain / LangGraph 等编排框架**, 用一个显式的"模型 ↔ 工具"循环驱动.

- **技术栈**: Python 3.10+; 运行时依赖 `openai`(AsyncOpenAI) / `python-dotenv` / `httpx` / `pyyaml` / `websockets`, 联网工具另需 `ddgs`、`html2text`
- **代码规模**: 49 个源码模块 / 约 11000 行; 测试 52 个文件 / **1564 个用例**(1556 passed + 6 skipped + 2 xfailed)
- **渠道**: CLI(直连或网关) + QQ(**NapCat / OneBot 11 反向 WebSocket**, 仅主人私聊); 支持**流式输出**(CLI 逐字 / QQ "正在输入" / QQ 渐进式气泡)
- **协议**: OpenAI Chat Completions + function calling, 任何兼容服务(DeepSeek / 通义 / vLLM / Ollama / One-API)改 `base_url` 即可接入

---

## 1. 快速开始

```bash
pip install -e ".[dev]"                  # 安装依赖 + 开发工具; 仅运行可去掉 [dev]

cp .env.example .env
export DEEPSEEK_API_KEY=sk-xxxx          # .env 里用 ${DEEPSEEK_API_KEY} 引用
python -m meowmeowclaw                   # 安装后也可以直接运行 meowmeowclaw
```

### 交互命令

| 命令 | 作用 |
| --- | --- |
| 直接输入 | 交给 Agent 处理 |
| `/exit`(`/quit` `/q`) | 退出 |
| `/help` | 查看命令帮助 |
| `/new` | 新建会话(旧会话保留在 `sessions/`) |
| `/sessions [--active\|--archived]` | 列出会话与短 ID |
| `/clear` | 归档当前会话并新建 |
| `/clear <会话ID>` | 归档指定会话 |
| `/clear <会话ID> --purge` | 永久删除指定会话 |
| `/tools` | 查看已注册工具 |
| `/skills` | 查看已发现的内置技能 |

### 配置项(`.env`, 与 `meowmeowclaw/` 同级)

优先级: **系统环境变量 > .env 文件 > 代码默认值**. 相对路径一律按**项目根**解析, 不随当前工作目录漂移.

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `model` | `deepseek-chat` | 模型名 |
| `api_key` | 空(启动即提示并退出码 1) | 支持 `${ENV_VAR}` 引用系统环境变量 |
| `base_url` | `https://api.deepseek.com` | OpenAI 兼容服务地址 |
| `workspace` | `<项目根>/workspace` | 留空或写 `.` 均表示该预设; 可写绝对路径覆盖; 启动装配时自动创建 |
| `max_iterations` | `32` | 单轮"模型↔工具"往返上限; 非法值回退默认 |
| `memory_dir` | `<workspace>/memory` | 记忆存储目录; 留空取默认; 相对路径按项目根解析 |
| `memory_max_turns` | `50` | 装载历史窗口的轮数上限; 非法或 <=0 回退默认 |
| `memory_max_chars` | `120000` | 装载历史窗口的字符上限(≈60k 中文 token); 非法或 <=0 回退默认 |
| `memory_max_cached_sessions` | `256` | 会话运行时(AgentLoop + 串行锁)缓存上限; `<=0` 不限制, 超限按 LRU 淘汰空闲会话 |
| `memory_cache_idle_seconds` | `0` | 空闲会话运行时自动淘汰秒数; `0` 关闭自动 TTL(仅容量 LRU + 显式清理) |
| `compression_enabled` | `true` | 上下文 Token 压缩总开关; `false` 时只按轮数/字符窗口装载历史 |
| `token_budget` | `48000` | 输入 Token 预算(估算 × 1.1 后比较); 超预算触发摘要/硬裁/工具占位 |
| `tokenizer` | `auto` | `auto` / `heuristic` / `tiktoken[:encoding]` / `hf` |
| `hf_tokenizer_path` | 空 | 本地 `tokenizer.json` 覆盖路径; 空时查 `<项目根>/tokenizers/<模型名>/tokenizer.json` |
| `keep_recent_turns` | `2` | 压缩时至少保留原文的最近完整轮数(下限 1) |
| `summary_model` | 空 | 摘要模型; 空 = 使用主 `model` |
| `summary_max_tokens` | `768` | 摘要输出上限 |
| `summary_timeout` | `15` | 摘要调用超时(秒) |
| `history_log_max_bytes` | `2097152` | `HISTORY.md` 轮转阈值(字节) |
| `history_log_original_chars` | `32000` | 单条审计记录原文 JSON 上限(字符) |
| `gateway_enabled` | `false` | 多渠道网关总开关; `true` 时 CLI/QQ 经 asyncio.Queue 总线(**QQ 渠道必须为 `true`**) |
| `gateway_bus_maxsize` | `1000` | 每个队列(inbound/outbound:<channel>/deadletter)容量 |
| `gateway_publish_timeout` | `5` | publish 背压等待秒数; 超时进死信并 warning |
| `gateway_shutdown_timeout` | `10` | 优雅关闭等待秒数(排空 dispatcher/in-flight/出站队列) |
| `qq_owner_id` | 空 | 主人 QQ 号; **仅该号私聊**会被响应, 其他人/群聊忽略 |
| `qq_ws_mode` | `reverse` | `reverse`=本进程监听, NapCat 连入; `forward`=本进程连接 NapCat |
| `qq_ws_host` | `127.0.0.1` | reverse=监听地址; forward=NapCat 地址 |
| `qq_ws_port` | `3100` | reverse=监听端口; forward=NapCat「WebSocket 服务器」端口 |
| `qq_ws_path` | `/onebot/v11/ws` | WebSocket 路径(缺前导 `/` 自动补齐) |
| `qq_token` | 空 | 与 NapCat 网络配置里的 Token 一致; 留空不校验(reverse 下不建议) |
| `qq_action_timeout` | `10` | 单次 OneBot 动作(发消息)等待响应秒数 |
| `qq_message_chunk_chars` | `3000` | 单条 QQ 消息字符上限, 超长回复自动分片 |
| `stream_enabled` | `false` | 流式输出总开关; `false` 时与旧版逐字节一致 |
| `cli_stream` | `true` | CLI 逐字输出(需 `stream_enabled=true`) |
| `qq_stream_mode` | `typing` | `off` / `typing`(仅"正在输入"+"1 条完整气泡) / `block`(渐进式多气泡) |
| `qq_typing_indicator` | `true` | 是否发送"对方正在输入"(NapCat `set_input_status`) |
| `qq_typing_refresh_seconds` | `5` | "正在输入"续期间隔(秒) |
| `qq_stream_first_chars` | `150` | `block` 模式首块触发下限(默认=target; 调小可更早见首块) |
| `qq_stream_min_chars` | `80` | `block` 模式单块硬下限 |
| `qq_stream_target_chars` | `150` | `block` 模式理想块大小(同级边界里取最贴近它的) |
| `qq_stream_max_block_chars` | `260` | `block` 模式单块硬上限(无自然边界时也在此处硬切) |
| `qq_stream_min_interval` | `2.5` | 两条中间气泡最小间隔(秒) |
| `qq_stream_max_messages` | `5` | **单轮总气泡上限**(含收尾补发) |
| `qq_stream_flush_interval` | `0.3` | 收尾补发块之间的间隔(秒) |
| `stream_suppress_tool_preamble` | `true` | 工具轮"前导文本"抑制(缓冲+宽限), `false`=过程旁白也发 |
| `stream_preamble_grace_ms` / `_chars` | `1500` / `40` | 前导抑制的宽限窗口(毫秒 / 字符) |

**会话运行时缓存(A+ A4/A5)**: 每个会话在内存中缓存一个 `AgentLoop` + 串行锁, 保证同会话串行且工具护栏/摘要缓存跨轮复用。
`memory_max_cached_sessions` 控制容量, 超限时按 LRU 淘汰最久未使用的**空闲**会话; `memory_cache_idle_seconds` 控制空闲
自动淘汰(`0` 关闭)。在途 turn 通过引用计数保护, 永不换锁/淘汰; `/clear`、purge、QQ 轮换、CLI `/new`·`/clear`
与进程关停会立即释放。淘汰只丢进程内辅助状态, JSONL 仍是唯一事实源。观测入口: `ConversationService.stats()`。

> 人设文件固定为项目根 `identity.md`，随仓库提供，不再通过 `.env` 配置。

### 上下文 Token 压缩(自动)

每次 `provider.chat()` 前按 `token_budget` 估算「system prompt + 工具定义 + 全部消息」;
超预算时按 **L1 摘要 → L2 硬裁最旧完整 turn → L3 当前轮工具结果占位** 逐级压缩。压缩只作用于
发给模型的**请求视图**: 不修改事实源消息、不写 JSONL; 每次摘要/降级会把「原文 + 摘要」追加到
`<memory_dir>/HISTORY.md`(仅供人工校验, 模型不可读写)。预算耗尽仍放不下时本轮返回
`context_overflow`, 提示拆分问题或 `/clear`。

- 精确计数: 安装 `pip install "meowmeowclaw[tokenizers]"`, 并在 `<项目根>/tokenizers/<模型名>/tokenizer.json`
  放好分词器(如 `tokenizers/deepseek-flash/tokenizer.json`); 找不到时自动回退 CJK 加权启发式。
- 默认装载窗口 `50 轮 / 120000 字符`: 120k 中文字符 ≈ 60k token, 叠加固定开销后可触达 48k 预算,
  让「历史摘要压缩」在默认配置下真正可用(此前 20 轮/50k 字符≈25k token, 默认预算不可达)。
- 设计细节见 `docs/CONTEXT_COMPRESSION_DESIGN.md`。

### 多渠道网关(asyncio.Queue 总线)

`gateway_enabled=true` 时, CLI 与 QQ 等渠道统一经进程内消息总线:

```
ChannelAdapter → inbound → GatewayDispatcher(去重 + ChannelPolicy)
                                → AgentWorker(ConversationService)
ChannelAdapter ← outbound:<channel> ← 回复 Envelope
```

- **统一信封**: `Envelope`(kind/channel/scope/conversation_id/session_id/message_id/correlation_id/reply_to/target_channel/metadata);
- **适配器契约**: `start/stop_inbound/stop/send`; 已提供 `LoopbackAdapter`(回环)、`QqAdapter` + `OneBot11Adapter`(**NapCat 反向/正向 WebSocket**)、`CliAdapter`(REPL);
- **策略层**: `QqPolicy`(身份/6h 轮换/指令)、`CliPolicy`(会话/指令); Agent 不感知渠道;
- **可靠性**: 有界队列 + publish 超时进死信; 同会话串行由 `ConversationService` 保证、跨会话并发; 关闭时按 `gateway_shutdown_timeout` 优雅排空;
- **观测**: `Gateway.stats()` 返回队列积压/在途/死信快照; 长跑时每 5 分钟写入一条状态日志; `HISTORY.md` 审计不受影响;
- `gateway_enabled=false`(默认)时交付层行为与旧直连逐字节一致; 设计见 `docs/GATEWAY_DESIGN.md`。

### 接入 QQ(NapCat / OneBot 11)

QQ 渠道独立进程运行, 与 CLI 互不干扰; 采用**反向 WebSocket**: 本进程监听, NapCat 作为客户端连入并自动重连。

**第一步: NapCat 侧(WebUI 一次配置)**

1. 登录 NapCat WebUI(`http://127.0.0.1:6099/webui`), 确认机器人 QQ 已登录;
2. 进入「网络配置 → OneBot 11」, 新增一条 **WebSocket 客户端**:

   | 字段 | 值 |
   | --- | --- |
   | 名称 | `meowmeowclaw` |
   | URL | `ws://127.0.0.1:3100/onebot/v11/ws` |
   | Token | 与 `.env` 的 `qq_token` 一致(示例 `meowclaw-3100`) |
   | 消息格式 | `array`(推荐) |
   | 启用 | ✅ |

3. 保存启用; NapCat 会按自身重连间隔(默认 30s)连入 Agent。

**第二步: Agent 侧**

```bash
conda activate AI_chat
pip install -e ".[dev]"          # 依赖新增 websockets
# .env: gateway_enabled=true, qq_owner_id=<主人QQ>, qq_token=<与 NapCat 一致>
python -m meowmeowclaw.qq --check    # 只校验配置并打印参数(不连 NapCat)
python -m meowmeowclaw.qq            # 启动 QQ 渠道(等价入口: meowmeowclaw-qq)
```

启动后看到 `NapCat 已连接` 即接入成功; 用主人 QQ 私聊机器人即可对话。
`/help` `/new` `/clear` `/sessions` 走策略直回、不消耗模型调用; 6 小时无活动自动轮换会话并先发「已开始新对话」。

**边界与语义**

- 仅响应 `qq_owner_id` 的**私聊**; 其他人、群聊、心跳/生命周期事件一律忽略;
- 出站用 `send_private_msg` + **消息段数组**发送(避免模型输出被当作 CQ 码执行); 超长回复按 `qq_message_chunk_chars` 分片;
- 入站按 `(channel, message_id)` 去重(NapCat 重推不会产生重复轮次); 断线期间 Agent 回复会丢弃并记 warning(fail-soft);
- 图片/语音等非文本段转 `[图片]` `[语音]` 等占位符进入上下文, v1 不支持多模态回复;
- 安全: 反向模式在服务端校验 Token; `stop_inbound()` 只停收新消息、保留连接, 保证优雅关闭时在途回复仍能送达。

### 流式输出(CLI 逐字 / QQ 渐进式气泡)

设计、里程碑与验收见 **`docs/STREAMING_DESIGN.md`**(S1–S5.1 已落地)。

```ini
stream_enabled=true          # 总开关; 默认 false(零行为变化)
cli_stream=true              # CLI 逐字输出
qq_stream_mode=typing        # off | typing | block
```

| 渠道 | 形态 | 说明 |
|---|---|---|
| CLI | 逐字打印 | 生成中实时输出; 最终只补发剩余后缀, **不会重打一遍** |
| QQ `typing` | "对方正在输入…" + **1 条**完整气泡 | 默认档位; 0 中间气泡, 无刷屏风险 |
| QQ `block` | **渐进式多条气泡** | 按语义边界分块(段落>换行>句末>逗号>硬切), 每块 80~260 字、贴近 target 150; 单轮总气泡 ≤5; 收尾按预算均分 |

**QQ 无法编辑已发消息**(NapCat 183 个动作里没有 edit), 所以 QQ 侧只做"伪流式":
`block` 靠**语义分块 + 节流 + 条数封顶**避免"一字一气泡", 最终答复只补发未送达的后缀
(水位线保证不重复、不缺失); 模型吐字节奏不影响块大小(30/60/120 字一批结果一致)。

- 不可调和的边界: 若模型先输出较长正文、再决定调用工具, 那部分旁白无法收回
  (默认用 `stream_suppress_tool_preamble=true` 抑制, 见设计文档 §4.3/§8);
- 观测: `Gateway.stats()` 含 `stream_enabled / stream_deltas_published / stream_deltas_dropped`;
- 回滚: `qq_stream_mode=typing`(仅保留"正在输入") 或 `stream_enabled=false`(完全回到旧行为)。

### 内置工具(默认注册 6 个 + `load_skill`; 内置技能始终存在)

| 工具 | 能力 | 关键限制 |
| --- | --- | --- |
| `read_file` | 读工作区内文本文件 | 越界/记忆运行时目录拦截; 长期记忆 `MEMORY.md` 可读; 超过 16000 字符截断 |
| `write_file` | 写文件(自动建父目录) | 越界/记忆运行时目录拦截; 覆盖 `MEMORY.md` 前自动备份上一版 |
| `list_dir` | 列目录(目录加 `/`、文件带字节大小、按名排序) | 越界/记忆运行时目录拦截 |
| `exec` | 在工作区执行 Shell 命令 | 60 秒超时; 危险命令黑名单; 输出 10000 字符截断 |
| `web_search` | DuckDuckGo 联网搜索 | 默认 5 条(上限 20); 20 秒超时; 输出 8000 字符截断 |
| `web_fetch` | 抓取 URL 并转纯文本 | 仅 http/https; **拒绝内网/回环地址**; 15 秒超时; 输出 12000 字符截断 |
| `load_skill` | 按名取回技能指南正文 | **仅发现技能时注册**; 未知名会回列可用技能; 16000 字符截断 |

> - 联网工具在受限网络(如国内直连)下需先配代理, 见 [第 6 章](#6-联网能力与代理配置wsl2--clash);
> - 技能系统(SKILL.md 目录约定、渐进式披露、`load_skill` 链路)见 [第 4 章](#4-技能系统skills).

---

## 2. 构建框架: 四层 + 一个循环

```
┌────────────────────────────────────────────────────────────────────────────┐
│ 入口 / 交互层      cli.py + bootstrap.py                                   │
│   build_application() 装配 6 工具(+load_skill) · 技能摘要 · REPL           │
└──────────────────────────────────────┬─────────────────────────────────────┘
                                       │                                      
┌──────────────────────────────────────▼─────────────────────────────────────┐
│ 控制流层           agent/loop.py :: AgentLoop                              │
│   run(): for 最多 max_iterations 轮「模型 ↔ 工具」                         │
│     chat → 有 tool_calls ? 执行工具并回填 → continue : 返回最终回答        │
│   护栏: _check_tool_loop(重复 10 次警告 / 20 次熔断) · clear_history()     │
└───────────┬─────────────────────────┬──────────────────────────┬───────────┘
            │                         │                          │            
┌───────────────────────┐ ┌───────────────────────┐ ┌────────────────────────┐
│ 提示词层              │ │ 能力层  tools/ (6+1)  │ │ 模型接入层             │
│ context.py            │ │ 本地: 读/写/列目录    │ │ llm/                  │
│ ContextBuilder        │ │ 执行: exec(黑名单)    │ │ LLMProvider (ABC)      │
│ build_system_prompt() │ │ 联网: search / fetch  │ │ OpenAICompatProvider   │
│ build_messages()      │ │ 技能: load_skill      │ │ → AsyncOpenAI          │
│ 人设+时间+工作区      │ │ → OpenAI function     │ │ 异常 → error 响应      │
│ + 记忆 / 技能摘要     │ │ 越界 / SSRF 防护      │ │ 可换任意兼容服务       │
└───────────────────────┘ └───────────────────────┘ └────────────────────────┘
                                                                 │            
┌────────────────────────────────────────────────────────────────▼───────────┐
│ 配置层             config.py :: Settings + load_config()                   │
│   paths.py 唯一定位项目根/workspace/人设; .env 解析无副作用               │
│   workspace 由 bootstrap.build_application() 创建; 技能目录同上           │
└────────────────────────────────────────────────────────────────────────────┘
```

**一次 `run()` 的数据流**

1. `ContextBuilder.build_messages(history, user_message)` → `[system] + 历史 + [本次用户消息]`
2. `provider.chat(messages, tools=registry.get_definitions(), model)` → `LLMResponse`
3. 若 `has_tool_calls`: 把 assistant(`tool_calls`) 追加进 messages → 逐个执行 `registry.execute(name, arguments)` → 结果以 `role="tool"` 回填 → 回到第 2 步
4. 若无工具调用: 追加最终 assistant 消息 → 整轮写入 `_session_history` → 返回文本
5. 超过 `max_iterations` 返回超时提示; `finish_reason=="error"` 返回错误文本

**依赖方向单向**: `cli → bootstrap → agent / skills / tools / llm`; `config` / `paths` 只被装配层与技能扫描使用, 契约层(`tools/base.py`、`llm/base.py`)零上游依赖, 无循环依赖.

---

## 3. 各组成部分

| 模块 | 行数 | 职责 | 关键 API |
| --- | ---: | --- | --- |
| `meowmeowclaw/config.py` | 684 | 读 `.env`、校验兜底, 纯解析不产生副作用; 含记忆窗口与会话运行时缓存(A4/A5)配置 | `load_config()` / `Settings` |
| `meowmeowclaw/paths.py` | 40 | 项目根 / `.env` / workspace / 人设路径的唯一来源 | `PROJECT_ROOT` / `resolve_workspace()` / `IDENTITY_FILE` |
| `meowmeowclaw/llm/base.py` | 104 | LLM 接入抽象 + **统一数据契约** + 流式事件 | `LLMProvider.chat()` / `chat_stream()`、`StreamTextDelta` / `StreamFinal` |
| `meowmeowclaw/llm/openai_compat.py` | 207 | OpenAI 兼容实现(异常包装为 `finish_reason="error"`) | `OpenAICompatProvider` |
| `meowmeowclaw/tools/base.py` | 91 | 工具抽象, 产出 OpenAI function 定义 | `BaseTool` |
| `meowmeowclaw/tools/registry.py` | 57 | 注册、查重、定义查询、按名路由执行 | `ToolRegistry` |
| `meowmeowclaw/tools/filesystem.py` | 309 | 文件三件套; realpath 路径防护 + 记忆目录策略 + MEMORY.md 备份 | `ReadFileTool` / `WriteFileTool` / `ListDirTool` / `resolve_in_workspace` |
| `meowmeowclaw/tools/shell.py` | 196 | 工作区内执行命令(黑名单 + 进程组清理) | `ExecTool` |
| `meowmeowclaw/tools/web_search.py` | 139 | DuckDuckGo 搜索(同步库丢线程池) | `WebSearchTool` |
| `meowmeowclaw/tools/web_fetch.py` | 248 | 网页抓取 → html2text → 清理(SSRF 防护) | `WebFetchTool` |
| `meowmeowclaw/skills/loader.py` | 237 | 技能扫描/索引/摘要(importlib.resources + 启动扫描一次) | `SkillCatalog` |
| `meowmeowclaw/skills/models.py` | 33 | 技能数据模型与配置错误 | `Skill` / `SkillConfigError` |
| `meowmeowclaw/skills/tool.py` | 85 | `load_skill` 工具(技能子系统对 Agent 的唯一出口) | `LoadSkillTool` |
| `meowmeowclaw/memory/models.py` | 381 | 会话数据模型 + `MemoryRecord`(结构化长期记忆) | `SessionKey` / `SessionMessage` / `MemoryRecord` / `new_memory_record` |
| `meowmeowclaw/memory/store.py` | 96 | 仓储抽象: `SessionStore` + `LongTermStore`(async Protocol) | `SessionStore` / `LongTermStore` |
| `meowmeowclaw/memory/jsonl.py` | 727 | JSONL 实现: 轮次原子写 / 窗口装载 / 归档 / 短 ID / 跨进程锁与崩溃补行; A1 计时与 `stats()`; A2 三路径尾读; A3 `load_recent` 反向块读窗口 | `JsonlSessionStore` |
| `meowmeowclaw/memory/metrics.py` | 126 | **A+ A1 指标**: 有界延迟样本(count/avg/p50/p95)、三项主路径、`measure()` 计时 | `LatencyStats` / `StoreMetrics` |
| `meowmeowclaw/memory/tail.py` | 114 | **A+ A2 尾读**: 反向块读完整行、惰性生成器命中即停、首行 header / 末条有效 turn | `read_header` / `read_last_lines` / `read_last_valid_turn` |
| `meowmeowclaw/memory/noop.py` | 42 | 长期记忆占位实现(v1 由 MEMORY.md 承担) | `NoopLongTermStore` |
| `meowmeowclaw/memory/filelock.py` | 110 | 跨进程 advisory 文件锁(flock/msvcrt) | `FileLock` / `async_file_lock` |
| `meowmeowclaw/agent/context.py` | 208 | System Prompt: 按"稳定->易变"排序(prefix caching 友好) | `ContextBuilder.build_system_prompt()` / `build_messages()` |
| `meowmeowclaw/agent/loop.py` | 459 | **控制流**: 多轮往返、防爆护栏、历史快照、流式增量与工具轮前导抑制 | `AgentLoop.run_turn(on_delta=...)` / `AgentTurn` / `run()` / `clear_history()` |
| `meowmeowclaw/conversation.py` | 375 | **编排层**: 装载历史 -> run_turn -> 仅完整轮次回写; 同会话串行; A4 `_SessionRuntime` + refcount/LRU/TTL 生命周期 | `ConversationService` / `ConversationResult` |
| `meowmeowclaw/qq.py` | 223 | **QQ 渠道进程入口**: 配置校验/`--check`/装配/信号与优雅关闭/周期状态日志 | `main()` / `run_qq()` / `qq_config_problems()` |
| `meowmeowclaw/channels/base.py` | 29 | 渠道适配层通用消息类型(传输无关) | `IncomingMessage` / `OutgoingMessage` |
| `meowmeowclaw/channels/bridge.py` | 29 | 平台 DTO ↔ 网关 Envelope 转换 | `incoming_to_envelope()` / `outgoing_from_envelope()` |
| `meowmeowclaw/channels/commands.py` | 67 | 跨渠道会话指令公共逻辑(QQ/CLI 共用) | `SessionCommandHelper` |
| `meowmeowclaw/channels/qq_policy.py` | 238 | QQ 策略: 身份过滤 / 6h 惰性轮换 / 指令; A4 轮换释放旧运行时 | `QqPolicy` |
| `meowmeowclaw/channels/qq_adapter.py` | 47 | QQ 适配器(可注入 transport; 传输见 `onebot11.py`) | `QqAdapter` |
| `meowmeowclaw/channels/onebot11.py` | 826 | **OneBot 11(NapCat)传输**: 反向服务端/正向客户端、token 鉴权、消息段↔文本、`send_private_msg`、分片、重连、`set_input_status`、block 分段呈现 | `OneBot11Transport` / `OneBot11Adapter` |
| `meowmeowclaw/channels/streaming.py` | 438 | **流式渲染基础件**: 水位线/前缀补发、语义分块(`BlockSegmenter`)、收尾均分(`split_evenly`)、"正在输入"续期(`TypingKeepAlive`) | `StreamAssembler` / `BlockSegmenter` / `split_evenly` |
| `meowmeowclaw/channels/qq_private.py` | 260 | QQ active 指针存储 + `QqPrivateService` 兼容 facade | `QqPrivateActiveStore` / `QqPrivateService` |
| `meowmeowclaw/channels/cli_policy.py` | 211 | CLI 策略: 会话生命周期(切走释放运行时) + /help /new /clear /sessions /tools /skills | `CliPolicy` |
| `meowmeowclaw/channels/cli_adapter.py` | 111 | CLI 适配器: REPL + 总线收发 + Future 配对 | `CliAdapter` |
| `meowmeowclaw/gateway/bus.py` | 179 | asyncio.Queue 总线: inbound/outbound/deadletter、背压、关闭、观测 | `AsyncioQueueBus` / `MessageBus` |
| `meowmeowclaw/gateway/envelope.py` | 119 | 统一信封与构造/校验 | `Envelope` / `make_inbound()` / `make_reply()` |
| `meowmeowclaw/gateway/policy.py` | 76 | 策略契约与裁决(ignore/reply/agent) | `ChannelPolicy` / `PolicyDecision` |
| `meowmeowclaw/gateway/dedup.py` | 47 | `(channel, message_id)` LRU 去重 | `DedupCache` |
| `meowmeowclaw/gateway/adapter.py` | 177 | 适配器契约/基类 + Loopback | `BaseChannelAdapter` / `LoopbackAdapter` |
| `meowmeowclaw/gateway/worker.py` | 43 | Agent 执行器(包装 ConversationService) | `AgentWorker` |
| `meowmeowclaw/gateway/dispatcher.py` | 193 | 单任务消费 + 策略路由 + 并发派发 | `GatewayDispatcher` |
| `meowmeowclaw/gateway/gateway.py` | 153 | 组装、优雅启停、`stats()` 观测 | `Gateway` |
| `meowmeowclaw/bootstrap.py` | 214 | **组合根**: 配置 -> Provider/工具/技能/记忆/Context/Loop + 可选 Gateway; A5 透传运行时缓存配置 | `build_application()` / `build_gateway()` / `Application` |
| `meowmeowclaw/cli.py` | 338 | **交付层**: banner / REPL; 网关开启时经 CliPolicy + CliAdapter; legacy 路径 /new /clear 同步释放运行时 | `main()` / `interactive_loop()` / `_run_gateway_repl()` |

### 3.1 契约先行, 实现可换

- **Provider 契约**: `LLMProvider.chat(messages, tools, model) -> LLMResponse`. 上层只认 `LLMResponse`, 不关心背后是 OpenAI、DeepSeek 还是本地 vLLM.
- **工具契约**: `BaseTool` 同时是"给模型看的 JSON Schema"(`to_function_definition()`)与"给人写的可执行体"(`execute()`).
- **消息契约**: 全链路 OpenAI 消息格式, `tool_calls.function.arguments` 是 JSON 字符串.
- **网关契约**: `ChannelAdapter`(start/stop_inbound/stop/send) + `ChannelPolicy`(ignore/reply/agent) + `MessageBus`; 设计见 `docs/GATEWAY_DESIGN.md`.

### 3.2 统一错误策略:"异常不炸主循环"

| 层 | 策略 |
| --- | --- |
| Provider | 捕获异常 → `LLMResponse(content="[LLM调用失败] ...", finish_reason="error")`(`CancelledError` 等 `BaseException` 继续上抛) |
| 工具 | 一律返回可读文本; `ToolRegistry.execute` 再兜一层 |
| Loop | `finish_reason=="error"` 立即返回; 空 `choices`、工具名不存在等都变成文本回流给模型 |
| 交互层 | 单次失败只打印 `[异常]`, 会话继续 |

好处是**模型能看到失败原因并自我纠正**(工具参数非法、工具名写错、命令被拦截等), 而不是让进程崩掉.

### 3.3 循环护栏

`_check_tool_loop(tool_name, args_json)` 以"工具名 + 入参 JSON"为签名, 在长度 30 的滑动窗口内统计重复次数:

- 重复 ≥ 10 次 → **警告**: 跳过本次执行, 回填 `[SYSTEM_ERROR] ...` 提示模型换思路
- 重复 ≥ 20 次 → **熔断**: 结束本轮并返回熔断说明
- 窗口内无论是否告警都记账, 否则计数会停在 10 导致熔断阈值永远不可达

### 3.4 会话状态

- `_session_history`: 跨轮次对话历史, **只保存跑完整的一轮**; 错误/熔断/超时这类半截过程不写入.
- `_tool_call_history`: 防爆签名滑窗, `/clear` 时一并清空.
- `reasoning_content`(推理模型思考过程)保留在 `LLMResponse` 上, **不回填 messages** -- DeepSeek 等要求多轮时不得回传.

---

## 4. 技能系统(Skills)

**技能 = 一段按需加载的"怎么做"指南**(`SKILL.md`)。平时只把「技能名 + 一句话描述」放进 System Prompt,
模型判断与当前任务相关时, 再用 `load_skill` 工具把指南正文取回来 —— 这就是**渐进式披露
(progressive disclosure)**: 既让模型知道"有哪些能力", 又不把长篇操作手册一次性塞进上下文.

### 4.1 目录约定

```
meowmeowclaw/skills/builtin/       # 内置技能随包发布(入 git / 随 wheel)
├── exec/SKILL.md
├── read_file/SKILL.md
└── ...                            # 每个子目录一个技能, 只扫描一层
```

> 技能不再放在 `workspace/` 下: `workspace/` 只保留 Agent 运行时产物, 内置技能随代码分发,
> 保证全新克隆/安装后技能一定存在。

`SKILL.md` 用 YAML frontmatter 描述元信息, 正文就是给模型看的指南:

```markdown
---
name: exec
description: 在工作区内执行 Shell 命令的用法与安全限制
---
# exec 使用指南
## 何时使用 ...
## 参数 ...
## 安全限制 ...
```

### 4.2 装配与执行链路

| 环节 | 实现 |
| --- | --- |
| 扫描 / 解析 | `skills/loader.py :: SkillCatalog`(importlib.resources、启动扫描一次、坏 YAML 跳过、重名报错) |
| 注入提示词 | `bootstrap.build_application()` 把 `catalog.summary()` 传给 `ContextBuilder(skills_summary=...)`, `build_system_prompt()` 追加 `## 可用技能` 章节 |
| 按需取回 | `skills/tool.py :: LoadSkillTool`(**有技能时才注册**), 模型调用 `load_skill(name="exec")` |
| 技能不存在 | 返回 `[错误] 未找到技能: xxx. 可用技能: ...`, 把可用名字回给模型便于自纠 |
| 越界防护 | 先按名**精确查内存索引**, 未知名直接返回错误, 不拼接路径、不触碰文件系统 |

System Prompt 里最终长这样:

```
## 可用技能
你有以下技能可用. 当某项技能与当前任务相关时, 请调用 load_skill 工具并传入技能名, 获取该技能的详细指南.

可用技能:
- exec (exec/SKILL.md): 在工作区内执行 Shell 命令的用法与安全限制
- read_file (read_file/SKILL.md): 读取工作区内文本文件的用法与限制
```

### 4.3 自带技能

仓库在 `meowmeowclaw/skills/builtin/` 下自带 6 个"工具用法"技能(`read_file` / `write_file` /
`list_dir` / `exec` / `web_search` / `web_fetch`), 把每个工具的参数、限制(截断 / 超时 / 命令黑名单 /
SSRF 防护)与推荐用法写成指南 —— 技能随代码入库、随包分发, 克隆/安装后必然可用.

### 4.4 边界

- 技能目前**只是文本**, 不含可执行脚本与随附资源(不同于 Claude Skills 的 `scripts/` 目录);
- 只扫描 `skills_dir` 的**一层子目录**, 不支持分组嵌套;
- `load_skill` 返回的正文超过 16000 字符会截断。

## 5. 安全设计

面向"通用型助手"的定位, 把风险点都做了显式处理, **并如实标注了边界**.

### 5.1 路径防护(文件三件套)

三件套共用唯一的 `resolve_in_workspace()`: 先把 `workspace + 用户路径` 归一化为绝对路径,
再用 `os.path.commonpath` 判定是否落在工作区内, 越界返回 `[安全拦截] ...`;
同前缀兄弟目录(`/ws_evil` vs `/ws`)不会再被误放行, 非字符串路径也会被包装成可读文本.

记忆目录策略(M5): `<memory_dir>/sessions|active|archive` 对读/写/列举全部拦截;
`memory/MEMORY.md` 允许读写, 且 `write_file` 覆盖前会先把上一版滚动备份为 `MEMORY.md.bak`;
System Prompt 同时注入"先 read 后 write、保留旧内容、不写敏感信息"的长期记忆维护约定.
路径校验统一走 `realpath`(M7), 工作区内指向外部的符号链接同样会被拦截.

### 5.2 命令执行防护(`exec`)

17 条正则黑名单(`re.IGNORECASE`)覆盖递归删除 / 格式化 / 关机重启 / 提权 / 覆盖设备文件 / 下载即执行 / 反弹 shell / Fork 炸弹等; 命中即返回 `安全拦截: 检测到危险命令模式 '...'`, **不创建任何进程**. 超时清理优先杀**整个进程组**(`start_new_session` + `killpg`), 并带**自杀保护**: 若子进程与父进程同组则退化为杀单进程, 避免误杀调用方.

### 5.3 网络访问防护(`web_fetch`)

- 仅允许 `http`/`https`;
- **默认拒绝本机/内网目标**: 字面量 IP、`localhost`/`*.localhost`、以及**域名解析后的地址**都要过 `not ip.is_global` 判据(覆盖回环、私网、链路本地、CGNAT/Tailscale、保留、组播等);
- 需要抓内网时显式传 `WebFetchTool(allow_private=True)`;
- 附 `<meta charset>` 嗅探、非 HTML 原样返回、2MB 转换上限.

### 5.4 其它

- `Settings.__repr__` 与启动信息**掩码 `api_key`**, 有专门用例断言密钥不出现在输出里;
- 所有工具输出都有长度上限(见第 1 章表格), 避免上下文被单次调用打爆;
- `.env` 已在 `.gitignore` 中, 模板用 `${ENV_VAR}` 而不是明文密钥.

### 5.5 已知缺口与局限(诚实清单)

| 位置 | 问题 | 影响 |
| --- | --- | --- |
| `shell.py` | 黑名单是**护栏不是沙箱** | `rm --recursive -f`、`find -delete`、`python -c`、`base64\|sh` 等可绕过; 也会误报(`git commit -m "fix rm -rf bug"`) |
| `web_fetch.py` | 只做**发起前**检查 | `follow_redirects=True` 时远端 302 跳内网仍会发出请求; 存在 DNS 重绑定窗口 |
| `llm/base.py` | `has_tool_calls` 直接 `len()` | `tool_calls=None` 时抛 `TypeError`; `usage` 类型标注缺 `Optional` |

> `filesystem.py` 的两条旧缺口(同前缀兄弟目录绕过、非字符串路径抛异常)已修复,
> 由 `TestPathHardening` 与 `TestResolveInWorkspace` 覆盖; 上表剩余项中 `llm/base.py` 仍以 `xfail` 跟踪。
> **生产建议**: `exec` 默认不注册或加开关、放进容器/受限用户运行; 把工作区当不可信输入.

---

## 6. 联网能力与代理配置(WSL2 + Clash)

`web_search` / `web_fetch` 需要出网. 若 Linux 侧不能直连(DNS 污染、无路由), 可通过 Windows 上运行的 Clash 代理解决. 以 **WSL2 mirrored 网络模式 + Clash Verge** 为例:

```bash
# Windows: 在 %UserProfile%\.wslconfig 中启用 mirrored, 然后 wsl --shutdown
#   [wsl2]
#   networkingMode=mirrored
# 之后 WSL 里 127.0.0.1 即 Windows 本机, 可直接访问 Clash 的混合端口(默认 7897)

export HTTPS_PROXY="http://127.0.0.1:7897" HTTP_PROXY="http://127.0.0.1:7897"
export NO_PROXY="127.0.0.1,localhost,::1,*.local,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
```

- 无需改任何代码: 底层的 `primp`(ddgs) 与 `httpx` 都会自动读取 `HTTPS_PROXY`;
- **不要设 `ALL_PROXY=socks5://...`**: httpx 未安装 `socksio` 时会直接报错, 会连累 LLM 调用链路;
- NAT 模式下 Windows 主机是网关 IP(`ip route` 默认网关)且**重启会变**, mirrored 模式才能稳定用 `127.0.0.1`.

验证:

```bash
curl -x http://127.0.0.1:7897 -o /dev/null -w '%{http_code}\n' https://duckduckgo.com/
RUN_NETWORK_TESTS=1 pytest tests/ -m network -q     # 真实联网用例
```

---

## 7. 与"用 DAG / 图编排构建的 Agent"的区别

以 LangGraph 这类"节点 + 边"的图编排框架为对照:

| 维度 | 本项目(命令式 ReAct 循环) | DAG / 图编排(如 LangGraph) |
| --- | --- | --- |
| **控制流归属** | 写在代码里: `AgentLoop.run()` 的一个 `for` 循环 | 声明在图里: 节点 + 边, 由**引擎**推进 |
| **分支路由** | 由**模型输出**(`tool_calls`)在运行时动态决定 | 由开发者预先声明, `conditional_edge` 依据 State 选择分支 |
| **循环** | 普通 Python 循环(`for` / `continue`) | 回边(back edge)形成环, 图里显式画出来 |
| **状态组织** | 一个 `messages: list[dict]` 直接传递 | 全局 `State` + `reducer` 合并规则 |
| **护栏** | 直接写在循环里(`_check_tool_loop` + `max_iterations`), 工具自身还带黑名单/SSRF 防护 | 需要额外插入 guard 节点或中间件 |
| **依赖** | 仅 openai + dotenv + httpx(+ddgs/html2text) | 引入编排框架及其概念栈 |
| **可观测 / 可视化** | 无内建; 靠日志与断点 | 图结构可可视化, 常见内置 tracing 集成 |
| **持久化 / 断点续跑** | 无内建(`_session_history` 在内存里) | 内建 checkpointer, 可从任意节点恢复 |
| **人工审批(HITL)** | 需自行在循环里加确认步骤 | 一等公民: 中断/恢复 API |
| **多 Agent 编排** | 需自己写调度(子 Agent 即另一个 `AgentLoop`) | 天然支持 supervisor / swarm 等拓扑 |
| **测试方式** | 换掉 `LLMProvider` 一个替身即可跑全链路(本项目 1556 passed 用例) | 通常要驱动图运行时, 或按节点分别测 |
| **调试体验** | 断点就在 `run()` 里, 栈短、易读易改 | 需要在框架抽象层之间跳转 |
| **适合场景** | 单 Agent + 工具调用的主线业务; 想快速看懂/改控制流 | 复杂分支编排、长流程、需要暂停恢复与人工介入 |

**一句话概括**: DAG 是"把控制流**画出来**交给引擎跑", 本项目是"把控制流**写成代码**自己跑". 本质差别是 **控制流归属**(引擎 vs 代码)与 **状态组织**(State + reducer vs messages 列表). 本项目的取舍是**用可读、可测、零依赖换取了编排能力**.

**演进建议**: 若后续需要"并行工具调用 / 子 Agent 分工 / 人工审批 / 断点续跑", 把 `AgentLoop.run()` 内的循环替换成图编排是自然路径; 由于 `LLMProvider`、`BaseTool` 都是抽象基类且数据契约独立, **替换编排层不需要改动 Provider、工具与配置层**. 可先用"双轨 + 配置开关 + 双引擎等价测试"灰度过渡.

---

## 8. 扩展指南

**加一个工具**(3 步)

```python
class HttpGetTool(BaseTool):
    @property
    def name(self) -> str: return "http_get"
    @property
    def description(self) -> str: return "抓取网页文本, 需要联网查资料时调用"
    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"url": {"type": "string"}},
                "required": ["url"], "additionalProperties": False}
    async def execute(self, **kwargs) -> str: ...

# 在 meowmeowclaw/bootstrap.py::build_registry() 中 import 并注册, 模型即可见:
registry.register(HttpGetTool())
```

**换模型 / 换服务**: 只改 `.env` 的 `model` + `base_url`; 非 OpenAI 协议则实现一个 `LLMProvider` 子类.
**换编排**: `AgentLoop` 是独立一层, 可整体替换, `bootstrap.py` 只改装配代码.

---

## 9. 测试

```bash
pytest                                        # 全量: 1037 passed, 6 skipped, 2 xfailed
pytest tests/agent/test_loop.py -v
RUN_NETWORK_TESTS=1 pytest -m network -v      # 仅真实联网用例
ruff check meowmeowclaw tests                 # 硬错误静态检查(E9 + F)
```

CI: GitHub Actions(`.github/workflows/ci.yml`)在 push / PR 时于 Python 3.10 / 3.11 / 3.12 上执行 `ruff check` 与 `pytest`.

策略: **Mock 为主、真实实现对照为辅**, 并用**变异测试**验证用例有效性(每轮改动都跑过 10~20 个变异体, 确认全被捕获).

| 测试文件 | 用例 | 重点 |
| --- | ---: | --- |
| `test_bootstrap.py` | 17 | 装配(工具/技能/Context/Loop/网关/运行时缓存透传)、ConfigError/SkillConfigError、导入边界 |
| `test_cli.py` | 26 | REPL、/new、/clear [id] [--purge]、/sessions、启动输出与退出码、网关分流 |
| `gateway/test_*.py` | 75 | 信封/总线/去重/策略/适配器/调度/网关生命周期/观测 |
| `channels/test_qq_policy.py` | 14 | QQ 策略: 身份/轮换/指令/跨渠道删除/A4 轮换释放运行时 |
| `channels/test_qq_adapter.py` | 3 | QQ 适配器: 入站转换/出站/停入站 |
| `memory/test_tail.py` | 27 | A+ A2 尾读工具: 反向块读/UTF-8 跨块/超大行/上限/与全量等价 |
| `memory/test_jsonl_tail_path.py` | 10 | A+ A2 存储层: 打桩证明不走全量、回退路径、A+1 累计口径、归档回退 |
| `memory/test_jsonl_load_recent_tail.py` | 212 | **A+ A3 窗口尾读**: 200 组随机文件全量差分 + 预算翻倍/回退/半行/超大行/UTF-8 边界 |
| `channels/test_streaming.py` | 29 | 流式基础件: 语义分块窗口/边界优先级/围栏保护/收尾均分/输入状态续期 |
| `channels/test_block_uniformity.py` | 10 | **S5.1 回归**: 用实测原文验证 722/288 字分块均匀、内容一致、条数封顶 |
| `tests/test_stream_llm.py` | 17 | 流式 Provider 契约: 默认回退/tool_calls 分片/空流回退/中断保文本/400 重试 |
| `agent/test_loop_stream.py` | 11 | AgentLoop 增量外发与工具轮前导抑制 |
| `channels/test_onebot11.py` | 32 | OneBot11: 文本/CQ 码提取、事件过滤、反向鉴权、正向重连、分片、stop_inbound 契约 |
| `channels/test_qq_napcat_integration.py` | 7 | 假 NapCat 客户端 ↔ Agent 反向 WS 端到端(往返/忽略/指令/轮换/重连) |
| `test_qq_entry.py` | 8 | QQ 入口: 配置校验、--check 输出、生命周期(启动→信号→优雅关闭) |
| `channels/test_cli_policy.py` | 13 | CLI 策略: 会话/指令/展示/A4 切会话释放运行时 |
| `channels/test_cli_adapter.py` | 7 | CLI 适配器: ask/超时/pending/REPL |
| `channels/test_qq_gateway_integration.py` | 4 | QQ 走网关端到端(轮换/指令/忽略) |
| `channels/test_cli_gateway_integration.py` | 5 | CLI 走网关端到端 + Ctrl+C 优雅关闭 |
| `agent/test_context.py` | 48 | 人设/时间/记忆约定、召回注入、System Prompt 稳定度排序 |
| `agent/test_loop.py` | 41 | 消息格式、防爆阈值、run_turn 历史快照/完成态、max_iterations 注入 |
| `skills/test_catalog.py` | 36 | frontmatter 边界、索引/摘要、坏 YAML 跳过、重名报错、内置资源可发现 |
| `skills/test_tool.py` | 30 | `load_skill` 契约、自纠提示、截断、与引导语口径一致 |
| `memory/test_models.py` | 28 | 会话键 canonical/storage_id、消息投影、元数据/摘要 |
| `memory/test_jsonl_store.py` | 29 | SessionStore 契约 + JSONL 布局/损坏行/归档/短 ID/并发与崩溃恢复 |
| `memory/test_long_term.py` | 13 | MemoryRecord 校验、LongTermStore 协议、Noop 空实现 |
| `memory/test_filelock.py` | 4 | FileLock 争用/释放/异步上下文 |
| `test_conversation.py` | 36 | ConversationService 编排、窗口、fail-soft、同会话串行、agent 缓存、A4 运行时生命周期 |
| `channels/test_qq_private.py` | 15 | QQ active 指针/6h 轮换/重启恢复/命令/跨渠道删除 |
| `tools/test_base.py` | 3 | 工具基类抽象约束 |
| `tools/test_registry.py` | 21 | 注册/查重/路由/异常包装、tools 包导入边界 |
| `tools/test_filesystem.py` | 101 | 三件套读写/截断、路径防护(含符号链接)、记忆目录策略与 MEMORY.md 备份 |
| `tools/test_shell.py` | 91 | 黑名单 17 条、进程组清理与自杀保护、超时、输出拼装 |
| `tools/test_web_search.py` | 44 | 结果格式化、条数归一化、超时、线程池执行、联网开关(4) |
| `tools/test_web_fetch.py` | 71 | SSRF 12 类目标、`async with` 关连接、UTF-8/GBK、截断、联网开关(2) |
| `test_provider_base.py` | 48 | 数据契约(默认值、可变默认、`has_tool_calls`) |
| `test_openai_compat.py` | 44 | 请求参数、tool_calls 转换、usage、异常兜底 |
| `test_config.py` | 147 | 取值优先级、路径/记忆目录解析、窗口/运行时缓存配置、非法值兜底、密钥掩码 |

---

## 10. 目录结构

```
MeowMeowClaw/
├── .env / .env.example          # 配置与模板(密钥不入库)
├── pyproject.toml               # 依赖 / 控制台入口 / pytest 配置
├── scripts/                     # 人工验收脚本(不进 CI)
│   └── bench_session_store.py   # A+ A1: 会话存储 50–2000 轮基准(load_recent/get_meta/append_turn)
├── identity.md                  # 人设文件(固定放项目根)
├── workspace/                   # 运行时工作区(自动创建, gitignore; 可被 .env 绝对路径覆盖)
├── tests/                       # 52 个测试文件 / 1564 用例(agent/skills/tools/memory/channels/gateway/llm 分层)
└── meowmeowclaw/
    ├── config.py                # 配置加载(纯解析, 无副作用)
    ├── paths.py                 # 项目根 / workspace / 人设路径唯一来源
    ├── bootstrap.py             # 组合根: 装配 Application(不 print)
    ├── cli.py                   # 交付层: banner / REPL / 会话命令 / 退出码
    ├── qq.py                    # QQ 渠道进程入口: python -m meowmeowclaw.qq / meowmeowclaw-qq
    ├── __main__.py              # python -m meowmeowclaw -> cli.main()
    ├── agent/
    │   ├── context.py           # System Prompt(含长期记忆维护约定)
    │   └── loop.py              # AgentLoop 控制流
    ├── tools/                   # 工具框架 + 内置工具
    │   ├── base.py              # BaseTool 契约
    │   ├── registry.py          # ToolRegistry 注册/路由
    │   ├── filesystem.py        # 三件套 + 路径防护 + 记忆目录策略/备份
    │   ├── shell.py             # ExecTool
    │   ├── web_search.py        # WebSearchTool
    │   └── web_fetch.py         # WebFetchTool
    ├── skills/
    │   ├── loader.py            # SkillCatalog 扫描/索引/摘要
    │   ├── models.py            # Skill / SkillConfigError
    │   ├── tool.py              # load_skill 工具(子系统唯一出口)
    │   └── builtin/             # 6 个内置技能(每子目录一个 SKILL.md, 随包发布)
    ├── memory/                  # 短期记忆(v1: SessionStore + JSONL)
    │   ├── metrics.py           # A+ A1: 会话 IO 延迟指标(有界样本/分位) + 计时上下文
    │   ├── tail.py              # A+ A2: JSONL 反向块读(尾读 header/末条有效 turn)
    │   ├── models.py            # SessionKey / SessionMessage / MemoryRecord
    │   ├── store.py             # SessionStore + LongTermStore 抽象接口
    │   ├── jsonl.py             # JsonlSessionStore 实现(A3: load_recent 反向块读窗口)
    │   ├── noop.py              # NoopLongTermStore(v1 占位)
    │   ├── filelock.py          # 跨进程 advisory 文件锁
    │   └── errors.py            # 记忆层错误类型
    ├── conversation.py          # ConversationService: 记忆 <-> AgentLoop 编排(A4: 运行时 refcount/LRU/TTL)
    ├── channels/                # 渠道层: DTO / 策略 / 适配器
    │   ├── base.py              # IncomingMessage / OutgoingMessage
    │   ├── bridge.py            # DTO <-> Envelope
    │   ├── commands.py          # 跨渠道会话指令公共逻辑
    │   ├── qq_policy.py         # QqPolicy(身份/轮换/指令)
    │   ├── qq_adapter.py        # QqAdapter(可注入 transport)
    │   ├── onebot11.py          # OneBot11 传输(NapCat 反向/正向 WS) + OneBot11Adapter
    │   ├── qq_private.py        # active 指针 + 兼容 facade
    │   ├── streaming.py         # 流式基础件: 水位线/语义分块/收尾均分/输入状态续期
    │   ├── cli_policy.py        # CliPolicy(会话/指令)
    │   └── cli_adapter.py       # CliAdapter(REPL, 支持逐字输出)
    ├── gateway/                 # 单进程 asyncio 消息总线网关
    │   ├── envelope.py          # 统一信封
    │   ├── bus.py               # AsyncioQueueBus + 死信/观测
    │   ├── policy.py            # ChannelPolicy / PolicyDecision
    │   ├── adapter.py           # ChannelAdapter 契约与基类
    │   ├── dedup.py             # 幂等去重
    │   ├── worker.py            # AgentWorker
    │   ├── dispatcher.py        # 消费/去重/路由/派发
    │   └── gateway.py           # 组装与优雅启停
    └── llm/                     # LLMProvider - OpenAICompatProvider
```

---

## 11. 已知限制与 Roadmap

- A+（会话 IO 尾读 + 运行时生命周期 + 指标）**A1–A5 已全部落地（2026-10-13）**：指标与基准 / `get_meta`·`append_turn`·`list_sessions` 尾读 / `load_recent` 反向块读窗口 / `_SessionRuntime` refcount·LRU·TTL 生命周期 / 配置透传与冒烟验收；设计与记录见 `docs/A_PLUS_OPTIMIZATION.md`，网关接入小结见 `docs/GATEWAY_DESIGN.md` §16.7
- 流式输出已落地 **S1–S5.1**（Provider 流式 / CLI 逐字 / QQ `typing`+`block` 伪流式，`stream_enabled` 默认 `false`）；里程碑、实测数据与验收见 `docs/STREAMING_DESIGN.md`
- 记忆系统 v1 已完成 **M1-M7**(存储/编排/CLI/QQ 服务层/MEMORY.md 约定与备份/LongTermStore 抽象/并发与崩溃收口); 上下文压缩 P1–P5、多渠道网关 W1–W4、QQ 真实传输 **W5(OneBot 11 / NapCat 反向 WS)** 已落地(`gateway_enabled` 默认 `false`, QQ 渠道需显式打开); 结构化长期记忆后端、飞书/Web 适配器、群聊策略与多 Agent 编排尚未接入
- 长期记忆 v1 由 `workspace/memory/MEMORY.md` 承担(Agent 写入 + Prompt 注入); 结构化 `LongTermStore` 抽象与 `NoopLongTermStore` 已落地, 持久化后端待后续实现
- 技能系统边界见 [4.4](#44-边界): 纯文本、单层目录、不含脚本与资源随附
- 安全侧的已知缺口见 [5.5](#55-已知缺口与局限诚实清单)(命令黑名单绕过、抓取重定向/重绑定、provider 契约边界)
- 单实例 `AgentLoop` 不建议并发 `run()`(内部状态未加锁)
- 工程配置见 `pyproject.toml`(依赖 / 控制台入口 / pytest / ruff); CI 见 `.github/workflows/ci.yml`
- **安装形态**: 当前以源码 / editable 安装为主; 纯 wheel 安装时 `identity.md` 不随包分发, CLI 会警告并回退内置默认人设, 默认 workspace 也位于 site-packages 旁 —— 请在 `.env` 显式配置绝对 `workspace`, 或使用 `pip install -e .`

