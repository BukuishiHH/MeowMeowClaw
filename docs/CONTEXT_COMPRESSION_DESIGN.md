# MeowMeowClaw 上下文 Token 压缩设计（v1）

> 状态：**设计已确认；P1（TokenCounter/配置）、P2（请求视图/L2 硬裁）、P3（摘要/滚动缓存/降级/bootstrap 接入）已实现，P4（HISTORY.md）/P5（L3/L4）待实现**。本文只定义行为与边界，不改 JSONL 存储格式。
> 关联：`docs/MEMORY_DESIGN.md` §5.4、`meowmeowclaw/agent/loop.py`、`meowmeowclaw/conversation.py`、`meowmeowclaw/llm/openai_compat.py`。

---

## 0. 背景与目标

### 0.1 现状

- 短期历史已有两级"字符口径"的静态护栏：
  - `ConversationService` 装载窗口 `memory_max_turns=20` / `memory_max_chars=50000`（`conversation.py:95-101`）；
  - `JsonlSessionStore._trim_by_chars` 从最新往回取**完整 turn**，超预算丢最旧（`memory/jsonl.py:338-394`）；
- 工具输出另有单条上限（8k~16k 字符）与落盘截断（8000 字符/条）；
- 但没有任何**模型上下文窗口感知**：不数 token、不识别 `context_length_exceeded`、没有压缩/降级，超限时 `OpenAICompatProvider` 统一包成 `finish_reason="error"`（`llm/openai_compat.py:39-47`），同一会话会反复失败。

### 0.2 本次目标

在每次 `provider.chat()` 之前，对**即将发送的请求**做 token 估算；超过 `.env` 中配置的预算时，把"最早的若干完整 turn"摘要压缩为一条摘要消息，用压缩后的请求视图调用模型；摘要失败时确定性降级，保证不因压缩本身阻塞回复。

### 0.3 非目标（v1 不做）

- 不改 JSONL schema、不把摘要写回 `sessions/*.jsonl`；
- 不做长期记忆（`MEMORY.md`）的自动改写、不做向量检索；
- 不做跨进程共享的摘要缓存；
- 不做多模态 content（图片/音频）token 计数，遇到非字符串按启发式兜底；
- 不保证任意 OpenAI 兼容端点的 tokenizer 都精确（见 §4）。

---

## 1. 已确认决策记录

| # | 决策 | 说明 |
|---|---|---|
| C1 | **请求视图隔离** | 压缩只生成"发给 Provider 的请求副本"（request view），不改本轮事实源 `messages`，摘要/占位不落 JSONL |
| C2 | **全量预算口径** | 预算统计 = system prompt + 工具定义 + 所有 role 消息（含 tool / tool_calls）+ 安全系数；`token_budget` 默认 `48000`（输入上限） |
| C3 | **分词三档降级** | 精确（tiktoken / 本地 HF tokenizer）> CJK 加权启发式 > 字符兜底；`len(text)//2` 不作为预算依据 |
| C4 | **先粗筛、再摘要、后硬裁** | 保留现有轮数/字符粗筛；摘要失败或收益不足时硬删同批最旧完整 turn，并插入固定占位提示 |
| C5 | **保护集** | 永不压缩：system prompt、最近 `keep_recent_turns=2` 个完整 turn、最后一个 `user` 起的当前轮 |
| C6 | **摘要消息形态** | 一条 `role="system"`、前缀 `[历史摘要]` 的合成消息，插在 system prompt 之后；不写成 user/assistant |
| C7 | **摘要失败 fail-soft** | 同模型、`tools=None`、最多 1 次、超时默认 15s；失败/为空/过大/无收益 → 直接降级，仅日志，不阻塞回复 |
| C8 | **审计日志** | 原文与摘要按时间戳追加到 `<memory_dir>/HISTORY.md`；不注入 Prompt、不被压缩逻辑读取、不写 JSONL；写失败 fail-soft |
| C9 | **单轮内超限** | 先压缩旧历史；仍超则对当前轮最旧 `tool` 结果做内容占位（保留 `tool_call_id` 配对）；极限走 error 结束本轮 |
| C10 | **用户不可见** | 压缩与降级只写 logger，不在回复文本中提示 |
| C11 | **配置项** | 新增 10 个 `.env` 键（§8），命名沿用现有短横线/下划线风格，非法值回退默认并告警 |
| C12 | **依赖** | 核心零新增硬依赖；精确分词作为可选 extra（`tiktoken` / `tokenizers`），缺失时自动降级 |
| C13 | **tokenizer 资产路径** | 本地 `tokenizer.json` 默认放 `<项目根>/tokenizers/<模型名净化>/tokenizer.json`；由 `paths.py` 统一解析候选路径，`tokenizers/` 默认加入 `.gitignore` |
| C14 | **审计保护** | `HISTORY.md`（及 `.1` / `.lock`）纳入记忆运行时保护，对模型 `read_file` / `write_file` / `list_dir` 禁访问；`MEMORY.md` 策略不变 |

---

## 2. 术语与口径

