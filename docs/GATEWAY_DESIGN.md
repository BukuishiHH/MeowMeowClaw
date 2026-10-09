# MeowMeowClaw 多渠道网关设计（v1：单进程 asyncio.Queue 总线）

> 状态：**设计稿，已确认决策，待实现**。本文只定义网关层行为与契约，不改 JSONL 存储格式、
> 不改 `ConversationService` / `AgentLoop` 的内部语义。
> 关联：`docs/ARCHITECTURE.md`、`docs/MEMORY_DESIGN.md`、`docs/CONTEXT_COMPRESSION_DESIGN.md`、
> `meowmeowclaw/channels/`、`meowmeowclaw/conversation.py`、`meowmeowclaw/bootstrap.py`、`meowmeowclaw/cli.py`。

---

## 0. 背景与目标

### 0.1 现状（交付层直连）

```
平台事件 → 传输适配器(未实现) → 渠道服务(QqPrivateService)
              ├─ 渠道策略: 身份过滤 / 6h 轮换 / 指令 / 权限
              └─ 直接 await ConversationService.handle_message(key, text, meta)
              ← List[OutgoingMessage]（同步返回）
```

- `channels/base.py` 只有 `IncomingMessage` / `OutgoingMessage` 两个 DTO，没有适配器契约、总线与路由；
- 渠道服务同时承担"平台策略"与"调用 Agent"两件事；
- CLI 完全旁路渠道层，直接在 `cli.py` 调 `ConversationService`；
- 核心（Agent/记忆）对渠道零依赖，但"渠道与 Agent 的进程/生命周期"没有解耦。

### 0.2 本次目标

在**单进程 asyncio** 内引入 `asyncio.Queue` 消息总线，把"渠道适配器"与"Agent 消费者"解耦：

- 任意渠道（QQ / 飞书 / Web / CLI）通过适配器把平台事件转成统一 `Envelope` 发布到入站队列；
- 网关调度器做去重、渠道策略与会话路由，交给 Agent Worker；
- Agent 回复封装为统一 `Envelope` 发布到 `outbound:<channel>`，由对应适配器发回平台；
- CLI 与 QQ、飞书一样经过总线（本版确认），保持 REPL 交互语义；
- 全部现有能力（会话隔离、跨渠道删除、压缩、审计、fail-soft）不退化。

### 0.3 非目标（v1 不做，接口预留）

- 外部消息中间件（Kafka/NATS/Redis Streams）、多进程/多机 Worker；
- 队列持久化、exactly-once、跨重启消息恢复；
- 流式分片、媒体附件（图片/语音/文件）、多租户配额；
- 主动推送的**生产方**（`kind=push` 与出站路由预留，v1 不产生）；
- 真实 OneBot/NapCat/飞书 SDK 传输实现（本阶段只做契约与 Fake/CLI 适配器）。

---

## 1. 已确认决策记录

| # | 决策 | 结论 |
|---|---|---|
| G1 | 代码位置 | 新增 `meowmeowclaw/gateway/` 子包；`channels/` 保留平台 DTO 与渠道策略 |
| G2 | Envelope | 新建内部 `Envelope`；`IncomingMessage` / `OutgoingMessage` 作为适配器边界 DTO 长期保留 |
| G3 | 队列拓扑 | `inbound` 单队列 + `outbound:<channel>` 每渠道一队列 + `deadletter` 预留 |
| G4 | 消费模型 | 单 `GatewayDispatcher` + 每条请求 `create_task`，`Semaphore(64)` 限并发 |
| G5 | 顺序语义 | **跨会话并发**；同会话 FIFO（dispatcher 到达顺序 + 现有 `ConversationService` 锁） |
| G6 | 背压 | 每队列 `maxsize=1000`；publish 超时 5s，超时进死信 + warning |
| G7 | 幂等去重 | 进程内 LRU（4096）按 `(channel, message_id)`；无 message_id 则内部生成 UUID、不参与去重；v1 不持久化 |
| G8 | 策略归属 | `ChannelPolicy` 在 dispatcher 侧；QQ 轮换/指令、群聊权限都属策略层，Agent 不感知渠道 |
| G9 | CLI 路径 | **CLI 与 QQ/飞书一样经过总线**；`CliAdapter` + `CliPolicy`，REPL 交互语义保持不变 |
| G10 | 错误语义 | 沿用现状：模型/记忆错误返回错误文本；内部异常只日志，不打断 dispatcher |
| G11 | 推送/多模态 | v1 只实现 request/reply/command；`push` 类型与出站路由预留，媒体/流式不做 |
| G12 | 开关与配置 | `gateway_enabled=false`（opt-in）；`gateway_bus_maxsize=1000`；`gateway_publish_timeout=5`；`gateway_shutdown_timeout=10` |
| G13 | bootstrap 集成 | `Application` 增加可选 `gateway`；独立 `build_gateway(app)`；`Application.close()` 优雅关闭 |
| G14 | 测试范围 | bus 单测、FakeAdapter 端到端、QqPolicy/CliPolicy 迁移测试、背压/去重/关闭；真实平台传输不在本阶段 |
| G15 | 会话隔离 | 采用**方案 A**（`ConversationService` 独占运行时：锁 + AgentLoop 缓存 + 装载/回写）；**方案 B**（Gateway 缓存独立 AgentLoop/常驻上下文）列为备选，当前不采用；量化对比与升级阈值见 §15 ADR-1 |

