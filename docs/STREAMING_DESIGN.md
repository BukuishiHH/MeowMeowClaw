# MeowMeowClaw 流式输出设计（v1：Provider 流式 / CLI 逐字 / QQ 伪流式）

> 状态：**设计已确认（2026-10-12），按 §9 里程碑实施**。本文定义流式输出的行为、契约、里程碑与验收标准；
> 只新增能力，**不改 JSONL 存储格式、不改 AgentLoop 护栏与上下文压缩语义**。
> 首批范围 **S1–S4**（地基 / 网关通道 / CLI 流式 / QQ typing）；S5(`block`) 与 S6 收尾在用户真机验证后再排期；
> 进度回填 §9.3 与 §12。
> 关联：`meowmeowclaw/llm/base.py`、`meowmeowclaw/llm/openai_compat.py`、`meowmeowclaw/agent/loop.py`、
> `meowmeowclaw/conversation.py`、`meowmeowclaw/gateway/`、`meowmeowclaw/channels/`、
> `docs/GATEWAY_DESIGN.md`、`docs/MEMORY_DESIGN.md`、`docs/CONTEXT_COMPRESSION_DESIGN.md`。

---

## 0. 背景与目标

### 0.1 背景（现状审计，代码级）

| 位置 | 现状 | 对流式的影响 |
|---|---|---|
| `llm/base.py:47` | 唯一入口 `LLMProvider.chat()`（抽象方法），无流式概念 | 需新增 **非抽象** `chat_stream()`，默认回退到 `chat()`，既有 Provider / 测试替身零破坏 |
| `llm/openai_compat.py:106` | `chat.completions.create(stream=False)` + `_parse_completion()` | 需流式分支：增量 `content`、按 `index` 拼装 `tool_calls` 分片、合成同一 `LLMResponse` |
| `agent/loop.py:189` | 每次迭代 `await provider.chat()` 一次拿全量 | 需 `on_delta` 注入点；工具轮的"前导文本"需要抑制策略（§4.3） |
| `conversation.py:108-168` | `handle_message()` 在会话锁内：装历史 → `run_turn` → 仅完整轮写盘 | 仅透传 `on_delta`；**持久化/锁语义不变** |
| `gateway/envelope.py:14-22` | kind 仅 `request/reply/command/push/error`，无流式类型 | 需新增 `KIND_STREAM`（有默认值的新字段，不破坏 schema） |
| `gateway/worker.py:25-43` | `AgentWorker.handle()` 不持有 bus | 由 `GatewayDispatcher` 注入 `on_delta` 回调（dispatcher 本就持有 bus） |
| `gateway/dispatcher.py:166-168` | `_publish_outbound()` 全量强语义（失败进死信） | delta 需 **best-effort**：超时直接丢弃，**不进死信**，避免刷爆排障队列 |
| `channels/cli_adapter.py:52-57` | `send()` 直接打印整段并唤醒 `ask()` | 需水位线：delta 打印后，最终 `reply` 只打印剩余后缀 |
| `channels/onebot11.py:454+` | `_call_api()` 目前只发 `send_private_msg` | 需新增 `set_input_status`（可选 `delete_msg` / `send_private_forward_msg`） |

> 结论：流式是**纵向贯穿 7 层**的改造（llm → agent → conversation → gateway → bus → adapter → 平台），
> 必须先把契约和里程碑冻结，再分层落地；任何一层单独改都会留下重复输出或语义裂缝。

### 0.2 目标

| # | 目标 | 验收指标 |
|---|---|---|
| G1 | Provider 具备流式能力且向后兼容 | `chat_stream()` 默认回退可用；未覆写的 Provider 行为与 `chat()` 完全一致 |
| G2 | CLI 逐字输出 | 首字延迟显著低于整段；**最终不重复打印**（拼接后与完整回答一致） |
| G3 | QQ 有"生成中"体感且不刷屏 | `typing` 模式：中间气泡 **0 条**；`block` 模式：单轮中间气泡 **≤ `qq_stream_max_messages`(默认 5)** |
| G4 | 内容完整性 | 已发送内容恒为最终回答的**前缀**；最终消息只补发剩余部分，绝不重复 |
| G5 | 零回归 | `stream_enabled=false` 时行为、API 调用序列、输出与现状逐字节一致；全量回归基线 `1096 passed` 不下降 |
| G6 | 可观测/可降级 | `typing`/`block` 任何一步失败均静默降级到"整段发送"，不影响最终答案送达 |

### 0.3 非目标（v1 不做）

- **不做 QQ 的 token 级流式**（"一个字一个气泡"），架构上不提供该档位；
- **不做 QQ 消息编辑**（平台无此 API，见 §1.2）；
- **默认不做 `draft`（撤回重发）模式**，仅保留能力评估（§2 方案 D，默认关闭且不建议开启）；
- 不改 `ConversationService` 的锁/装载/回写语义，不改 `ContextCompressor` 的摘要调用（摘要始终走非流式 `chat()`）；
- 不把 `reasoning_content`（思考过程）外发给任何渠道；
- 不做群聊流式（群聊策略本就不在 v1 范围）。