| 术语 | 定义 |
|---|---|
| **turn** | 一条 `role="user"` 开始，到（不含）下一条 `user` 之前的全部消息；包含 assistant 文本、assistant `tool_calls`、成对的 `tool` 结果 |
| **完整 turn** | 已由 `ConversationService` 持久化的轮次（`AgentTurn.completed=True`）；压缩只处理完整 turn |
| **当前轮** | 最后一个 `role="user"` 及其之后的全部消息；工具循环中会不断追加 assistant/tool 消息，**全程受保护** |
| **事实源 messages** | `AgentLoop.run_turn` 内维护的 `messages` 列表，用于切出 `messages[new_start:]` 持久化；保持 OpenAI 消息完整语义 |
| **请求视图 request_messages** | `compressor.prepare_request(messages)` 返回的浅拷贝投影：可含摘要消息、可含被占位的 tool 内容；只传给 `provider.chat()`，**绝不参与持久化** |
| **预算口径** | `estimated_input_tokens * SAFETY_FACTOR(1.1)` 与 `token_budget` 比较；预算只覆盖输入，输出空间由"模型窗口 − 预算"的余量承担 |

> C1 的收益：`AgentLoop.run_turn` 的 `new_start = 1 + len(base_history)`（`loop.py:154`）与 `messages[new_start:]`（`loop.py:203`）**不需要改动**，压缩替换历史不会让持久化切片错位或丢消息。

---

## 3. 总体流程与模块划分

```
ConversationService._load_history()
  → store.load_recent(max_turns, max_chars)        # 现有粗筛：输入护栏
  → AgentLoop.run_turn(history, user_message)
       messages = [system] + history + [current]   # 事实源，全程不变
       for iteration in range(max_iterations):
           request = compressor.prepare_request(messages, tools)   # 请求视图
           if compressor.over_budget(request):
               request = await compressor.compress(request)        # 历史摘要 / 硬裁 / 工具占位
           response = await provider.chat(request, tools=..., model=...)
           ... # 事实源照常追加 assistant / tool 消息
  → turn.messages 照常持久化（不含摘要与占位）
```

| 新增模块 | 职责 |
|---|---|
| `meowmeowclaw/llm/tokenizer.py` | `TokenCounter` 协议 + `TiktokenCounter` / `HFTokenizerCounter` / `HeuristicCounter` + `build_counter(model, config)`；只做计数，无状态、可单测 |
| `meowmeowclaw/agent/compression.py` | `split_turns()`、`ContextCompressor`（估算、预算判断、摘要、滚动缓存、硬裁、工具占位、请求视图投影）、降级矩阵 |
| `meowmeowclaw/agent/audit.py`（或并入 compression.py） | `HistoryAuditLog`：HISTORY.md 追加写、时间戳、轮转、文件锁、fail-soft |
| `meowmeowclaw/agent/loop.py`（改） | 注入可选 `compressor`；每次 chat 前 `prepare_request`；新增一个结束原因；默认 `None` 时行为与现在完全一致 |
| `meowmeowclaw/bootstrap.py`（改） | 在 `agent_factory(session_key)` 内按会话构造 `ContextCompressor`（带 `SessionKey`），与 AgentLoop 同生命周期 |
| `meowmeowclaw/config.py` / `.env.example` / `README.md`（改） | 10 个配置键、默认值、校验、文档表 |

---

## 4. Token 计量设计

### 4.1 `len(text) // 2` 是否可行？——不建议作为预算依据

`len//2` 固定假设"2 字符 = 1 token"，对中英混排会双向失真：

| 文本 | 实际经验值 | `len//2` | 偏差方向 |
|---|---|---|---|
| 中文（CJK，本项目主要输入） | 约 0.5~1 token/字符（常见汉字多为 1 token/字） | 0.5 token/字符 | **低估最多约 2 倍 → 实际仍超窗** |
| 英文/自然语言 | 约 3.5~4 字符/token（≈0.25 token/字符） | 0.5 token/字符 | 高估约 2 倍 → 过度压缩、浪费摘要调用 |
| 代码/JSON（符号密集） | 约 2~3.5 字符/token | 0.5 token/字符 | 多数情况下略高估，少数符号密集片段接近临界 |

结论：`len//2` 只有在"中英各半"时误差才互相抵消，而最坏情况（纯中文）恰好落在**不安全**方向。它最多作为日志里的乐观下界，**不能用于决定"是否超预算"**。

### 4.2 推荐方案：三档 `TokenCounter`

```
TokenCounter (Protocol)
  count_text(text: str) -> int
  count_message(message: dict) -> int
  count_messages(messages) -> int
  count_tools(tool_defs) -> int
  name: str          # 日志/审计里记录实际使用的档位
```

| 档位 | 实现 | 适用 | 依赖 |
|---|---|---|---|
| 精确 A | `TiktokenCounter`：`tiktoken.encoding_for_model()` / 指定 encoding | OpenAI 系列（模型名匹配 `gpt-*` / `o1*` / `o3*` 等） | 可选 `tiktoken` |
| 精确 B | `HFTokenizerCounter`：读取**本地** `tokenizer.json`（`tokenizers.Tokenizer.from_file`） | 用户显式提供 DeepSeek/Qwen 等 tokenizer 文件 | 可选 `tokenizers` |
| 启发式 | `HeuristicCounter`：CJK 加权字符估算（见 §4.3） | 其余全部情况（默认、离线、未知模型、导入失败） | 无 |
| 极简兜底 | 同启发式内部：无法分类时按 `ceil(len(text) * 0.75)` | 空/异常输入 | 无 |