---

## 2. 架构总览

```
平台(QQ / 飞书 / Web / CLI ...)
      │ ① 平台事件
      ▼
 ChannelAdapter ──publish(Envelope)──► inbound queue ──► GatewayDispatcher
      ▲                                                      │
      │ ⑦ send(envelope)                       ② Dedup + ChannelPolicy.resolve()
      │                                                      │
 outbound:<channel> ◄──publish(reply)── ③ AgentWorker (ConversationService.handle_message)
```

| 组件 | 职责 | 依赖 |
|---|---|---|
| `Envelope` | 网关内部统一消息（见 §3） | 无 |
| `MessageBus` / `AsyncioQueueBus` | 队列读写、背压、死信、关闭 | asyncio |
| `ChannelAdapter` | 入站：平台事件 → Envelope → `inbound`；出站：消费 `outbound:<channel>` → 平台发送 | `MessageBus` |
| `ChannelPolicy` | 入站信封 → `ignore / reply / agent`；身份、触发、轮换、指令、权限 | `ConversationService`（仅查询/命令） |
| `GatewayDispatcher` | 单任务消费 `inbound`：去重 → 策略 → 直发/派发 Worker | bus、policy、worker |
| `AgentWorker` | 调 `ConversationService.handle_message`，回复封装为 `kind=reply` | `ConversationService` |
| `Gateway` | 组装与生命周期：启动、排空、in-flight、优雅关闭 | 以上全部 |

**模块划分（目标）**

```
meowmeowclaw/gateway/
├── __init__.py         # 导出 Gateway/Envelope/...（不放具体实现 import 副作用）
├── envelope.py         # Envelope / MessageKind / 构造与校验辅助
├── bus.py              # MessageBus Protocol + AsyncioQueueBus + 队列键常量
├── adapter.py          # ChannelAdapter Protocol + BaseChannelAdapter(出站循环)
├── policy.py           # ChannelPolicy Protocol + PolicyDecision + 公共命令辅助
├── dedup.py            # DedupCache(LRU)
├── worker.py           # AgentWorker
├── dispatcher.py       # GatewayDispatcher
└── gateway.py          # Gateway 生命周期与组装
```

`channels/` 侧新增/改造：`channels/cli_policy.py`、`channels/cli_adapter.py`、`channels/qq_policy.py`；
`QqPrivateService` 保留为兼容 facade（见 §9）。

---

## 3. Envelope 协议

```python
@dataclass(frozen=True)
class Envelope:
    kind: str                        # request / reply / command / push / error
    channel: str                     # 入站=来源渠道；出站=目标渠道
    scope: str = "private"           # private / group / session
    conversation_id: str = ""        # QQ uin / 群 group_id / CLI session / Web 会话
    text: str = ""
    message_id: str = ""             # 平台消息 ID（去重键）；入站缺失时内部生成 UUID
    correlation_id: str = ""         # 请求-回复配对（回复=入站 message_id）
    session_id: Optional[str] = None # 渠道会话实例（QQ 轮换后的 session_id）
    sender_id: str = ""
    reply_to: Optional[str] = None   # 平台回复目标（消息 ID / 线程 ID）
    received_at_ms: int = 0          # 平台时间；缺失由适配器补当前时间
    created_at_ms: int = 0           # 网关内部时间
    target_channel: Optional[str] = None  # 出站路由键；入站可为 None
    metadata: dict[str, Any] = field(default_factory=dict)
```