---

## 1. 平台能力约束（基于本机 NapCat 实测勘察）

勘察对象：`D:\NapCat.Shell\NapCat.Shell\napcat.mjs`（Windows 侧运行中的 NapCat.Shell，OneBot11 动作枚举共 **183** 个）。

### 1.1 可用能力清单

| 能力 | Action | 实测关键实现 | 本设计中的用途 |
|---|---|---|---|
| 发私聊消息 | `send_private_msg` | 支持消息段数组；**每条 = 一个新气泡** | 最终回答 / 分段块发送 |
| **设置输入状态** | `set_input_status` | payload `{user_id, event_type}`（示例 `event_type: 1`）→ `sendShowInputStatusReq({chatType: KCHATTYPEC2C, peerUid}, event_type)`；NapCat 自身以 `notice_type=notify, sub_type=input_status, status_text="对方正在输入..."` 上报同类事件 | ⭐ QQ 渠道"流式体感"主力（`typing` 模式） |
| 撤回消息 | `delete_msg` | `recallMsg(peer, msgId)`；QQ 协议撤回窗口 **2 分钟**，且对端可见"撤回了一条消息" | `draft` 备选方案（默认不做） |
| 贴表情 | `set_msg_emoji_like` | `{message_id, emoji_id, set}` | 可选：对用户消息贴 👀 表示"已收到"（本设计不启用） |
| 私聊合并转发 | `send_private_forward_msg` | `{user_id, messages:[节点]}`，渲染为"聊天记录"卡片（需点开） | 备选：超长回答压成 1 个气泡（本设计不启用） |

> 待实测项：`event_type` 仅见示例值 `1`（推测 1=开始输入，是否存在 0/2=结束输入未知）。
> 实现阶段（S4）先用真实 NapCat 发一条请求并观察 QQ 客户端表现，结论回填本文 §12。

### 1.2 硬约束：QQ 无"消息编辑"

- 183 个动作中**不存在** edit / modify / update-message 类接口；
- 因此"同一条气泡内文本持续增长"（CLI / Telegram / Slack 式真流式）在 QQ 上**物理不可行**；
- 一切 QQ 侧"流式"只能是：① 缩短首字延迟的多气泡**分段呈现**；② "正在输入"状态指示；③ 撤回重发（有副作用）。

---

## 2. 备选方案对比

以"一个 800 字回答"为标尺：

| 方案 | 气泡数 | 额外噪音 | 首字延迟 | 风控风险 | 失败模式 | 结论 |
|---|---|---|---|---|---|---|
| A. token 直发 | 300~800 | 刷屏，不可用 | 最低 | **极高** | — | ❌ 明确排除 |
| B. `typing` 仅指示器 | **1** | 无 | 无渐进（但有"正在输入"） | 无 | `set_input_status` 失败 → 静默降级 | ⭐ **推荐默认** |
| C. `block` 语义分段 | **2~6**（硬上限） | 消息条数增多，符合 QQ 聊天习惯 | 1~3s 见首块 | 低（节流+封顶） | 中途断线 → 剩余内容由最终消息补发 | ⭐ 可选开启 |
| D. `draft` 撤回重发 | 2×N + N 条"撤回提示" | **每次更新一条撤回提示** | 1~3s | 中高 | 超 2 分钟撤回失败 → 残留多条草稿 | ❌ 建议不实现（保留文档评估） |
| E. 合并转发卡片 | 1（卡片） | 需点开才见内容 | 无渐进 | 低 | — | 备选，v1 不启用 |

---

## 3. 设计范围与已确认决策（2026-10-12 用户确认）

| # | 决策 | 结论 | 状态 |
|---|---|---|---|
| S-D1 | QQ 默认呈现形态 | **`typing`**（0 中间气泡 + "正在输入"）；`block` 先作为显式开关，**待真机使用满意后再讨论翻默认** | 已确认 |
| S-D2 | `draft`（撤回重发） | **不实现**，仅在本文保留评估结论 | 已确认 |
| S-D3 | 工具轮"前导文本" | 默认**抑制**（缓冲 + 宽限窗口，见 §4.3）；`stream_suppress_tool_preamble=false` 可放开 | 已确认 |
| S-D4 | `set_input_status` 失败语义 | 静默降级（debug 日志），不影响正常回复；`qq_typing_indicator` 可一键关闭 | 已确认 |
| S-D5 | CLI 流式是否同批实现 | **是**（与 QQ 共用同一套水位线，避免重复输出） | 已确认 |
| S-D6 | 首批实施范围 | **S1→S4**（地基 / 网关 / CLI / QQ typing）；S5(`block`) 与 S6 收尾由用户在真机验证后决定排期 | 已确认 |
| S-D7 | 配置命名与默认值 | 按 §6 表格执行 | 已确认 |

> 启用方式：S1–S4 完成后默认 `stream_enabled=false`（零行为变化）；
> 用户真机验证通过后，将 `.env` 的 `stream_enabled=true` 打开即可获得 CLI 逐字 + QQ "正在输入"。