解析顺序（`tokenizer=auto`，由 `build_counter` 负责）：

1. 模型名匹配 tiktoken 映射且 `tiktoken` 可导入 → 精确 A；
2. `tokenizers` 可导入且找到本地 `tokenizer.json`（查找规则见下）→ 精确 B；
3. 否则 → 启发式，并 `logger.warning` 一次（每个进程只提醒一次，避免刷屏）。

**本地 `tokenizer.json` 的存放路径**（新增 `paths.py` 统一解析，其他模块禁止自行拼路径）：

```
<项目根>/tokenizers/                       # DEFAULT_TOKENIZER_DIR，整体加入 .gitignore
├── deepseek-chat/
│   └── tokenizer.json                     # 推荐形态：按模型名建子目录
└── deepseek-ai_DeepSeek-V3.json           # 也接受：<净化模型名>.json 单文件
```

查找规则（`hf_tokenizer_path` 为空时）：

1. `<PROJECT_ROOT>/tokenizers/<sanitized_model>/tokenizer.json`
2. `<PROJECT_ROOT>/tokenizers/<sanitized_model>.json`

其中 `sanitized_model = re.sub(r"[^A-Za-z0-9._-]", "_", model)`（如 `deepseek-ai/DeepSeek-V3` → `deepseek-ai_DeepSeek-V3`）；按顺序取第一个存在的文件。

- `hf_tokenizer_path` 非空时优先：绝对路径原样使用，相对路径按**项目根**解析（与 `workspace` / `memory_dir` 同一约定）；
- 路径不存在/不是文件 → warning + 回退启发式（不报错）；
- `tokenizer=tiktoken` / `tokenizer=hf` 为强制模式：依赖或文件缺失时启动 warning 并回退启发式，仍不阻塞启动；
- **lazy 加载**：精确编码/tokenizer 在第一次计数时才真正打开；打开失败（离线、缓存缺失、文件损坏）→ 一次性 warning + 该实例永久回退启发式，不重试、不阻塞请求。

> 离线约束：v1 **不自动联网下载** tokenizer；DeepSeek 精确计数需要用户自行准备 `tokenizer.json`（如从 HF `deepseek-ai/DeepSeek-V3` 仓库仅取 `tokenizer.json`）并按上述路径放置或配置。任意 OpenAI 兼容端点"普遍精确"不可达，启发式是必须可用的底线。
> 选此路径的理由：tokenizer 是静态模型资产，不属于 Agent 运行时数据，放项目根 `tokenizers/` 可避免污染 `workspace/` 文件沙箱与 `memory/` 审计区。

### 4.3 启发式公式

```
cjk   = count(ch in CJK ranges)          # \u4e00-\u9fff / \u3400-\u4dbf / \uf900-\ufaff / \u3000-\u303f / \uff00-\uffef(全角标点) 等
other = len(text) - cjk
tokens = ceil(cjk * 1.0 + other * 0.3)
```

- 中文按 1 token/字（保守上界），英文/符号按 0.3 token/字符（略高于经验值）；
- 若想要更短的一行实现，可用 `len(text.encode("utf-8")) // 3`（中文 3 字节→1 token，英文 1 字节→0.33 token），同样偏保守，二选一即可；文档默认采用 CJK 加权版，便于以后按语种校准。

### 4.4 每条消息与工具的开销

- 每条消息固定开销 `MESSAGE_OVERHEAD_TOKENS = 4`（role/分隔符）；
- `content` 为 `None`：只计开销；
- assistant `tool_calls`：把 `id + function.name + function.arguments` 按实际发送的 JSON 字符串用 `json.dumps(..., ensure_ascii=False)` 计数，再 +4；
- 工具定义：对 `registry.get_definitions()` 的 JSON 序列化计数（每次请求都发送，不能漏）；
- 未知字段/多模态 content：`str(value)` 后走启发式。

### 4.5 安全系数、预算与校准

- 判定式：`ceil(estimated_input_tokens * 1.1) > token_budget` 即触发压缩（`SAFETY_FACTOR = 1.1`，常量，v1 不做配置）；
- `token_budget` 是**输入**预算，不含输出预留。默认 48000 基于 deepseek-chat 64K 窗口，预留约 16K 给输出与估算误差；换模型必须同步调整（写入 `.env.example` 注释）；
- `LLMResponse.usage.prompt_tokens` 是压缩前那次请求的真实值（`llm/openai_compat.py:184-205` 已解析）。v1 只把"估算 vs 实际"写进日志/HISTORY.md 作为校验数据，不自动反哺系数；自动校准（EWMA 修正因子、按 model 维度缓存）留待 v2。

### 4.6 计数范围（Q3 结论）

**按实际发送内容全量计**：system prompt + 工具定义 + 全部历史消息（user / assistant / tool / tool_calls）+ 当前提问。理由：

- `tool` 消息通常占上下文主体（单条上限 8k~16k 字符、单轮最多 32 次工具往返），只统计 user+assistant 会严重低估；
- 工具定义每次请求都带，也不可忽略；
- "哪些 turn 优先被压缩"另用**按 turn 汇总的全量 token** 决定，user/assistant 裸文本只作为价值判断的辅助信号，不能作为预算口径。