约定：

- **入站**：适配器产出 `kind="request"`、`channel`=来源、`target_channel=None`；
- **出站**：`kind="reply"`、`target_channel`=来源渠道、`correlation_id`=入站 `message_id`、
  `conversation_id/session_id/reply_to` 原样带回；
- **直发命令回复**：`kind="reply"`，由 Policy 构造，`metadata["source"]="policy"`；
- **错误回复**：`kind="error"`，走同一出站队列（v1 仅日志，不额外造文案）；
- **主动推送**：`kind="push"`，只要求 `target_channel + conversation_id + text`，v1 预留；
- `message_id` 是幂等键；`correlation_id` 只用于配对，不参与去重；
- Envelope 是不可变对象；跨队列传递不做深拷贝（单进程内约定只读）。

---

## 4. 消息总线

### 4.1 队列键

| 键 | 方向 | 消费者 |
|---|---|---|
| `inbound` | 适配器 → dispatcher | `GatewayDispatcher`（单任务） |
| `outbound:<channel>` | dispatcher/worker → 适配器 | 对应 `ChannelAdapter`（每渠道一个消费任务） |
| `deadletter` | 失败/超时 → 调试 | 人工/日志（v1 不实现自动重放） |

### 4.2 契约

```python
class MessageBus(Protocol):
    async def publish(self, queue_key: str, envelope: Envelope) -> bool: ...
    async def get(self, queue_key: str) -> Envelope: ...
    def task_done(self, queue_key: str) -> None: ...
    async def close(self) -> None: ...
```

- 实现 `AsyncioQueueBus`：`dict[str, asyncio.Queue]` 按需创建；队列 `maxsize=gateway_bus_maxsize`；
- `publish`：`await asyncio.wait_for(queue.put(env), gateway_publish_timeout)`；超时→尝试放入
  `deadletter`（best-effort）并返回 `False`，调用方决定是否提示；
- `close`：拒绝新 publish，唤醒阻塞的 `get`（`get` 抛 `GatewayClosedError`），清空队列；
- 不做 ack/nack：单进程内异常由 worker/dispatcher 捕获，避免引入无意义的投递语义。

### 4.3 背压与死信

- 所有队列有界；慢适配器只会堵自己的 `outbound:<channel>`，不阻塞 dispatcher 和其他渠道；
- `inbound` 满时适配器 `publish` 等待/超时；超时消息进死信并 warning；
- 死信 v1 仅内存保留最近 N 条（默认 128）供排障，进程退出即丢。

---

## 5. 适配器契约

```python
class ChannelAdapter(Protocol):
    channel: str
    async def start(self, bus: MessageBus) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, envelope: Envelope) -> None: ...
```

- `BaseChannelAdapter` 提供出站消费循环：`start` 时创建任务读 `outbound:<channel>`，逐条 `await self.send(env)`，
  异常只 warning（单条失败不杀循环）；
- 入站辅助：`make_inbound(...)` 生成 Envelope 并 `await bus.publish(INBOUND, env)`；
- 适配器只做协议转换与收发，**不做会话策略**（策略在 Policy 层）；
- 生命周期：`start` 幂等；`stop` 取消出站任务并等待结束；不得在 `send` 中做无超时阻塞。

### 5.1 CLI 适配器（G9）

CLI 与 QQ/飞书同路径，但保留 REPL 语义：

```
CliAdapter.start()
 ├─ _outbound_loop(): env = await bus.get("outbound:cli")
 │      print(env.text)  →  resolve(self._pending[env.correlation_id])
 └─ _read_loop():
        text = await asyncio.to_thread(input, "> ")   # 不阻塞事件循环
        if text in ("/exit", "/quit"): 触发 Gateway 关闭
        message_id = uuid4().hex
        future = loop.create_future(); pending[message_id] = future
        publish(inbound Envelope(kind=request, channel="cli", ...))
        await future                                # 等回复打印后再显示下一个提示符
```