---

## 4. 详细设计

### 4.1 总体数据流

```
                    inbound:<channel>
  平台事件 ──► Adapter ──publish──► Dispatcher ──policy──┐
                                       │                 │
                          (1) phase=start (KIND_STREAM)  │
                                       ▼                 ▼
                              outbound:<channel>    AgentWorker.handle(on_delta=...)
                                       │                 │
                                       │        provider.chat_stream() ── delta 流
                                       │                 │
                          (2) phase=delta (KIND_STREAM) ◄┘   [best-effort]
                                       │
                                       ▼
                              ChannelRenderer
                              ├─ typing : 只维护"正在输入"续期，不发中间气泡
                              ├─ block  : BlockSegmenter 语义分片 + 节流 → send()
                              └─ off    : 忽略
                                       ▲
                          (3) phase=end + kind=reply (强语义, 必达一次)
                                       │
                              渲染器只补发"未发送的后缀"
```

要点：

- 复用一个 `outbound:<channel>` 队列，**不新建队列**；
- `phase=start/end` 与 `kind=reply` 是强语义（进死信、可观测）；`phase=delta` 是 best-effort；
- 渲染器按 `correlation_id` 维护独立状态，`end` 时清理（TTL + 容量上限防泄漏）。

### 4.2 LLM 层契约（新增，向后兼容）

```python
# meowmeowclaw/llm/base.py

@dataclass(frozen=True)
class StreamTextDelta:
    """文本增量; reasoning_content 永不出现在这里(不对外呈现思考过程)."""
    text: str

@dataclass(frozen=True)
class StreamFinal:
    """流结束: 复用现有 LLMResponse(含 tool_calls / finish_reason / usage)."""
    response: LLMResponse

StreamEvent = Union[StreamTextDelta, StreamFinal]

class LLMProvider(ABC):
    @abstractmethod
    async def chat(self, ...) -> LLMResponse: ...          # 现状, 不变

    async def chat_stream(self, messages, tools=None, model=None, max_tokens=None):
        """默认实现: 调 chat() 后吐一个 StreamFinal.
        未覆写的 Provider(含测试替身)自动降级为"非流式", 契约不破坏。
        """
        yield StreamFinal(await self.chat(messages, tools=tools, model=model, max_tokens=max_tokens))
```

`OpenAICompatProvider.chat_stream()` 实现要点：

| 项 | 规则 |
|---|---|
| 请求 | 与 `chat()` 同参 + `stream=True`；`stream_options={"include_usage": True}` 仅当服务端支持（失败则回退不带该参数重试一次） |
| content | `choices[0].delta.content` 非空 → `yield StreamTextDelta(text)` |
| tool_calls | 按 `delta.tool_calls[i].index` 对齐累加：`id`/`function.name` 取首次非空值，`function.arguments` 逐片 **字符串拼接**（不能逐片 `json.loads`），结束后统一解析 |
| finish_reason | 取最后一个非空 chunk 的值；缺省 `stop` |
| usage | 优先取带 `usage` 的尾块；缺失给 `{}` |
| 异常 | 包装为 `finish_reason="error"` 的 `LLMResponse`：**已生成文本保留**并追加 `\n\n[流式输出中断: <原因>]` |
| 空流 | 一个 delta 都没有且无 tool_calls → 回退调用一次 `chat()`（兼容不支持流式的网关） |

### 4.3 Agent 层：`run_turn(on_delta=...)` 与工具前导抑制

```python
async def run_turn(self, user_message, *, history=None, on_delta: Optional[Callable[[str], Awaitable[None]]] = None) -> AgentTurn
```

- `on_delta=None`（默认）→ 行为与现状**逐字节一致**（仍调用 `provider.chat()`，不启用流式）；
- `on_delta` 非空且 `stream_enabled=true` → 该次迭代走 `provider.chat_stream()`；
- **压缩器不变**：`ContextCompressor.prepare_request()` 照常在流式前执行，摘要调用走非流式 `chat()`。

**工具轮前导抑制（S-D3）**：模型可能在决定调用工具前先输出一段旁白（"我先查一下…"）。

```
每个 provider 响应维护 preamble_buffer:
  ├─ 收到 text delta → 暂存缓冲(不外发)
  │    ├─ 缓冲达到 preamble_grace_chars(默认 40) 且未发现 tool_calls → 放行缓冲并转为"直通模式"
  │    └─ 达到 preamble_grace_ms(默认 1500ms) 且未发现 tool_calls → 同上
  ├─ 收到 tool_calls delta → 丢弃整个缓冲, 该响应标记为"工具轮"(后续 delta 也丢弃)
  └─ 响应结束:
       ├─ 有 tool_calls → 不外发任何该响应的文本
       └─ 无 tool_calls(最终回答) → 放行缓冲, 进入直通模式
```