### 4.7 依赖与文件可用性验证（2026-10-09 实测）

| 项 | 结果 |
|---|---|
| 运行环境 | CPython 3.12.3 / linux x86_64；与 `requires-python>=3.10` 兼容 |
| 精确档 A：`tiktoken` | **可用**：`tiktoken 0.14.0` 有 cp312 manylinux wheel，可从 PyPI 下载安装；`encoding_for_model("gpt-4o") → o200k_base` 正常 |
| tiktoken 数据文件 | **首次使用需联网下载**：`cl100k_base` 1.68 MB ≈ 20 s，`o200k_base` 3.61 MB ≈ 60 s（本机网络）；下载后由 `TIKTOKEN_CACHE_DIR`（默认用户缓存）持久化，之后可离线。加载失败必须回退启发式 |
| 精确档 B：`tokenizers` | **可用**：`tokenizers 0.23.2` 有 cp312 wheel，可安装；声明依赖 `huggingface-hub>=0.16.4,<2.0`，会随 optional extra 一并装入 |
| 本地 tokenizer 文件 | **可用**：项目内已有 `<项目根>/tokenizers/deepseek-ai_DeepSeek-V4-Flash/tokenizer.json`（6.37 MB，vocab=129280），`Tokenizer.from_file()` 离线加载约 0.24 s，计数正常 |
| 模型名匹配 | ⚠️ 该文件按 `deepseek-ai/DeepSeek-V4-Flash` 命名；当前 `.env` 的 `model=deepseek-chat` 不会自动命中，需显式 `hf_tokenizer_path=tokenizers/deepseek-ai_DeepSeek-V4-Flash/tokenizer.json`，或按 §4.2 规则补对应模型目录 |

同批样本的精确值与启发式/`len//2` 对比（DeepSeek 系列 tokenizer）：

| 样本 | 字符数 | 精确 tokens | 启发式 `1.0CJK+0.3其他` | `len//2` | 结论 |
|---|---:|---:|---:|---:|---|
| 中文 | 29 | 14 | 28 | 14 | 启发式约高估 2.0×（安全方向） |
| 英文 | 65 | 11 | 20 | 32 | 启发式约高估 1.8× |
| 代码/JSON | 91 | 23 | 28 | 45 | 启发式约高估 1.2× |

结论：两档依赖均可安装、本地文件可用，设计方案不变；三项落地要求：

1. `tiktoken` 首次加载可能耗时/失败 → 计数 lazy 加载、失败一次性告警并永久回落启发式（不阻塞、不重试）；
2. `tokenizers` 的 `huggingface-hub` 传递依赖接受，计入 optional extra 的安装体积；
3. 已放置的 `DeepSeek-V4-Flash` tokenizer 与当前 `deepseek-chat` 模型名不匹配，需显式配置 `hf_tokenizer_path` 或调整目录/`model` 后才走精确档。

---

## 5. 压缩算法设计

### 5.1 处理顺序（Q2 结论：轮数裁剪仍需保留，前后各一道）

```
① store.load_recent(max_turns=20, max_chars=50000)      # 现有粗筛（摘要前，保护摘要输入）
② 组装 + 估算（全量口径）
③ 未超预算 → 原样发送，不做任何摘要/改写
④ 超预算 → 压缩"最旧的可压缩完整 turn"为摘要
⑤ 重新估算 → 仍超 → 硬删最旧完整 turn（必要时连摘要一起丢）+ 占位提示
⑥ 仍超 → 当前轮工具结果占位
⑦ 仍超 → context_overflow 错误结束本轮（completed=False，不落盘）
```

为什么不能只靠摘要替代轮数裁剪：

1. 摘要是机会性的（调用可能失败、超时、被配置关闭），硬裁剪是确定性的最终保证；
2. 摘要成功后 system + 工具 + 最近 K 轮 + 摘要本身仍可能超预算，需要收尾循环；
3. `max_turns/max_chars` 同时承担成本、保留期与隐私策略，并防止把巨量历史喂给摘要模型；
4. 无限保留 + 纯摘要会导致摘要不断滚动重写、信息漂移、每次都可能触发额外 LLM 调用。

顺序结论：**粗筛在摘要前**（不白花摘要调用），**token 硬裁在摘要后**（最终保险）；二者都以"完整 turn"为单位，绝不切开 `tool_calls` ↔ `tool` 配对。

### 5.2 turn 切分与保护规则

```
input:  messages（事实源，system 在下标 0）
system  = messages[0]
last_user_idx = 最后一个 role=="user" 的下标
history = messages[1:last_user_idx]          # 只含完整历史 turn
current = messages[last_user_idx:]           # 当前轮（含工具循环中间消息），受保护
turns   = split_turns(history)               # 每个元素以 user 开头
compressible = turns[:-keep_recent_turns]    # 最旧的若干完整 turn
```