- `/exit`、`/quit` 属 REPL 本地命令，由适配器处理，不发布；
- 其余 `/` 指令（`/help /new /clear /sessions /skills`）**发布进总线**，由 `CliPolicy` 处理；
  CLI 每次进程启动创建新 `SessionKey(channel="cli", scope="session", conversation_id=<uuid>)`，
  `/new` 换新 uuid，`/clear` 归档当前 + 换新（与现有 `cli.py` 行为对齐）；
- banner / 启动信息仍由交付层 `cli.py` 在 `gateway.start()` 前打印；
- Ctrl+C：`cli.py` 捕获后调用 `await gateway.stop()`，取消 pending future。

### 5.2 QQ / 飞书适配器（后续）

- `QqAdapter`：真实 OneBot/NapCat 传输适配（本阶段先留接口与 Fake 实现）；
- `FeishuAdapter`：webhook/长连接事件 → Envelope；回复需要 `message_id/reply_to` 与租户 token，属适配器细节；
- 所有适配器必须能通过同一份 **Adapter 契约测试**（FakeAdapter 先跑通）。

---

## 6. 会话策略层

```python
@dataclass(frozen=True)
class PolicyDecision:
    action: str                              # "ignore" / "reply" / "agent"
    replies: tuple[Envelope, ...] = ()       # action=reply 时直发
    pre_replies: tuple[Envelope, ...] = ()   # action=agent 时先发（如"已开始新对话"）
    session_key: Optional[SessionKey] = None
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

class ChannelPolicy(Protocol):
    channel: str
    async def resolve(self, envelope: Envelope) -> PolicyDecision: ...
```

- `ignore`：非本人 / 非私聊 / 空文本（QQ 现有语义）；
- `reply`：`/help`、`/sessions`、跨渠道 `/clear <id>` 等，直接产出回复 Envelope；
- `agent`：携带 `SessionKey`、清洗后的 `text`、`meta={"channel","scope","sender_id"}`；
  `pre_replies` 由 dispatcher 在派发前先发（QQ 超时轮换的"已开始新对话"）；
- **公共命令辅助**：把 `/new /clear /sessions` 的通用逻辑（列表、短 ID 解析、跨渠道归档/删除、当前会话判定）
  抽成共享 helper，QQ/CLI 各自注入"新建会话实例"的回调，避免两份实现漂移；
- **QQ 策略**：从现有 `QqPrivateService` 抽出身份过滤、active 指针、6h 惰性轮换、指令；
  `QqPrivateService` 退化为兼容 facade（内部 = QqPolicy + 直连 ConversationService），保证既有测试不破坏；
- **CLI 策略**：进程内当前会话 + 指令；无 6h 轮换；
- **Agent 不感知渠道**：`ConversationService.handle_message(key, text, meta)` 是唯一入口。

---

## 7. 调度与执行

### 7.1 Dispatcher

```
while running:
    env = await bus.get(INBOUND)
    if dedup.seen(env): task_done; continue
    decision = await policy_for(env.channel).resolve(env)
    publish pre_replies → outbound:<channel>
    if decision.action == "reply": publish replies; task_done
    elif decision.action == "agent":
        await semaphore.acquire()
        task = create_task(worker.handle(env, decision))
        inflight.add(task)
    task_done
```

### 7.2 并发与顺序（G5）

- 单 dispatcher 保证**到达顺序**；每条 agent 请求一个 task，跨会话并发；
- 同会话（同 `SessionKey.storage_id`）由现有 `ConversationService` 的 `asyncio.Lock` 串行；
- `asyncio.Lock` 等待队列 FIFO，因此同会话顺序 = dispatcher 派发顺序（best-effort FIFO，单进程内可测）；
- `Semaphore(64)`（模块常量 `GATEWAY_MAX_INFLIGHT`）限制 in-flight，防止一次性打爆 Provider；
- Worker 完成后释放信号量；task 异常只记日志，不影响其他会话。

> 会话运行时（锁 + AgentLoop 实例缓存 + 每轮装载）当前归 `ConversationService` 独占，Gateway 只做调度；
> 备选方案 B 与 A 的量化对比、升级阈值见 §15 ADR-1。