- **残余风险（必须写进文档与运维认知）**：若模型先连续输出 > `grace` 的正文、随后才决定调用工具，那部分文本已发出且 QQ 无法编辑/收回（撤回会产生提示）；
- 保守派开关：`stream_suppress_tool_preamble=true`（默认）为上述启发式；设为 `false` 表示"工具轮文本照发"（用于调试或偏好过程可见的场景）；
- 防爆护栏、`max_iterations`、`FINISH_REASON_*` 语义全部不变。

### 4.4 编排层：`ConversationService`

```python
async def handle_message(self, key, user_message, *, meta=None, on_delta=None) -> ConversationResult
```

- 仅把 `on_delta` 透传给 `agent.run_turn`；
- 会话锁、历史装载、`persisted` 判定、fail-soft 全部不变；
- 任何 `on_delta` 抛出的异常都不得影响本轮结果（渲染侧自身 try/except 兜底）。

### 4.5 网关层：`KIND_STREAM` 与 best-effort 发布

新增信封类型（`gateway/envelope.py`）：

```python
KIND_STREAM = "stream"     # 加入 VALID_KINDS 与 _OUTBOUND_KINDS

# 复用字段: channel/target_channel/conversation_id/session_id/message_id(生成)/correlation_id/reply_to
# 新增字段: delta_seq: int = 0        # 同一 correlation 内单调递增, 便于排障与乱序断言
# 语义约定: metadata["phase"] ∈ {"start", "delta", "end"}; delta 时 text=增量文本
```

| phase | 产生方 | 时机 | 队列语义 |
|---|---|---|---|
| `start` | `GatewayDispatcher._spawn_agent()` | 派发 agent 任务前 | 强语义（进死信） |
| `delta` | `AgentWorker.handle()` 经由 dispatcher 注入的 `on_delta` | 每次文本增量 | **best-effort**：`bus.publish_best_effort()`，队列满/超时直接丢弃 + debug 日志，**不进死信** |
| `end` | `GatewayDispatcher._handle_agent()` | worker 完成后、发布 `reply` **之前** | 强语义 |

- `AsyncioQueueBus` 新增 `publish_best_effort(queue_key, envelope) -> bool`（不写死信、超时更短，默认 0.5s）；`MessageBus` 协议保持向后兼容（调用侧 `getattr` 兜底，缺失时退化为普通 `publish`）；
- `AgentWorker.handle(request, decision, *, on_delta=None)`：仅当传入回调且 `decision.action == ACTION_AGENT` 时启用；
- **去重不变**：`DedupCache` 只作用于 `INBOUND` 入站信封，流式出站信封不参与去重；
- **关闭语义不变**：`end` 与 `reply` 都在 `Gateway.stop()` 的排空范围内；delta 允许在关闭时丢失。

### 4.6 渲染层：`channels/streaming.py`（CLI / QQ 共用）

```python
class StreamAssembler:
    """平台无关的水位线 + 前缀校验。保证: 已发送内容 ≙ 最终回答前缀。"""

    def on_delta(self, text: str) -> None: ...      # 累积(由子类决定是否立即发送)
    def plan_final(self, full_text: str) -> list[str]:
        """返回最终仍需发送的文本分片: full_text[len(prefix):] 按 max_chars 切;
        若 full_text 不以 prefix 开头(罕见: 网关重试/内容被改写) → 记录 warning 并整段重发(接受一次重复)。"""
    def set_sent(self, text: str) -> None: ...      # 更新水位线(子类发送成功后调用)

class BlockSegmenter:
    """纯函数: 增量文本 → 可发送的语义块。可 100% 单测。"""
    def push(self, text: str) -> None: ...
    def take(self, *, force: bool = False) -> Optional[str]: ...

class TypingKeepAlive:
    """QQ 专用: start → set_input_status(1) → 每 refresh 秒续期 → end/超时停止。"""
```

**`BlockSegmenter` 边界规则（从高到低优先级）**

1. `\n\n`（段落边界）
2. `\n`（换行）
3. 句末标点：`。！？!?；;…`
4. 逗号类：`，,、`（仅当缓冲 ≥ `min_chars × 1.5`）
5. 无任何边界且缓冲 ≥ `hard_chars` → 强制硬切

**保护规则**

- 反引号代码围栏（```）未闭合时**不切**，直到闭合或触发 `hard_chars`；
- 不在 URL / 数字内部切（如 `3.14`、`1,000`、`http://...`）；
- 单块仍受 `qq_message_chunk_chars` 限制（块内再分片）。

**节流（三层防御，彻底消灭"一字一气泡"）**

```python
def _should_flush(self) -> bool:
    if self.mode != "block":                    return False
    if now - self.last_sent_at < min_interval:  return False   # 时间节流
    if self.messages_sent >= max_messages:      return False   # 条数硬上限
    floor = first_chars if self.messages_sent == 0 else min_chars
    return len(self.buffer) >= floor and self.segmenter.has_boundary()
```

> 数学结论：单轮中间气泡数 **≤ `qq_stream_max_messages`**（默认 5），与模型吐字速度无关；
> 无论流式如何表现，最终 `reply` 都会把**剩余全部内容**补齐。

### 4.7 QQ 适配器新增动作（`channels/onebot11.py`）