- `keep_recent_turns` 默认 2，下限 1（即使硬裁也至少保留 1 个旧 turn，除非预算实在放不下）；
- 当前轮永远不摘要；如果 `len(turns) <= keep_recent_turns`，**本轮不做摘要**，直接进入硬裁/工具占位路径；
- `split_turns` 对缺失 user 的畸形历史做容错：丢弃前导非 user 片段（记 warning），保证不会切出悬空的 tool 消息。

### 5.3 摘要调用

| 项 | 规定 |
|---|---|
| 调用对象 | 同一 `provider`，`model = summary_model or 主 model` |
| 参数 | `tools=None`，不传工具定义；`max_tokens=summary_max_tokens`（默认 768；P3 为 `LLMProvider.chat` 新增可选 `max_tokens`，`OpenAICompatProvider` 透传，未传时不带该参数） |
| 超时 | `asyncio.wait_for(..., timeout=summary_timeout)`（默认 15s；Provider 签名不变） |
| 次数 | 每次压缩事件最多 1 次；失败不重试 |
| 递归防护 | 直接调用 `provider.chat`，**绝不经过 AgentLoop**，因此不会再次进入压缩路径 |
| 输入构造 | 按角色裁剪后序列化：user 全文上限 2000 字符、assistant content 2000、assistant tool_calls 名称+参数 300、tool 结果 head 300 + tail 300；总上限 `SUMMARY_INPUT_CHARS = 24000`（常量）。仍超则逐步压缩 tool 片段为一行；再超则放弃摘要走硬裁 |
| 提示词要求 | 保留：用户偏好、已确认结论、未完成任务、关键文件路径、工具执行结果要点、时间线；禁止编造；中文输出；若有上一版摘要则合并 |
| 输出校验 | 去空白后非空；`counter.count_text(summary) < 0.8 × 被替换内容的 token`（收益阈值）；否则视为无效 |

### 5.4 摘要消息形态与位置

- 形态：单条 `{"role": "system", "content": "[历史摘要] <summary>\n（此摘要覆盖最早 N 轮对话）"}`；
- 位置：请求视图中紧跟在主 system prompt 之后、剩余原文 turn 之前；
- 选择 `system` 的理由：不打乱 user/assistant 交替；OpenAI / DeepSeek 兼容端点接受多 system 消息；对 prefix caching 友好（摘要不变时前面公共前缀稳定）；
- 不写入事实源、不写 JSONL；`AgentTurn.messages` / `messages[new_start:]` 中永远不会出现摘要。

### 5.5 滚动摘要缓存（会话内）

- 缓存内容：`summary_text` + `covered_message_count` + `prefix_hash`（被覆盖前缀的 sha256），存放在 `ContextCompressor` 实例内；
- 每个 `ContextCompressor` 与 `SessionKey` 绑定、随 AgentLoop 缓存于 `ConversationService._agents`，会话生命周期内有效；进程重启即失效（可接受，重启后首次超预算重摘一次）；
- 命中条件：本次要压缩的前缀与 `prefix_hash` 一致 → 直接复用摘要，不再调用模型；
- 未命中/历史增长：把"旧摘要 + 新增的可压缩 turn"一起交给摘要模型做**合并摘要**，覆盖范围单调扩张；
- 未超预算时**不主动摘要**（不额外烧钱），仍由 `max_turns/max_chars` 粗筛控制上限。

### 5.6 降级矩阵（Q1 结论）

按优先级从高到低，全部 fail-soft、只记日志：

| 级别 | 触发 | 行为 |
|---|---|---|
| L0 正常 | 估算 ≤ 预算 | 原样发送，零 LLM 调用 |
| L1 摘要 | 估算 > 预算且有可压缩 turn | 1 次摘要调用 → 摘要 system 消息替换最旧 turn → 采纳需同时满足"非空 + 收益达标" |
| L2 硬裁 + 占位 | 摘要调用失败 / 超时 / 空 / 收益不足 / 摘要输入超限 | 丢弃同批最旧完整 turn，替换为一条 **system 角色**的 `[历史省略] 因上下文预算，最早的 N 轮对话已省略。`；不重试 |
| L3 当前轮工具占位 | L2 后仍超，且当前轮存在 tool 消息 | 从最旧开始把 `tool.content` 换成 `[工具结果已省略: 上下文预算不足]`，保留 role / tool_call_id / 其余字段（结构合法） |
| L4 失败结束 | L3 后仍超（system + 当前提问本身过大），或已无可裁内容 | 返回 `AgentTurn(completed=False, finish_reason="context_overflow")`，answer 提示用户拆分问题或 `/clear`；本轮不写 JSONL |

补充规则：

- L2 的占位消息同样是请求视图产物，**不落盘**；JSONL 里原始完整 turn 不受影响；
- 摘要结果若把总预算压下去了但没到目标，不二次摘要，直接进入 L2（防止一次请求内反复调用）；
- HISTORY.md 记录 L1 与 L2/L3/L4 事件（见 §7），便于核对"到底是摘要了还是丢了"。

### 5.7 单轮内工具循环超限

- 每次 `provider.chat` 前的估算都会发现当前轮工具结果增长导致的超限；
- 处理优先级：**先对旧历史做 L1/L2（若还有可压缩 turn）→ 再对当前轮做 L3 内容占位 → 最后 L4**；
- L3 只改请求视图里的 `content`，不改 assistant 的 `tool_calls`、不改 `tool_call_id`，保证下一轮请求依旧配对合法；
- 工具循环上限 `max_iterations=32` 与单工具输出上限保持不变，作为最外层护栏。