### 7.3 AgentWorker

```
result = await conversation.handle_message(decision.session_key, decision.text, meta=decision.meta)
reply = Envelope(kind="reply", target_channel=env.channel, conversation_id=env.conversation_id,
                 session_id=decision.session_key.session_id, reply_to=env.message_id,
                 correlation_id=env.message_id, text=result.answer)
await bus.publish(outbound_queue(env.channel), reply)
```

- `completed=False`（错误/熔断/`context_overflow`）同样把 `answer` 发给用户——与现状一致；
- Worker 不关心持久化（`ConversationService` 内部完成），不重复实现 fail-soft；
- 记忆写失败/压缩降级日志照旧，不额外打扰用户。

### 7.4 去重

- `DedupCache(maxsize=4096)`：`OrderedDict`；`seen(key) -> bool`（命中返回 True 并移到末尾）；
- key = `f"{channel}:{message_id}"`，`message_id` 为空则不去重（内部 UUID 只用于 correlation）；
- 进程重启后去重丢失：接受（QQ 断线重连重推的持久化去重留待后续，若需要可落 JSONL/SQLite）。

---

## 8. 生命周期

**启动**：`Gateway.start()` → 打开 bus → 逐个 `adapter.start(bus)`（先起出站消费）→ 启动 dispatcher；
顺序保证"适配器开始收事件前，消费侧已就绪"。

**运行**：dispatcher 单任务 + 出站消费任务 + 适配器自身任务；全部登记在 `Gateway._tasks`。

**优雅关闭** `Gateway.stop()`：

1. 停止适配器入站（不再 publish 新请求）；
2. 等 `inbound` 排空 + in-flight task 完成（`gateway_shutdown_timeout=10s`）；
3. 超时则 cancel 剩余 task 并 warning；
4. 清空出站队列（可尝试最后一次 flush，超时丢弃）；
5. `adapter.stop()` → `bus.close()`；
6. 幂等：重复调用 `stop()` 安全。

`Application.close()` 若 `gateway is not None`，先 `await gateway.stop()` 再关闭 conversation/store。

---

## 9. 与现有代码的迁移映射

| 现有 | 目标 | 备注 |
|---|---|---|
| `channels/base.py::IncomingMessage/OutgoingMessage` | **保留** | 适配器边界 DTO；与 Envelope 互转 |
| `channels/qq_private.py::QqPrivateService` | `QqPolicy` + `QqAdapter`（后续）+ 兼容 facade | 现有 15 个 QQ 测试保持全绿；facade 走老直连路径 |
| `channels/qq_private.py::QqPrivateActiveStore` | `QqPolicy` 内部继续使用 | v1 不抽象通用 ChannelStateStore |
| `cli.py` REPL / `_handle_command` | `CliAdapter`（输入输出）+ `CliPolicy`（会话与指令） | `/exit /quit` 留在适配器；banner 留在 `cli.py` |
| `cli.py::new_cli_session()` | `CliPolicy` 初始会话 | 进程启动新会话语义不变 |
| `bootstrap.Application` | 增加 `gateway: Optional[Gateway]`；`build_gateway(app)` | `gateway_enabled=false` 时不构造，行为零变化 |
| `ConversationService` | **不动** | AgentWorker 的唯一被调方 |
| JSONL / 压缩 / 审计 | **不动** | `meta` 继续带 `channel/scope/sender_id` |

**双轨过渡**：`gateway_enabled=false` 时 QQ 走现有 `QqPrivateService` 直连、CLI 走现有 REPL；
开关打开后两者切到网关路径。等 QQ/CLI 网关路径通过验收（含对等测试），再讨论把默认值翻为 `true`
并在后续版本删除旧直连分支（G12，v1 不改默认）。

---

## 10. 配置项

| 键 | 默认值 | 说明 |
|---|---|---|
| `gateway_enabled` | `false` | 总开关；false 时完全不构造 Gateway（现状行为） |
| `gateway_bus_maxsize` | `1000` | 每个队列（inbound / 每渠道 outbound / deadletter）容量 |
| `gateway_publish_timeout` | `5` | publish 等待秒数；超时进死信并 warning |
| `gateway_shutdown_timeout` | `10` | 优雅关闭等待 in-flight 的秒数 |