| 方法 | 动作 | 说明 |
|---|---|---|
| `set_input_status(user_id, event_type=1)` | `set_input_status` | best-effort；失败仅 debug 日志；`event_type` 语义 S4 真机确认 |
| `recall(message_id)`（**S-D2 决定不实现**时才不写） | `delete_msg` | 仅 `draft` 方案需要，默认不实现 |

- 复用既有 `_call_api()`（echo 配对 + 超时），但 typing 调用使用更短超时（如 3s）且**不阻塞出站主循环**：由 `TypingKeepAlive` 独立任务驱动；
- `OneBot11Adapter` 消费 `phase=start/end`：`start` → 启动续期任务；`end`/停止 → 取消任务。

### 4.8 CLI 适配器

```
on_delta(text)        → print(text, end="", flush=True); assembler.set_sent(text)
kind=reply 到达       → 只打印 plan_final(full_text) 的剩余分片 + 换行; 唤醒 ask() 的 Future
kind=stream(start/end)→ 忽略(start 可选打印 "[生成中...]" 前缀, 由 cli_stream 控制)
```

- `CliAdapter.ask()` 的 `_pending` **只在 `kind=reply` 时被唤醒**（现状扩展）；
- 打字机效果与最终补发共用同一水位线，杜绝"流式打完又整段重打"。

---

## 5. 不变量与错误语义

| # | 不变量 | 保障方式 |
|---|---|---|
| I1 | **前缀不变量**：任一时刻已发送内容 ≙ 最终回答的前缀 | 渲染器水位线 + `plan_final()` 前缀校验 |
| I2 | **最终答案必达一次** | `kind=reply` 走现有强语义通道；流式失败不阻断 |
| I3 | **持久化只写完整轮次** | `ConversationService` 逻辑不变（`completed=False` 不落盘） |
| I4 | **零回归** | `stream_enabled=false` 时不产生任何 stream 信封、不调用 `chat_stream()` |
| I5 | **单会话串行** | 现有 `ConversationService` 会话锁保证不会出现两条流交叉 |
| I6 | **思考过程不外发** | `StreamTextDelta` 只承载 `content`，`reasoning_content` 仅保留在 `LLMResponse` |
| I7 | **渠道隔离** | 渲染器按 `correlation_id` 分状态；QQ 私聊语义（仅主人）不变 |
| I8 | **关闭/背压安全** | delta 可丢（best-effort），`start/end/reply` 不丢；死信队列不被流式污染 |

错误语义：

- 流中断：已生成文本保留 + `[流式输出中断: …]`，`finish_reason=error` → 本轮 `completed=False`（不落盘），但用户仍能看到已生成内容与中断提示；
- 渲染侧发送失败：单条失败只 warning（沿用 `BaseChannelAdapter` 语义），最终 `reply` 会补齐剩余部分；
- typing 失败/超时：静默降级（debug），不影响回复；
- 前缀不一致：warning + 整段重发（可接受的一次重复，概率极低）。

---

## 6. 配置项（新增）

| 键 | 默认值 | 说明 |
|---|---|---|
| `stream_enabled` | `false` | 全局流式总开关；`false` 时完全走现有非流式路径（零回归） |
| `cli_stream` | `true` | CLI 逐字打印（仅在 `stream_enabled=true` 时生效） |
| `qq_stream_mode` | `typing` | `off` / `typing` / `block` / `draft`（`draft` 默认不实现） |
| `qq_typing_indicator` | `true` | 是否调用 `set_input_status` |
| `qq_typing_refresh_seconds` | `5` | "正在输入"续期间隔 |
| `qq_stream_first_chars` | `60` | `block` 模式首块最小字数（越小首字越快） |
| `qq_stream_min_chars` | `80` | `block` 模式后续块最小字数 |
| `qq_stream_min_interval` | `2.5` | `block` 模式两条气泡最小间隔（秒） |
| `qq_stream_max_messages` | `5` | 单轮中间气泡硬上限 |
| `qq_stream_hard_chars` | `900` | 无自然边界时的强制切点 |
| `stream_preamble_grace_ms` | `1500` | 工具轮前导抑制宽限（毫秒） |
| `stream_preamble_grace_chars` | `40` | 工具轮前导抑制宽限（字符） |
| `stream_suppress_tool_preamble` | `true` | `false` = 工具轮文本照发（调试用） |

解析沿用 `config.py` 现有约定：**系统环境变量 > .env > 默认值**；非法值回退默认并 warning；
`.env.example` 同步，密钥类字段不在 `Settings.__repr__` 中出现。

---

## 7. 测试策略