---

## 6. Q1 / Q2 / Q3 结论速览

| 问题 | 结论 |
|---|---|
| Q1 摘要失败怎么降级 | 单路径级联：摘要失败/无效 → 硬删同批最旧完整 turn + 固定占位提示 → 继续请求；仅日志；输入/输出双重预算 + 收益阈值 + 1 次不重试 |
| Q2 还要不要轮数裁剪 | 要。保留 `max_turns/max_chars` 放**摘要前**做粗筛；token 驱动的硬裁放**摘要后**做最终保证；都以完整 turn 为单位 |
| Q3 计算整个历史还是只算 user+assistant | 按实际请求全量计（system + 工具定义 + 所有 role 消息 + 当前提问）；tool 与 tool_calls 绝不能漏；压缩选择再按 turn 汇总全量成本 |

---

## 7. `HISTORY.md` 审计日志

### 7.1 定位

- **仅审计/人工校验**：记录每次压缩/降级的"原文 + 摘要 + 时间戳 + 估算数据"；
- 不注入 System Prompt（与 `MEMORY.md` 不同）；
- 压缩逻辑不读回它（不是摘要缓存，缓存只在内存）；
- 不写 JSONL、不改 `SessionStore` 协议。

### 7.2 位置与格式

`<memory_dir>/HISTORY.md`（默认 `<workspace>/memory/HISTORY.md`）。追加式 Markdown，一次压缩事件一个 section：

```markdown
## 2025-10-09T09:12:33.412Z | session=cli:session:<uuid> | storage=<storage_id> | event=summary | result=ok

- 触发: estimated_input_tokens=61203 > budget=48000 (counter=heuristic-cjk, safety=1.1)
- 替换: 最早 12 个 turn / 47 条消息；保留最近 2 个 turn 原文
- 摘要: model=deepseek-chat, summary_tokens≈812, elapsed=1.8s
- 压缩后: estimated_input_tokens=21780

### 摘要

<summary text>

### 原文（单条记录上限 history_log_original_chars 字符；完整原文见 sessions/<storage_id>.jsonl）

[
  {"role": "user", "content": "..."},
  {"role": "assistant", "tool_calls": [...]},
  {"role": "tool", "tool_call_id": "...", "content": "...(截断标记)"}
]
```

- 时间戳：`utc_now_ms()` / `ms_to_iso()`（UTC ISO8601，与记忆系统一致）；
- `event`：`summary` / `fallback_trim` / `tool_elision` / `context_overflow`；`result`：`ok` / `fallback` / `failed`；
- 原文 dump：整条记录的 JSON 超过 `history_log_original_chars` 时按比例 head/tail 截断并附 `"(审计原文截断)"` 标记；完整原文始终以 `sessions/<storage_id>.jsonl` 为准；
- 会话标识用 `SessionKey.canonical` + `storage_id`，便于与 JSONL 对齐。

### 7.3 写入实现

- 追加写：`asyncio.to_thread` 打开 `"a"` + `encoding="utf-8"`；
- 并发：复用 `memory/filelock.py` 的 `async_file_lock(HISTORY.md.lock, timeout=5.0)`，覆盖"读大小 → 轮转 → 追加"全过程；同会话另有 `ConversationService` 串行锁；
- 失败语义：任何 `OSError` 只 `logger.warning`，压缩/回复流程继续（与记忆 fail-soft 一致）；
- 不在事件循环里做同步 I/O。

### 7.4 轮转与隐私

- 大小上限 `history_log_max_bytes`（默认 2 MiB）；超限时 `HISTORY.md → HISTORY.md.1`（单代覆盖）后新建，避免无界增长；
- HISTORY.md 含原始对话内容，属敏感数据：`workspace/` 已在 `.gitignore`（第 220 行）内，不会误提交；
- **已确认**：把 `HISTORY.md` 纳入 `filesystem.py` 的记忆运行时保护名单，对 `read_file` / `write_file` / `list_dir` 禁止访问，避免模型篡改审计记录；`MEMORY.md` 的现有可读写策略不变；
- 实现方式：`is_memory_denied` 由"目录名单"扩展为"目录名单 + 运行时文件名单"，`HISTORY.md`、`HISTORY.md.1`、`HISTORY.md.lock` 全部命中；`memory` 根目录**列举**仍拒绝，`MEMORY.md` 读写照旧放行。

---

## 8. 配置项（`.env`）

| 键 | 默认值 | 说明 |
|---|---|---|
| `compression_enabled` | `true` | 总开关；`false` 时完全退回现状（不估算、不压缩） |
| `token_budget` | `48000` | 输入 token 预算（估算 × 1.1 后比较）；基于 64K 窗口、预留约 16K 输出与误差 |
| `tokenizer` | `auto` | `auto` / `tiktoken` / `hf` / `heuristic`；`tiktoken:<encoding>` 可强制指定 |
| `hf_tokenizer_path` | 空 | 可选覆盖；空时按 §4.2 顺序查找 `<项目根>/tokenizers/...`；相对路径按项目根解析；仅在 `tokenizers` 可导入时生效 |
| `keep_recent_turns` | `2` | 至少保留原文的最近完整 turn 数；下限 1 |
| `summary_model` | 空 | 摘要模型；空 = 使用主 `model` |
| `summary_max_tokens` | `768` | 摘要输出上限（同时用于收益校验参考） |
| `summary_timeout` | `15` | 摘要调用超时（秒），浮点 |
| `history_log_max_bytes` | `2097152` | HISTORY.md 轮转阈值（字节） |
| `history_log_original_chars` | `32000` | 单条审计记录中原文 JSON 的字符上限 |