模块常量（暂不配置）：`GATEWAY_MAX_INFLIGHT=64`、`DEDUP_MAXSIZE=4096`、`DEADLETTER_MAX_KEEP=128`。
解析与校验沿用 `config.py` 的"系统环境变量 > .env > 默认值 + 非法回退 + warning"模式；
`.env.example` / README 配置表在实现阶段同步。

---

## 11. 不变量与风险

**必须保持的不变量**

1. `ConversationService` / `AgentLoop` / `ContextBuilder` / JSONL schema 零改动；
2. 同一会话严格串行、跨会话并发；回复必须回到原渠道 + 原会话；
3. 请求视图隔离与压缩审计语义不变（网关不碰 messages）；
4. `gateway_enabled=false` 时现有 CLI / QQ 行为与测试逐字节不变；
5. 适配器/策略不得直接互相调用（只经 bus）。

**风险与对策**

| 风险 | 对策 |
|---|---|
| 同会话顺序在多个 task 下"看起来"乱序 | 依赖 `ConversationService` 锁 + dispatcher 派发顺序；增加同会话顺序集成测试 |
| 平台重推导致重复 turn | dedup LRU；重启丢失已知并接受；后续可持久化 |
| 慢适配器拖垮出站队列 | 每渠道独立队列 + 有界 + 超时进死信 |
| CLI 阻塞式 input 卡事件循环 | `asyncio.to_thread(input)`，Ctrl+C 走 `gateway.stop()` |
| 主动推送绕过策略/权限 | v1 不实现 push 生产方；实现时同样经 bus 并走 Policy 校验 |
| 网关异常导致消息静默丢失 | 死信 + warning；`stop()` 排空；错误回复 fail-soft |
| 开关双轨造成行为不一致 | 网关路径必须通过对等测试后才允许默认翻转 |

---

## 12. 测试策略

| 层 | 用例 |
|---|---|
| `envelope` | 必填字段校验、`kind` 语义、correlation 生成、不可变性 |
| `bus` | FIFO、队列隔离、maxsize/背压超时→死信、close 后 publish/get 行为、task_done/join |
| `dedup` | 命中/未命中、LRU 淘汰、空 message_id 不去重 |
| `policy` | QQ 身份过滤/轮换/指令；CLI 会话与 `/new /clear`；公共命令 helper；跨渠道删除 |
| `dispatcher` | 去重短路、策略 ignore/reply/agent 三路、pre_replies 顺序、异常不打断 |
| `worker` | 回复路由到 `outbound:<channel>`、`completed=False` 仍回复、记忆异常 fail-soft |
| 并发/顺序 | 同会话严格串行（时间线断言）、跨会话并发（重叠窗口断言）、in-flight 上限 |
| `adapter` 契约 | FakeAdapter 双端收发；出站单条异常不杀循环；start/stop 幂等 |
| CLI 集成 | 输入→总线→Agent→打印；`/exit` 优雅关闭；pending future 取消 |
| 回归 | `gateway_enabled=false` 时现有 913 用例全绿；开启后 CLI/QQ 对等测试通过 |
| 生命周期 | 关闭时排空、超时 cancel、无死锁；重复 stop 安全 |

---

## 13. 实施阶段

| 阶段 | 内容 | 完成标准 |
|---|---|---|
| W1 | `gateway/` 骨架：envelope/bus/dedup/adapter 契约 + FakeAdapter + dispatcher/worker + 配置开关 | bus/契约/路由单测通过；`gateway_enabled=false` 全量回归不动 |
| W2 | QQ 策略抽取（`QqPolicy` + facade）+ `QqAdapter`(Fake 传输) + 公共命令 helper | QQ 既有 15 用例全绿；网关路径对等测试通过 |
| W3 | `CliAdapter` + `CliPolicy` + `cli.py` 接入（开关控制） | CLI 交互/指令/退出对等；Ctrl+C 优雅关闭 |
| W4 | README/.env.example/架构文档同步；死信与观测日志；评估是否翻默认开关 | 全量回归 + 手工冒烟（真实 CLI/QQ 路径） |