| 层 | 用例要点 |
|---|---|
| `llm/openai_compat` 流式 | delta 拼接、`tool_calls` 按 index 分片拼装（id/name/arguments 跨块）、usage 提取、空流回退、异常中断（保留已生成文本） |
| `llm/base` | `chat_stream()` 默认回退实现；未覆写 Provider 与 `chat()` 输出一致 |
| `agent/loop` | `on_delta=None` 行为不变；最终回答轮 delta 直通；工具轮前导抑制（宽限字符/时间/后续才出现 tool_calls 的残余场景）；护栏与 `max_iterations` 不变 |
| `conversation` | `on_delta` 透传；`persisted/completed` 判定不变；回调异常不影响结果 |
| `gateway` | `KIND_STREAM` 校验；`start/delta/end` 顺序与字段；delta best-effort 丢弃（不写死信）；`end` 清理；`stream_enabled=false` 不发任何 stream 信封 |
| `channels/streaming` | 边界规则表驱动（段落/换行/句末/逗号/硬切/围栏保护/URL 保护）；节流（注入假时钟）；条数封顶；前缀不变量；前缀不一致回退 |
| `channels/onebot11` | `set_input_status` 请求构造与 echo 配对；失败静默降级；`start/end` 驱动续期任务生命周期 |
| `cli_adapter` | delta 打印、最终只打印后缀、`ask()` 只被 `reply` 唤醒、Ctrl+C 语义不变 |
| 端到端（假 NapCat） | `stream_enabled=true` + `qq_stream_mode=block`：气泡数 ≤ 上限、拼接文本 == 完整回答、无重复；`typing` 模式：0 中间气泡 |
| 回归 | `stream_enabled=false` 全量回归 **≥1096 passed**；`ruff check` 通过 |

---

## 8. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 工具轮前导文本"先发后知" | 少量过程旁白留在 QQ 聊天里且无法编辑 | 缓冲 + 宽限抑制（§4.3）；提供 `stream_suppress_tool_preamble` 开关；文档明示残余风险 |
| `set_input_status` 为 NapCat 私有扩展 | 换版本/换实现可能不可用 | 视为可选能力：失败静默降级；`qq_typing_indicator=false` 可一键关闭 |
| QQ 风控/限流 | 连续多发消息可能被限制 | 三层节流 + `qq_stream_max_messages` 封顶；默认 `typing` 形态零额外消息 |
| 出站队列被慢 `send` 阻塞 | delta 延迟、最终消息排队 | delta best-effort 丢弃；最终 `reply` 仍走强语义；QQ `send` 等待 echo 有超时 |
| 流与最终文本不一致（网关重试/内容改写） | 内容重复或缺失 | 前缀校验 + 整段重发兜底（I1） |
| 长回答 × 分块 × 3000 字上限 | 块内二次分片错乱 | `BlockSegmenter` 输出再经 `chunk_text()`；不变式测试覆盖 |
| 关闭/重启期间的流 | 半截消息 | `end/reply` 在优雅关闭排空范围内；delta 允许丢弃 |
| `stream_enabled=false` 回归 | 行为漂移 | S1–S5 每个里程碑都跑全量回归 + 契约测试 |

---

## 9. 里程碑与排期

### 9.1 里程碑总览

| 里程碑 | 名称 | 范围（交付物） | 完成定义（DoD） | 用户可见变化 |
|---|---|---|---|---|
| **S1** | 流式地基 | `llm/base.py` 事件模型 + `chat_stream()` 默认回退；`openai_compat.py` 真流式；`agent/loop.py::run_turn(on_delta=...)` + 前导抑制；`conversation.py` 透传 | 流式单测全绿；`stream_enabled=false` 全量回归 ≥1096 passed 不变；无任何渠道改动 | **无** |
| **S2** | 网关流式通道 | `envelope.KIND_STREAM`；`bus.publish_best_effort()`；`dispatcher/worker` 的 `start/delta/end`；`channels/streaming.py::StreamAssembler` | 网关层单测（顺序/best-effort/清理）；无渠道消费方 → 行为不变 | **无** |
| **S3** | CLI 流式 | `CliAdapter` delta 打印 + 最终只补后缀；`config.py` 新增 `stream_enabled/cli_stream`；CLI 启动信息 | 交互 + 管道两种方式验证；无重复尾巴；关闭开关逐字节一致 | CLI 逐字输出 |
| **S4** | QQ：`typing` 指示器 | `onebot11.set_input_status` + `TypingKeepAlive` + adapter 消费 `start/end`；`qq_stream_mode=typing` | **真机**：QQ 客户端出现"对方正在输入…"；最终仍是 1 条完整气泡；NapCat 不支持时静默降级；指令不触发 typing | QQ 有"生成中"体感 |
| **S5** | QQ：`block` 语义分段 | `BlockSegmenter` + 节流/封顶/兜底 + `qq_stream_*` 配置 | 单测（边界/围栏/上限/前缀不变量）+ 真机：800 字回答 ≤6 条气泡、拼接一致、无重复 | QQ 渐进呈现 |
| **S6** | 收尾 | README / `.env.example` / `GATEWAY_DESIGN.md` 同步；真机冒烟记录；可选 `Gateway.stats()` 流式计数 | 文档齐全；ruff + 全量回归通过；冒烟记录回填 §12 | — |

**明确不做（v1）**：`draft` 撤回重发（S-D2）、合并转发卡片、emoji 秒回 ack、群聊流式、token 级直发。