- 解析沿用 `config.py` 现有模式：默认值常量、`_KEY_ALIASES` 别名、非法/越界回退默认并 warning；
- `compression_enabled` 接受 `true/false/1/0/yes/no`（大小写不敏感）；
- `token_budget` 过小（如 < 1000）按非法值回退默认并 warning，避免把正常对话全部压光；
- README 配置表与 `.env.example` 同步新增说明。

---

## 9. 代码集成点

### 9.1 `AgentLoop`（最小侵入）

- `__init__` 增加 `compressor: Optional[ContextCompressor] = None`（默认 `None`，既有测试/直接构造行为不变）；
- 循环内把 `provider.chat(messages, ...)` 改为：

```
request_messages = messages
if self.compressor is not None:
    request_messages = await self.compressor.prepare_request(messages, self.tools.get_definitions())
    # 内部含：估算 → 历史压缩 → 当前轮工具占位
response = await self.provider.chat(request_messages, tools=..., model=self.model)
```

- `prepare_request` 未超预算时**返回原列表对象**（零改写/零额外调用）；压缩详情见 `compressor.last_outcome`（`changed` / `estimated_tokens` / `dropped_turns` / `still_over_budget`），P5 的溢出错误路径据此判断；
- **事实源 `messages`、`new_start`、`AgentTurn.messages`、`_session_history` 全部不动**（C1）；现有 `run()` / `run_turn()` / 错误与护栏语义零变化；
- 新增结束原因常量 `FINISH_REASON_CONTEXT_OVERFLOW = "context_overflow"`（loop 层常量，与 `max_iterations` / `circuit_break` 同级；`ConversationService` 只认 `completed`，无需改判断）。

### 9.2 `bootstrap`

- `agent_factory(session_key)` 内构造 `TokenCounter`（全局共享，无状态）+ `HistoryAuditLog`（全局共享）+ `ContextCompressor(session_key=..., counter=..., provider=..., model=..., tools=..., config=...)`，注入 AgentLoop；
- `compression_enabled=false` 时**不构造 counter/compressor，直接向 AgentLoop 传 `None`**（零开销）；
- 摘要调用复用同一个 `provider` 实例与连接池；`summary_model` 空时复用主 model；
- P3 已按此装配；`audit_log` 暂为 `None`，P4 注入 `HistoryAuditLog`。

### 9.3 `config.py` / `paths.py` / 文档

- `Settings` 增加上述 10 字段与 `__repr__`（不打印敏感信息，本组无密钥）；
- `paths.py` 增加 `DEFAULT_TOKENIZER_DIR = PROJECT_ROOT / "tokenizers"` 与 `resolve_tokenizer_path(raw_value, model)`（唯一路径解析入口）；
- `.gitignore` 增加 `tokenizers/`（本地模型资产，避免误提交大文件；确需入库可 `git add -f`）；
- `README.md` 配置表、`.env.example` 增加注释样例（含 tokenizer.json 放置示意）；
- `docs/MEMORY_DESIGN.md` §5.4"摘要压缩：不做"更新为"由 CONTEXT_COMPRESSION_DESIGN 承担"。

### 9.4 依赖

- 核心：零新增硬依赖（启发式永远可用）；
- 可选：`pyproject.toml` 已加入 `[project.optional-dependencies] tokenizers = ["tiktoken>=0.7", "tokenizers>=0.19"]`；
- 实测（§4.7）：`tiktoken 0.14.0` 首次使用需联网下载编码文件（可缓存）；`tokenizers 0.23.2` 传递依赖 `huggingface-hub`；两者均有 CPython 3.12 manylinux wheel；
- 安装 extra 后，DeepSeek 等模型仍需自备 `<项目根>/tokenizers/<净化模型名>/tokenizer.json`（或配置 `hf_tokenizer_path`）才会启用精确计数；项目内已有 `deepseek-ai_DeepSeek-V4-Flash` 一份，但与当前 `model=deepseek-chat` 不自动匹配；
- 文档说明离线限制：无 cache、无文件时自动走启发式。

---

## 10. 日志与错误语义

| 事件 | 级别 | 关键字段 |
|---|---|---|
| 触发压缩 | INFO | session、估算/预算、counter.name、压缩 turn 数 |
| 摘要成功 | INFO | summary_tokens、elapsed、压缩后估算、cache=hit/miss |
| 摘要失败/降级 | WARNING | 失败原因（timeout/error/empty/no_gain/input_too_long）、实际降级级别 |
| 使用启发式分词 | WARNING（每进程一次） | model、原因（无精确 tokenizer） |
| HISTORY.md 写失败 | WARNING | path、异常 |
| 预算极限 | WARNING + 返回错误 | finish_reason=context_overflow，answer 提示拆分或 `/clear` |