---

## 14. 待实现时的开放项（不阻塞设计）

1. `gateway_enabled` 默认翻转为 `true` 的时机：等 W3 完成并经过一轮真实使用后再决定；
2. 去重持久化：若 QQ 平台重推成为实际问题，再加 JSONL/SQLite 去重表；
3. `deadletter` 是否需要落盘/重放：v1 仅内存排障；
4. 真实 OneBot/NapCat/飞书适配器：依赖对应平台 SDK，独立于本网关核心；
5. 主动推送（`kind=push`）的权限与限流：实现时补 Policy 校验与配额。


---

## 15. ADR-1：会话运行时归属（方案 A vs 方案 B）

> 状态：**决策已记录，方案 B 当前不采用，待指标触发后再评估**。

### 15.1 背景与两种方案的精确定义

**方案 A（当前采用）**：会话运行时归 `ConversationService` 独占。

- `_locks[storage_id]` + `_agents[storage_id]`（`conversation.py:69-93`）；
- 每轮 `handle_message` 在锁内完成"`load_recent` 装载 JSONL → `AgentLoop.run_turn(history=...)` → 仅完整轮 `append_turn`"；
- AgentLoop 在生产路径**不持有对话上下文**（`_session_history` 仅旧 `run()` 用），只带工具防爆滑窗与压缩器摘要缓存；
- Gateway/Policy 只做路由与调度，不持有会话实例。

**方案 B（备选）**：`agent_factory(session_key) -> AgentLoop` 返回全新实例，Gateway 用
`_agents: dict[str, AgentLoop]` 按 `session_key` 缓存，并负责实例生命周期（创建/缓存/销毁）。
它有两种强度：

- **B1（轻量）**：只把实例缓存搬到 Gateway，仍每轮从 JSONL 装载历史；
- **B2（常驻）**：实例持有对话上下文（`_session_history`/内存 checkpointer），JSONL 退化为持久化与审计。

### 15.2 决策

当前采用 **A**；B 的合理诉求（显式缓存/淘汰/容量治理）通过 **A+ 生命周期 API**（§15.8）吸收；
只有当 §15.6 的可观测阈值被触发时，才评估迁移到 B（B2），且必须满足 §15.9 的硬约束。

### 15.3 量化对比（项目真实实现微基准）

基准环境：CPython 3.11（项目 conda 环境）、`JsonlSessionStore`、窗口 `max_turns=50 / max_chars=120000`、
每轮 user+assistant 各约 1200 字符、`/tmp` 存储（可能为 tmpfs，真实磁盘会更慢）；只统计装载与消息投影，
不含 LLM 网络。窗口最终返回 90 条消息。

| 会话总会话数 | JSONL 文件 | `load_recent`+投影 avg | p95 |
|---:|---:|---:|---:|
| 50 | 0.39 MB | 1.32 ms | 1.64 ms |
| 200 | 1.55 MB | 3.99 ms | 4.40 ms |
| 500 | 3.87 MB | 9.39 ms | 10.53 ms |
| 1000 | 7.73 MB | 18.12 ms | 20.70 ms |
| 2000 | 15.47 MB | 39.83 ms | 44.64 ms |

- `ContextBuilder.build_messages` 另约 **0.06 ms**，故 A 的每轮固定开销 ≈ `load_recent`；
- 根因：`load_recent` 先 `_read_turns` **解析整个 JSONL 文件**，再做窗口裁剪，成本是 **O(总会话长度)** 而非 O(窗口)；
- 真实 LLM 参考：`deepseek-flash` 单次 1~5 s。50 轮时开销 <0.3%，1000 轮约 2%，2000 轮约 4%；
  换本地快模型（50~200 ms/轮）或高频短消息时占比显著上升。

### 15.4 性能维度对比