### 9.2 依赖关系

```
S1 ──► S2 ──► S3 ──► S4 ──► S5 ──► S6
        │      └──────┴──────┘
        └─ S2 不依赖任何渠道；S4/S5 可并行开发（不同文件: onebot11.py vs streaming.py），但建议串行提交
```

| 里程碑 | 依赖 | 预计工时 | 可并行 |
|---|---|---|---|
| S1 | — | 1.5–2d | 否（地基） |
| S2 | S1 | 1–1.5d | 否 |
| S3 | S2 | 0.5–1d | 可与 S4 并行 |
| S4 | S2 | 1d（含真机验证） | 可与 S3 并行 |
| S5 | S2 | 1.5d（含真机观感调参） | 否 |
| S6 | S3+S4(+S5) | 0.5d | 否 |

### 9.3 进度追踪（实现时回填）

| 里程碑 | 状态 | 提交 | 完成日期 | 备注 |
|---|---|---|---|---|
| S1 流式地基 | 已完成 | 待回填 | 2026-10-12 | 新增 31 用例；全量 1127 passed |
| S2 网关流式通道 | 未开始 | — | — | — |
| S3 CLI 流式 | 未开始 | — | — | — |
| S4 QQ typing | 未开始 | — | — | 依赖真机验证 `event_type` |
| S5 QQ block | 待排期 | — | — | S1–S4 真机验证通过后决定 |
| S6 收尾 | 待排期 | — | — | 文档/冒烟随 S1–S4 增量同步 |

状态取值：`待确认 → 未开始 → 进行中 → 已完成 / 已放弃（附原因）`；每个里程碑**独立提交**，提交前必须满足：
① `ruff check` 通过；② 全量回归不低于基线；③ 该里程碑新增测试全绿；④ 回填本表与 §12。

### 9.4 各里程碑详细 DoD

**S1（流式地基）**

- [ ] `StreamTextDelta` / `StreamFinal` / `chat_stream()` 默认回退；`LLMProvider` 子类无需改动即可工作
- [ ] `OpenAICompatProvider.chat_stream()`：content 增量、tool_calls 分片拼装、usage、空流回退、异常中断
- [ ] `AgentLoop.run_turn(on_delta=...)`；`on_delta=None` 时**调用路径与现状完全一致**（可用测试断言"未调用 chat_stream"）
- [ ] 前导抑制三种场景单测：宽限内出 tool_calls（丢弃）/ 宽限后出 tool_calls（已放行部分保留 + warning）/ 正常最终回答（直通）
- [ ] `ConversationService.handle_message(on_delta=...)` 透传 + 回调异常隔离
- [ ] 全量回归 ≥1096 passed；ruff 通过

**S2（网关流式通道）**

- [ ] `KIND_STREAM` 加入 `VALID_KINDS` / `_OUTBOUND_KINDS`，`delta_seq` 字段默认 0
- [ ] `AsyncioQueueBus.publish_best_effort()`：不写死信、超时丢弃；`MessageBus` 协议向后兼容
- [ ] dispatcher 在派发前发 `start`、完成后发 `end`（`end` 在 `reply` 之前）
- [ ] `delta` 只在 `on_delta` 存在时发布；发布失败不影响 agent 结果
- [ ] `StreamAssembler`：水位线 / `plan_final()` / 前缀不一致回退，单测覆盖
- [ ] `stream_enabled=false` 时**零 stream 信封**（测试断言总线队列为空）
- [ ] 全量回归 ≥1096 passed

**S3（CLI 流式）**

- [ ] 交互模式逐字输出；管道模式（`echo "..." | python -m meowmeowclaw`）输出完整可读
- [ ] 最终 `reply` 只打印后缀；自动化测试断言"输出文本 == 完整回答"（无重复、无缺失）
- [ ] `_pending` 仅在 `kind=reply` 时唤醒；`/help`、`/exit`、Ctrl+C 行为不变
- [ ] `stream_enabled=false` 或 `cli_stream=false` 时输出与现状逐字节一致
- [ ] 全量回归 ≥1096 passed

**S4（QQ typing）**

- [ ] `set_input_status` 请求构造正确（echo 配对、短超时、失败静默）
- [ ] `TypingKeepAlive`：start 触发、按 `qq_typing_refresh_seconds` 续期、end/异常/关闭时取消；**无任务泄漏**
- [ ] 真机：向机器人发一条需要数秒生成的消息 → QQ 客户端显示"对方正在输入…"；最终 1 条完整气泡
- [ ] 真机：`/help` 等指令**不触发** typing（无 agent 任务）；`qq_typing_indicator=false` 时完全不调用
- [ ] 记录 `event_type` 实测结论到 §12
- [ ] 全量回归 ≥1096 passed

**S5（QQ block）**