- 不向用户回复插入"[已压缩]"，仅日志（C10）；
- 摘要调用失败不写 `finish_reason="error"`，不污染本轮完成状态。

---

## 11. 测试策略

| 层 | 用例 |
|---|---|
| `tokenizer` | 中文启发式保守性（≥ `len//2`）、英文不过度离谱、CJK 边界字符、`content=None`、tool_calls JSON 计数、工具定义计数、缺 `tiktoken`/`tokenizers` 时自动降级、本地 tokenizer.json 缺失降级、`name` 正确、模型名净化与两个默认候选路径的命中顺序、相对 `hf_tokenizer_path` 按项目根解析 |
| `split_turns` | 标准 user/assistant/tool 序列、连续 user、前导 tool 畸形容错、最后 user 起受保护、`tool_calls` ↔ `tool` 不被切开 |
| `ContextCompressor` 正常路径 | 未超预算：零 provider 调用、返回原消息；超预算：1 次摘要调用（`tools=None`、`max_tokens`、超时生效）；摘要 system 消息位置正确；事实源不变 |
| 缓存 | 相同前缀第二次触发走 cache（0 次调用）；历史增长后合并摘要；重启/换会话缓存失效 |
| 降级矩阵 | 摘要 error/timeout/空 → L2 硬裁 + 占位；摘要无收益 → L2；L2 后仍超 → L3 tool 占位且 `tool_call_id` 保留；system+当前提问本身超 → L4 `context_overflow`、`completed=False`、不落盘 |
| `AgentLoop` 集成 | 注入 compressor 后既有 run/run_turn 测试全绿；`new_start` 切片与 `AgentTurn.messages` 不含摘要；JSONL 中无摘要/占位；compressor=None 行为与现状 bit 级一致 |
| `HISTORY.md` | 记录含 UTC 时间戳/session/估算/摘要/原文；原文超限截断；并发追加不交错；轮转生成 `.1`；写失败 fail-soft；不被 Prompt 注入 |
| `filesystem` 策略 | `HISTORY.md` / `HISTORY.md.1` / `HISTORY.md.lock` 三件套被拒；`MEMORY.md` 读写与备份行为不变 |
| `config` / `paths` | 10 个键默认值/别名/非法值回退/开关语义；`resolve_tokenizer_path` 的净化名、候选顺序、相对/绝对路径、缺失回退；README 表与默认值一致 |

回归要求：现有 633 用例保持全绿；新增用例默认不联网（精确分词用例用 mock/本地 fixture）。

---

## 12. 实施阶段（建议提交顺序）

| 阶段 | 内容 | 完成标准 |
|---|---|---|
| P1 | `llm/tokenizer.py` + `paths.resolve_tokenizer_path` + config 10 键 + `.env.example` + `.gitignore` | 计数、路径解析与配置单测通过 |
| P2 | `agent/compression.py`：切分、估算、请求视图、未超预算短路、（无摘要的）硬裁兜底 + loop 注入 | 现有测试全绿；预算不足路径可控 |
| P3 | 摘要调用 + 滚动缓存 + L1~L2 降级 + 审计日志接入 | 降级矩阵用例通过 |
| P4 | `HISTORY.md` 完整实现（锁/轮转/fail-soft）+ filesystem 保护策略 | 审计与策略用例通过 |
| P5 | 当前轮 L3 工具占位 + L4 `context_overflow` + README/设计文档收尾 | 全量回归 + 手工验证（真实 DeepSeek 长会话） |

---

## 13. 已确认结论（含依赖验证）

| # | 结论 |
|---|---|
| 1 | 启发式公式采用 `ceil(CJK×1.0 + 其他×0.3)`；`len//2` 不作为预算依据 |
| 2 | `HISTORY.md` 对模型三件套**禁读写**（`MEMORY.md` 现有策略不变） |
| 3 | 单条审计记录原文上限 32k 字符；超出 head/tail 截断并指向 `sessions/<storage_id>.jsonl` |
| 4 | `token_budget=48000` 写入 `.env.example` 作为默认值 |
| 5 | 接受可选 extra `tiktoken` + `tokenizers`；两档依赖与本地 tokenizer 文件已验证可用（§4.7） |
| 6 | L4 文案固定为"上下文超出预算，请拆分问题或使用 /clear" |
| 7 | `tokenizer.json` 路径约定：`<项目根>/tokenizers/<sanitized_model>/tokenizer.json`（备选单文件形态），`tokenizers/` 加入 `.gitignore`；项目内已按此放置 `deepseek-ai_DeepSeek-V4-Flash/tokenizer.json` |

**遗留操作项（不阻塞编码）**：已放文件对应 `deepseek-ai/DeepSeek-V4-Flash`，而当前 `.env` 是 `model=deepseek-chat`；使用精确档前需二选一：

- 在 `.env` 设置 `hf_tokenizer_path=tokenizers/deepseek-ai_DeepSeek-V4-Flash/tokenizer.json`；或
- 让 `model` 与目录名一致/补放 `tokenizers/deepseek-chat/tokenizer.json`。

确认完毕，按 §12 的 P1 → P5 顺序开始编码。