| 维度 | A | B2（常驻上下文） |
|---|---|---|
| 每轮装载 | O(总会话长度)：读盘 + JSON 解析 + 裁剪 | O(1)（热会话）；append 落盘不变 |
| Prompt 组装 | ~0.06 ms，identity/MEMORY 每轮新鲜读取 | 同量级；缓存 system 部分可略省，但需处理 MEMORY/时间变化 |
| 内存 | 实例仅带滑窗/摘要缓存；消息瞬时分配 | 每活跃会话常驻完整窗口（120k 字符 ≈ 0.3~1 MB+），随活跃会话数线性增长 |
| 冷启动/重启 | 无额外成本（本就每轮读） | 重启后首轮须从 store 重新水化；崩溃丢内存上下文 |
| 同会话并发 | 锁跨 LLM await，严格串行 | 一致；淘汰策略不当会打断 in-flight |
| Token 成本 | 基线 | 相同（Prompt 内容不变） |
| 长会话/高频 | 越老越慢（文件越大越吃亏） | 与窗口相关、与总长度无关，明显占优 |
| 一致性 | JSONL 唯一事实源，天然与跨渠道/重启一致 | 需失效协议（`/clear`、purge、跨渠道删除、轮换） |

### 15.5 各自优势

**A**：事实源唯一、重启/跨渠道/跨进程一致；锁与装载同源，不会"复活已删会话"；内存小、生命周期简单；
fail-soft/窗口/持久化逻辑成熟有测试；适合聊天式、低频、长尾会话、多进程共享 store。
**B（B2）**：热会话每轮 O(1)，高频短消息/本地快模型下延迟更低；可显式管理容量/TTL/淘汰并观测活跃会话；
可持有会话级昂贵资源（子进程、浏览器/沙箱、长连接、checkpointer），支持流式与 mid-turn 恢复。
**B1**：无性能收益，只买到生命周期治理——该诉求用 A+ 即可满足。

### 15.6 升级至 B 的触发阈值（建议先埋指标）

满足任意一条即可进入评估，不要求全部满足：

1. `load_recent` **p95 > 20 ms**，或占单轮端到端延迟 **>5~10%**；
2. 单会话文件持续增长到 **数 MB / 数千轮**，而窗口仍是 50 轮级别；
3. 单会话 **>1 msg/s** 或全局 **>20~50 msg/s**，且模型延迟较低（本地/缓存命中）；
4. 需要会话级昂贵资源、流式输出、mid-turn 恢复/中断；
5. 存储换成远程 DB/慢盘，每轮装载成为瓶颈。

### 15.7 不构成升级理由的情形

- 省 token/LLM 成本（B 不减少 Prompt token）；
- 会话隔离正确性（A 已按 `storage_id` 隔离）；
- 只想要缓存淘汰/容量治理（用 A+ 的 `close_session/evict_idle`）；
- 多进程/多机扩展（B 的单机内存缓存跨进程无效，仍需 sticky routing + 共享状态）。

### 15.8 A+ 中间路线（现在可做，低成本）

1. **把装载从 O(总会话长度) 降到 O(窗口)**：JSONL 尾部读取或按 turn 维护 offset 索引，避免每次全文件解析
   ——这是 A 当前最大性能短板，修好后多数场景无需 B；
2. **生命周期 API**：`ConversationService.close_session(key)` / `evict_idle(ttl)` / `max_cached_sessions`，
   由 Gateway 在 `/clear`、purge、QQ 轮换、关停时调用（吸收 B1 优点）；
3. **埋点**：`load_recent` p50/p95、会话文件大小、活跃 Agent 数、进程常驻内存。

### 15.9 若未来采用 B 的硬约束

1. **键必须是 `storage_id`**（`SessionKey.user_id` 不参与 canonical/storage_id，用对象做键会产生双实例/双锁）；
2. **JSONL 仍是唯一事实源**：内存上下文为 write-through 缓存，miss/重启时从 store 水化，不允许内存成为唯一真相；
3. **失效协议**：`/clear`、purge、归档、QQ 轮换、跨渠道删除必须使对应实例失效，避免复活已删会话；
4. **淘汰安全**：仅在会话 idle（无 in-flight turn）时淘汰；实例与锁同生命周期；
5. **回归门槛**：通过现有 `ConversationService` 契约测试（锁/窗口/fail-soft/跨渠道删除）与网关对等测试。

### 15.10 状态

- 决策：**采用 A + A+，B 暂不采用**；
- 触发评估：以 §15.6 指标为准；
- 本 ADR 只定义方向，不改动现有代码；A+ 的第 1/2 项可在网关实现稳定后作为独立优化项排期。