- [ ] `BlockSegmenter` 表驱动单测：段落/换行/句末/逗号/硬切/代码围栏/URL/数字
- [ ] 节流：假时钟验证最短间隔、最小字数、条数封顶；封顶后剩余内容全部由最终消息承载
- [ ] 端到端（假 NapCat）：气泡数 ≤ `qq_stream_max_messages + 1`；拼接文本与完整回答一致；无重复
- [ ] 真机：800 字级回答观感确认；观察是否有风控/限流迹象；参数回填 §6 默认值（若需调整，更新本文与 `.env.example`）
- [ ] 全量回归 ≥1096 passed

**S6（收尾）**

- [ ] README「流式输出」章节 + 配置表；`.env.example` 同步
- [ ] `docs/GATEWAY_DESIGN.md` §5/§10/§16 增加方框（流式通道与 §10 配置项）
- [ ] 真机冒烟记录写入 §12（含 CLI 与 QQ 两种形态）
- [ ] ruff + 全量回归最终确认

---

## 10. 验收清单（真机）

| # | 场景 | 期望 | 状态 |
|---|---|---|---|
| V1 | CLI 交互提问（长回答） | 首字 < 1s 出现；逐字增长；结束后无重复段落 | 待验收 |
| V2 | CLI 管道输入 | 输出完整、可读、无 ANSI 乱码 | 待验收 |
| V3 | CLI `/help` `/exit` | 与现状一致 | 待验收 |
| V4 | QQ `typing` 模式：普通提问 | 出现"对方正在输入…"；最终 1 条完整回答 | 待验收 |
| V5 | QQ `typing` 模式：`/help` | 秒回帮助文本；不出现 typing | 待验收 |
| V6 | QQ `block` 模式：800 字回答 | 气泡数 ≤6；内容拼接 == 完整回答；无重复 | 待验收 |
| V7 | QQ `block` 模式：含代码块回答 | 代码块不被切断 | 待验收 |
| V8 | 断线/重启演练 | 重新连入后会话续接；无残留半截消息 | 待验收 |
| V9 | `stream_enabled=false` 回归 | 所有渠道行为与流式前一致 | 待验收 |
| V10 | 连续 10 轮 private 问答 | 无风控提示、无死信堆积（`Gateway.stats()` 死信=0） | 待验收 |

---

## 11. 决策确认记录（Q1–Q7，2026-10-12）

| # | 问题 | 结论 |
|---|---|---|
| Q1 | QQ 默认呈现形态 | **`typing`**（0 中间气泡 + "正在输入"）；`block` 作为开关，真机使用满意后再讨论默认开启 |
| Q2 | 是否实现 `draft`（撤回重发） | **不实现**（撤回提示噪音 + 2 分钟窗口限制） |
| Q3 | 工具轮前导文本 | **抑制**（缓冲 + 宽限），残余风险已在 §8 明示 |
| Q4 | `set_input_status` 失败语义 | **静默降级**（debug 日志），提供 `qq_typing_indicator` 开关 |
| Q5 | CLI 是否同批改造 | **是**（共用 `StreamAssembler`，避免重复输出） |
| Q6 | 首批实施范围 | **S1→S4**（地基 / 网关 / CLI / QQ typing）；S5(`block`) 由真机验证后决定 |
| Q7 | 配置命名与默认值 | 按 §6 表格执行 |

> 用户确认原文（要点）："Q1 默认，待后期使用满意后默认开启 block；Q2–Q5（完成 S1–S4 的实现，
> 并由我进行实机测试后决定何时开启）、Q6、Q7：默认。"
> 即：**先实现 S1–S4，交付后由用户真机验证，再决定何时打开开关与是否排期 S5/S6**。

---

## 12. 实施记录（待更新）

### 12.1 里程碑提交记录

| 里程碑 | 提交 | 日期 | 关键变更 | 回归 |
|---|---|---|---|---|
| S1 | 待回填 | 2026-10-12 | `llm/base.py` 流式事件与默认回退；`openai_compat.chat_stream`（tool_calls 分片/usage/空流回退/中断保文本/400 重试）；`AgentLoop.run_turn(on_delta)` + 工具轮前导抑制；`ConversationService` 透传；新增 `tests/test_stream_llm.py`(17) / `tests/agent/test_loop_stream.py`(11) / `tests/test_conversation.py`(3) | ruff 通过；1127 passed / 6 skipped / 2 xfailed（基线 1096） |
| S2 | — | — | — | — |
| S3 | — | — | — | — |
| S4 | — | — | — | — |
| S5 | — | — | — | — |
| S6 | — | — | — | — |

### 12.2 真机实测结论

| 项 | 结论 | 日期 |
|---|---|---|
| NapCat 动作枚举（183 个）中无消息编辑 API | 已确认（源码勘察） | 2026-10-12 |
| `set_input_status` 的 `event_type` 语义 | 待 S4 实测 | — |
| `block` 模式观感与风控表现 | 待 S5 实测 | — |

### 12.3 参数标定记录

| 参数 | 初值 | 真机标定值 | 依据 |
|---|---|---|---|
| `qq_stream_min_interval` | 2.5s | — | 观感 + 风控观察 |
| `qq_stream_max_messages` | 5 | — | 观感 |
| `qq_stream_first_chars` | 60 | — | 首字延迟 |
