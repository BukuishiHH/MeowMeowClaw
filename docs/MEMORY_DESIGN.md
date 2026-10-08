# MeowMeowClaw 记忆系统设计（v1：单用户 / CLI + QQ 私聊）

> 状态：**v1 已实现（M1-M7）**；里程碑到代码/测试的映射见 §14
> v1 包含：短期记忆（按渠道隔离）+ `MEMORY.md` 最简长期记忆（跨渠道共享、Agent 可写）
> 群聊、结构化长期记忆、多用户、多后端、加密均为"预留扩展"，见 §11
> 关联：`docs/ARCHITECTURE.md`

---

## 0. 本次范围收窄对比

| 维度 | 前一版通用设计 | v1 确认范围 |
|---|---|---|
| 用户 | 预留多用户/多租户 | **仅单用户**（接口仍保留 namespace） |
| 渠道 | CLI / 飞书 / QQ / 群聊 | **CLI + QQ 私聊**（不含飞书、不含群聊） |
| 短期记忆 | 完整设计 | **本次实现**，各渠道严格隔离 |
| 长期记忆 | 结构化 LongTermStore | **仅 `MEMORY.md` 文件级最简机制**（Agent 写、Prompt 读、跨渠道共享）；LongTermStore 只预留接口 |
| 会话策略 | 未定 | CLI 进程生命周期；QQ 闲置 6 小时轮换 |
| 会话清理 | 未定 | **不自动清理**；`/clear [会话标识] [--purge]` 手动指定 |
| 存储 | JSONL + MySQL/Redis 设计 | **仅 JSONL**，抽象接口保证可换 |
| 加密 | 待定 | **不加密**，仅文件权限 0700/0600 |
| 群聊 | 支持设计 | **暂不支持**，字段与接口预留 |

---

## 1. 范围

### 1.1 v1 做什么

- 单用户，两种渠道：**CLI**（进程生命周期会话）与 **QQ 私聊**（闲置 6 小时轮换）；
- **短期记忆**：完整 turn 的持久化、按会话装载、窗口裁剪、清空/新建/列出/删除；
- **最简长期记忆**：`MEMORY.md` 文件由 Agent 通过 `write_file` 维护，`ContextBuilder` 每次拼进 System Prompt；同一用户的各渠道共享该文件；
- 存储抽象接口 `SessionStore` + 唯一 JSONL 实现；
- 预留 `LongTermStore` 抽象 + `NoopLongTermStore` + `ContextBuilder` 注入点（将来结构化长期记忆用）；
- `/sessions`、短 ID、`/clear`、`/clear <id>`、`/clear <id> --purge`、`/new`；
- 为群聊预留 `scope/group`、`sender_id` 等字段与命名空间设计；
- 明确并发、存储位置、工具访问边界、保留策略与测试策略。

### 1.2 v1 不做什么

- 不做结构化长期记忆（不实现 LongTermStore 的存储/召回/提炼/遗忘）；
- 不做群聊、飞书或其他渠道；
- 不做多用户/多租户；
- 不做 MySQL/Redis/SQLite 后端（只保证接口可换）；
- 不做加密、不做向量检索、不做摘要压缩（预留接口）；
- 不做附件/图片/语音的存储（仅预留字段）；
- **不做自动清理/自动过期**（所有会话无限期保留，删除仅手动）。

---

## 2. 已确认决策记录

| # | 决策 | 确认结果 |
|---|---|---|
| D1 | 抽象接口与实现 | 定义 `SessionStore` 抽象接口 + **唯一 JSONL 实现**，后续可换后端 |
| D2 | 渠道共享边界 | **短期记忆各渠道严格隔离；长期记忆（`MEMORY.md`）有意跨渠道共享** |
| D3 | `MEMORY.md` 定位 | **v1 最简长期记忆机制**：Agent 用 `write_file` 写、`ContextBuilder` 读入 Prompt；不经过 LongTermStore |
| D4 | 加密 | **不加密**；目录 0700、文件 0600、不入 git、备份提示 |
| D5 | CLI 会话 | 每次启动 = **全新会话**，进程退出即结束，绝不自动恢复 |
| D6 | 会话文件保留 | **全面落盘、无限期保留、不自动清理** |
| D7 | `/clear` | **无参 = 归档当前会话**；`/clear <id>` = 归档指定会话；**`--purge` = 永久删除** |
| D8 | `/new` | **创建新会话**（新 session_id）；旧会话保持非归档状态、不受影响 |
| D9 | CLI 多进程 | 每个进程独立 session_id，互不干扰；启动日志打印 session_id |
| D10 | QQ 计时锚点 | **用户最后一条消息到达时间**；机器人回复不刷新 |
| D11 | QQ 超时判断 | **惰性判断**（收到新消息时比较当前时间与最后活动时间） |
| D12 | QQ active 指针 | **持久化**（联系人 → 当前 session_id + 最后活动时间），重启可恢复 |
| D13 | QQ 超时旧会话 | 轮换后**归档保留，不自动删除** |
| D14 | QQ 超时提示 | 固定文案：**"已开始新对话"** |
| D15 | QQ 上下文恢复 | 6 小时内恢复最近 **20 轮 / 50000 字符**（可配置） |
| D16 | QQ 指令集 | **`/help`、`/new`、`/clear`、`/sessions`**；仅私聊、仅本人可用 |
| D17 | QQ 并发 | 同一联系人消息**排队串行**处理 |
| D18 | 进程部署 | QQ 机器人服务与 CLI **可能同时运行在同一台机器**，文件所有权互不冲突 |
| D19 | 多模态 | 仅存文本；图片/语音/附件仅预留字段，不处理 |
| D20 | 短期存储内容 | 完整 turn（含工具调用与结果）；单条工具结果默认截断 **8000 字符**并标记 |
| D21 | 时间戳 | **UTC epoch millis + ISO8601** 两种都存 |
| D22 | 写入失败 | **fail-soft**：告警并继续回复用户，不中断对话 |
| D23 | 群聊预留 | 现在加 `scope=group`、`sender_id`、群 ID 字段；触发规则/权限未来在渠道层实现 |
| D24 | 长期接口预留 | `LongTermStore` Protocol + `NoopLongTermStore` + `ContextBuilder` 注入点；v1 不实现其存储 |
| D25 | 编排 | `ConversationService`：解析会话 → 装载 → 调 `AgentLoop` → 回写 turn → 处理指令；AgentLoop 接收历史快照 |
| D26 | 存储位置 | 默认 `<workspace>/memory/`，可通过 `memory_dir` 配置覆盖 |
| D27 | 工具访问边界 | `sessions/`、`active/`、`archive/` 对文件工具**全禁**；`MEMORY.md` **可读可写**（写入前自动备份） |
| D28 | 后端升级时机 | 多用户/多渠道服务化/群聊/需要查询事务时：优先 SQLite，其次 MySQL；Redis 仅做缓存/锁/去重/队列 |
| D29 | 手动清理 | 所有会话不自动清理；通过 `/clear [id] [--purge]` 手动指定删除；**允许跨渠道删除** |
| D30 | 会话标识发现 | 新增 **`/sessions`** 指令 + **短 ID**（storage_id 起 8 位的最短唯一前缀）；命令按前缀解析 |
| D31 | 清空/轮换后的身份 | 归档或轮换后**生成新的 session 实例 id**，避免归档文件与活跃文件重名/短 ID 冲突 |
| D32 | `MEMORY.md` 写入保护 | 每次 Agent 写入前**自动备份 `MEMORY.md.bak`**；System Prompt 明确"先读后写、保留旧内容"；**不做大小/频率限制** |
| D33 | 跨渠道删除 | CLI 与 QQ 均可删除对方渠道的会话；QQ 指令仍限本人私聊 |
| D34 | `MEMORY.md.bak` 备份份数 | **单份滚动备份**（每次写入覆盖上一份） |
| D35 | `/sessions` 展示 | 默认列出**活跃 + 归档**；展示短 ID、状态、渠道、时间、轮数，并支持复制完整键 |
| D36 | 短 ID 规则 | 最短唯一前缀，**最少 8 位**；冲突自动加长 |
| D37 | "已开始新对话"提示范围 | 超时轮换、`/new`、跨渠道删除导致重建，三场景统一使用 |
| D38 | `--purge` 确认 | 直接执行；输入短 ID 视为确认，不做二次确认 |

---

## 3. 概念模型

```
                    ┌──────────────────────────────────────────┐
                    │            ConversationService           │
                    │  会话解析 / 超时轮换 / 装载 / 回写 / 指令  │
                    └───────┬──────────────────────────┬───────┘
                            │                          │
              ┌─────────────┴─────────────┐            │ 读/写
              ▼                           ▼            ▼
   ┌────────────────────────┐   ┌────────────────────────┐
   │      SessionStore      │   │  MEMORY.md（长期，跨渠道）│
   │  短期：每会话 turn 流水  │   │  Agent write_file 维护   │
   │  v1: JsonlSessionStore │   │  ContextBuilder 读入 Prompt│
   └────────────────────────┘   └────────────────────────┘
                            ┌────────────────────────┐
                            │ LongTermStore（预留）    │
                            │ v1: Noop（不参与）       │
                            └────────────────────────┘
```

| 概念 | 说明 |
|---|---|
| Session（会话） | 一段连续对话的容器；短期记忆的隔离与并发单位 |
| SessionKey | `channel + scope + conversation_id (+user_id)`；会话实例 id 不可复用 |
| Turn | 一轮"用户输入 → 模型最终回答"（含工具调用/结果）；**原子写入单位** |
| SessionStore | 短期记忆仓储抽象；v1 唯一实现 JSONL |
| `MEMORY.md` | v1 的长期记忆文件；Agent 维护、Prompt 注入、跨渠道共享 |
| LongTermStore | 结构化长期记忆预留接口；v1 = Noop |
| ConversationService | 编排层：会话解析、轮换、装载、回写、指令处理 |
| 短 ID | storage_id 的最短唯一前缀，供 `/sessions` 与 `/clear <id>` 使用 |

---

## 4. 会话生命周期

### 4.1 CLI：进程生命周期绑定

```
启动 CLI
  → 生成 session_id (UUID)
  → SessionKey = "v1:cli:session:<uuid>"
  → 空历史启动（不恢复旧会话）
  → 每轮：load_recent → AgentLoop.run → append_turn
  → /new：生成新 session_id，旧会话留在 sessions/
  → /clear：归档当前会话 + 生成新 session_id
  → /clear <id> [--purge]：归档/永久删除指定会话
  → /sessions：列出会话与短 ID
  → /exit：退出；文件保留
```

规则：

- 每次启动都是新会话，绝不自动恢复；
- `/new`：仅切换新会话，旧会话**不归档**、仍留在 `sessions/`；
- `/clear`：归档当前会话到 `archive/`，并生成新 session_id（D31，避免身份复用）；
- `/clear <id>`：归档指定会话；若目标是当前会话，同样生成新 session_id；
- `/clear <id> --purge`：永久删除指定会话（含归档副本）；
- 多进程互不干扰；启动日志打印 session_id 与短 ID。

### 4.2 QQ 私聊：闲置 6 小时轮换

```
收到 QQ 私聊消息
  → contact = "v1:qq:private:<uin>"
  → 读 active 指针(contact → session_id, last_activity_ms)
  → 若指针不存在 / 对应会话文件缺失 / now-last_activity > 6h:
        · 旧会话归档
        · 生成新 session_id，更新 active 指针
        · 回复"已开始新对话"
  → 否则沿用当前会话
  → 装载最近窗口(20 轮 / 50000 字符)
  → AgentLoop.run
  → append_turn
  → 更新 active.last_activity_ms = 本次用户消息到达时间
```

规则：

- 计时锚点 = 用户最后一条消息到达时间；机器人回复不刷新；
- 惰性判断；服务重启靠持久化 active 指针恢复；
- 超时、`/new`、`/clear`、外部删除导致会话失效时，统一回复提示语 **"已开始新对话"**；
- 仅私聊，且指令仅本人可用；群聊字段预留不启用。

### 4.3 会话键、短 ID 与存储 ID

| 场景 | 逻辑键（canonical key） | 说明 |
|---|---|---|
| CLI | `v1:cli:session:<uuid>` | 每个进程一个；`/clear`/`/new` 后更换 uuid |
| QQ 联系人（逻辑） | `v1:qq:private:<uin>` | 只用于 active 指针 |
| QQ 会话实例 | `v1:qq:private:<uin>:<uuid>` | 超时/清空/新建后更换 uuid |

- 物理文件名 `storage_id = base32(sha256(canonical_key))[:26]`，规避 `:`、`/`、`..`、Windows 非法字符与超长；
- **短 ID**：对当前列出的会话取 `storage_id` 的**最短唯一前缀（最少 8 位，冲突时自动加长）**；
- 命令解析：`/clear <前缀>`、`/clear <前缀> --purge`；前缀冲突时报候选列表；
- 逻辑键完整保存于文件头与 `/sessions` 输出（可复制）。

### 4.4 指令集

| 指令 | CLI | QQ 私聊 | 语义 |
|---|---|---|---|
| `/exit` | ✅ | — | 退出 CLI |
| `/new` | ✅ | ✅ | 新建会话；旧会话保留在 `sessions/` |
| `/clear` | ✅ | ✅ | 归档当前会话，并新建会话 |
| `/clear <id>` | ✅ | ✅ | 归档指定会话（若为当前会话则同时新建） |
| `/clear <id> --purge` | ✅ | ✅ | 永久删除指定会话（含归档副本） |
| `/sessions` | ✅ | ✅ | 列出会话（短 ID / 状态 / 渠道 / 时间 / 轮数） |
| `/help` | ✅ | ✅ | 指令帮助 |
| `/tools` `/skills` | ✅ | — | CLI 调试指令 |

### 4.5 会话发现与手动清理

- `/sessions` 输出示例：

```
短ID      状态      渠道   创建时间(UTC)         最后活动(UTC)         轮数
a1b2c3d4  active    cli    2026-01-01T09:00:00Z  2026-01-01T09:20:00Z  12
e5f6a7b8  archived  qq     2025-12-31T10:00:00Z  2025-12-31T11:00:00Z  33
```

- 默认列出**活跃 + 归档**，`--active` / `--archived` 可过滤；
- 所有会话**不自动清理**；磁盘占用由用户自行监控；
- 允许跨渠道删除：CLI 可归档/删除 QQ 会话，QQ 也可操作 CLI 会话；QQ 指令仅本人私聊可用；
- 删除指定会话时若该会话正被另一进程使用：归档/删除在会话级文件锁内执行；QQ 服务下次装载发现文件缺失时按新会话处理并提示"已开始新对话"。

---

## 5. 短期记忆数据模型

### 5.1 Turn 记录（JSONL 行，示意）

```json
{
  "type": "turn",
  "schema_version": 1,
  "seq": 12,
  "turn_id": "01J8Z...",
  "ts_ms": 1760000000123,
  "ts_iso": "2026-01-01T00:00:00.123Z",
  "messages": [
    {"role": "user", "content": "帮我看下 README", "ts_ms": 1760000000000},
    {"role": "assistant", "content": null, "tool_calls": [ ... ], "ts_ms": 1760000000050},
    {"role": "tool", "tool_call_id": "call_1", "content": "...(截断标记)", "ts_ms": 1760000000080},
    {"role": "assistant", "content": "看完了...", "ts_ms": 1760000000120}
  ],
  "meta": {
    "channel": "cli",
    "scope": "session",
    "sender_id": null,
    "truncated_tool_results": 1
  }
}
```

会话文件第一行为 `type: "header"`，保存 `storage_id / session_key / channel / scope / conversation_id / user_id? / created_at`；后续行为 turn。

### 5.2 存什么 / 不存什么

| 内容 | 是否落盘 | 说明 |
|---|---|---|
| user / assistant 文本 | ✅ | 完整保存 |
| assistant `tool_calls` | ✅ | 保持 OpenAI 消息格式 |
| tool 结果 | ✅ | 单条默认截断 8000 字符，加截断标记 |
| 时间戳 | ✅ | turn 与消息带 `ts_ms`；turn 额外带 ISO |
| 渠道/发送者 | ✅ | `meta.channel / scope / sender_id`（群聊预留） |
| 图片/语音/附件 | ❌（预留字段） | v1 不存内容 |
| 长期记忆 | ⚠️ | **`MEMORY.md` 承担**（系统外机制，见 §9.4）；LongTermStore 为 Noop |

### 5.3 时间戳

- `ts_ms`：UTC epoch millis，排序与超时判断的权威字段；
- `ts_iso`：UTC ISO8601，仅供人工排查；
- 不依赖本地时区；turn 内顺序以 `seq` 与数组顺序为准。

### 5.4 上下文窗口与截断

| 项 | v1 默认 | 可配置 |
|---|---|---|
| 装载轮数 | 20 轮 | ✅ `max_turns` |
| 装载字符上限 | 50,000 字符 | ✅ `max_chars` |
| 工具结果截断 | 8,000 字符/条 | ✅ `max_tool_result_chars` |
| 超出窗口 | 丢弃最旧的整轮 | — |
| 摘要压缩 | 不做，仅预留接口 | 未来 |

### 5.5 写入失败语义

- **fail-soft**：append 失败只记录 warning/metrics，正常回复用户；
- 由于每轮从 store 装载，append 失败意味着该轮不在下一轮上下文中（已知代价）；
- 绝不因记忆故障阻塞回复；启动时做一次目录可写健康检查并提示。

---

## 6. 抽象接口设计（签名示意，非实现）

### 6.1 SessionStore（短期，v1 唯一实现 = JSONL）

```
async append_turn(key, messages, *, turn_id=None, meta=None) -> SessionMeta
async load_recent(key, *, max_turns=None, max_chars=None) -> list[SessionMessage]
async get_meta(key) -> SessionMeta | None
async list_sessions(*, include_archived=True) -> list[SessionSummary]
async archive(key) -> None          # /clear：移动到 archive/
async purge(key) -> None            # /clear --purge：永久删除
async close() -> None
```

- `append_turn`：一轮原子写入（内部加锁）；
- `load_recent`：从最近往前取整轮，坏行跳过并告警；
- `list_sessions`：扫描活跃/归档文件头 + 末行，返回短 ID、状态、时间、轮数；
- `archive`：`sessions/<id>.jsonl → archive/<id>.jsonl`；
- `purge`：删除活跃与归档中的对应文件。

### 6.2 LongTermStore（预留，v1 = Noop）

```
async recall(namespace, *, query=None, limit=20) -> list[MemoryRecord]
async remember(namespace, record) -> MemoryRecord
async forget(namespace, record_id) -> None
async list_namespaces() -> list[str]
async close() -> None
```

- v1 用 `NoopLongTermStore`：`recall` 返回空，其余方法无操作；
- namespace：现在 `user:default`；未来 `user:<id>`、`group:<id>`；
- 结构化长期记忆落地后，`MEMORY.md` 可作为迁移来源，见 §11.2。

### 6.3 ConversationService（编排层）

1. **会话解析**：CLI 进程 session_id；QQ 读 active 指针 + 6 小时惰性轮换；
2. **装载**：`load_recent(key, 20 轮 / 50k 字符)`；
3. **执行**：历史快照交给 `AgentLoop.run(user_message, history=...)`；
4. **回写**：`append_turn`；更新 QQ active 指针；
5. **指令**：`/sessions`、`/new`、`/clear [id] [--purge]`、`/help`；
6. **并发**：同一会话串行；不同会话并行；跨渠道删除走会话级锁。

### 6.4 与 AgentLoop 的边界

- `AgentLoop` 不再负责持久化：`run()` 接收历史快照，本轮临时 state 留在单次调用内；
- 短期上下文由 ConversationService 装载/回写；
- 好处：纯逻辑、易测、可并发；代价：需调整 AgentLoop 签名与测试（实现阶段处理）。

### 6.5 异步与错误

- 接口全部 `async`；JSONL 用短 IO，将来 MySQL/Redis 直接适配；
- 统一 `MemoryStoreError`；
- 读失败返回空历史 + warning；写失败 fail-soft；
- 启动时检查目录可写与 `schema_version` 兼容。

---

## 7. JSONL 后端设计

### 7.1 目录布局

```
<workspace>/memory/
├── sessions/
│   ├── <storage_id>.jsonl
│   └── <storage_id>.jsonl.lock
├── archive/
│   └── <storage_id>.jsonl
├── active/
│   └── qq_private.jsonl        # QQ: uin → 当前 session_id + last_activity_ms
└── MEMORY.md                   # v1 长期记忆（Agent 维护，跨渠道）
    └── MEMORY.md.bak           # 写入前自动备份（滚动一份）
```

- `memory_dir` 默认 `<workspace>/memory`，可配置；
- 目录 0700、文件 0600；明文存储（D4）。

### 7.2 记录格式

- 文件第一行：`{"type":"header","schema_version":1,"storage_id":...,"session_key":...,...}`；
- 其后每行一个 turn（§5.1）；
- 追加写：`json.dumps(..., ensure_ascii=False, separators=(",", ":")) + "\n"`；
- 字段只增不删；读取忽略未知字段。

### 7.3 写入、读取与恢复

- 同一会话写入前取进程内 `asyncio.Lock`；跨进程操作（含跨渠道删除）加会话级文件锁；
- 一轮一次 `write()`；可选每轮 `flush/fsync`；
- 读取跳过损坏/半行并告警；
- `archive`/`purge` 用 `tmp + os.replace` 或 `os.replace` 保证原子；
- **不做自动清理/压缩/过期**；磁盘监控由用户负责。

### 7.4 `/sessions` 的数据来源

- 扫描 `sessions/*.jsonl` 与 `archive/*.jsonl`：
  - 读第一行 header → 逻辑键、渠道、创建时间；
  - 读最后一行 turn → 最后活动、轮数（v1 单用户规模可接受；若变慢再加 sidecar meta）；
  - 计算短 ID（最短唯一前缀，至少 8 位）；
- 输出排序：最后活动时间倒序。

### 7.5 QQ active 指针

- 文件：`active/qq_private.jsonl`，追加事件：
  - `{"type":"activate","uin":...,"session_id":...,"at_ms":...}`
  - `{"type":"activity","uin":...,"session_id":...,"at_ms":...}`
  - `{"type":"clear","uin":...,"at_ms":...}`
- 按 `uin` 取最新事件；写者只有 QQ 服务（单实例；可加 `.lock` 防误启多实例）；
- 若 active 指向的会话文件缺失（被 CLI 删除/归档），QQ 服务按新会话处理并提示"已开始新对话"。

---

## 8. 并发设计

### 8.1 场景与策略

| 场景 | 风险 | v1 策略 |
|---|---|---|
| CLI 与 QQ 服务同时运行 | 共享 `memory/` | 文件所有权分离；无共享索引；跨渠道删除加会话级文件锁 |
| 同一 CLI 进程 | 单线程交互 | 天然串行 |
| 同一 QQ 联系人连发消息 | 上下文交错 | 每联系人队列/锁，整轮串行 |
| QQ 服务重启 | active 丢失 | active 指针持久化，重启恢复 |
| 跨渠道删除活跃会话 | 写入复活/指针失效 | 会话文件锁 + QQ 装载时校验文件存在性 |
| `/clear`/`/new` 与写入并发 | 数据复活/丢失 | 同一会话锁内执行，归档后生成新 session id |
| 记忆写入失败 | 上下文缺失 | fail-soft + 告警 |

### 8.2 锁的分层

- **进程内**：`SessionKey → asyncio.Lock`（弱引用/LRU），同会话串行；
- **跨进程**：会话级文件锁（`<storage_id>.jsonl.lock`），用于"两个进程可能动同一会话"的兜底（v1 主要为跨渠道删除）；
- **QQ active 文件**：单实例写者 + 启动 `.lock` 防误启多实例；
- 不使用全局锁。

### 8.3 幂等与崩溃恢复

- QQ 渠道层按 `(channel, event_id)` 去重，防止平台重推造成重复 turn/回复；
- `turn_id` 用于写入去重（实现阶段定粒度）；
- 崩溃最多丢最后一轮；已落盘 turn 完整；
- 归档/删除使用原子替换，崩溃不会留下半改文件。

---

## 9. 存储位置、工具边界与安全

### 9.1 位置

- 默认 `<workspace>/memory/`，`memory_dir` 可配置；
- 不放项目根；随 workspace 一起备份；不入 git。

### 9.2 工具访问策略（D27）

| 路径 | read_file | write_file | list_dir |
|---|---|---|---|
| `memory/sessions/**` | ❌ | ❌ | ❌ |
| `memory/active/**` | ❌ | ❌ | ❌ |
| `memory/archive/**` | ❌ | ❌ | ❌ |
| `memory/MEMORY.md` | ✅ | ✅（写入前自动备份） | ✅ |
| 其他工作区 | ✅ | ✅ | ✅ |

- 实现层在 `resolve_in_workspace` 之上加 ToolPolicy 例外：仅放行 `memory/MEMORY.md`；
- `write_file` 命中 `memory/MEMORY.md` 时：先把现有内容复制到 `memory/MEMORY.md.bak`，再写入新内容；
- 目的：既允许 Agent 维护长期记忆，又尽量防止一次覆盖清空。

### 9.3 无加密的已知风险（已接受）

- JSONL 与 `MEMORY.md` 均为明文，包含完整对话与笔记；
- 依赖 0700/0600 权限、单用户环境、机器账号安全；
- 备份/同步会复制明文；未来需要时再引入加密（§11.5）。

### 9.4 `MEMORY.md`：v1 最简长期记忆

- **定位**：短期对话之外、跨渠道共享的长期笔记；由 Agent 自己维护，不经过 LongTermStore；
- **读取**：`ContextBuilder` 每次构建 System Prompt 时读取并拼接（保留现有行为）；
- **写入**：Agent 通过 `write_file("memory/MEMORY.md", ...)` 写入；每次写入前自动备份 `MEMORY.md.bak`；
- **Prompt 约定**（实现时写入 System Prompt）：
  1. 修改长期记忆前必须先 `read_file` 读取当前内容；
  2. 合并/追加后整体写回，不得清空已有笔记；
  3. 只记录稳定、可复用的事实/偏好，不记录临时对话细节；
  4. 不写入密钥、密码等敏感信息；
- **不限制大小/频率**（D32）；风险由备份与 Prompt 约定缓解；
- 由于跨渠道共享，CLI 的笔记 QQ 能看到，反之亦然——这是有意设计。

---

## 10. 测试策略

| 类型 | 覆盖内容 |
|---|---|
| 契约测试 | SessionStore 行为规格（JSONL 实现通过；未来后端复用） |
| CLI 会话 | 每次启动新会话；`/clear` 归档+新建；`/new` 切换；`/sessions` 列出；多进程隔离 |
| QQ 会话 | 6 小时前沿用/之后轮换；active 指针持久化；重启恢复；提示语；`/new`/`/clear` |
| 会话管理 | 短 ID 解析（唯一/冲突）；`/clear <id>`；`--purge`；归档与永久删除 |
| 跨渠道 | CLI 删除 QQ 会话后 QQ 服务重建会话并提示；QQ 删除 CLI 会话 |
| 窗口与截断 | 20 轮/50k 字符；不切开 turn；8000 字符工具结果截断 |
| 并发 | 同联系人串行；不同会话并行；归档/删除与写入互斥 |
| 崩溃恢复 | 半行 JSON、部分 turn、归档替换中断 |
| 工具边界 | sessions/active/archive 全禁；MEMORY.md 可读写、写前备份 |
| 长期记忆 | MEMORY.md 拼接进 Prompt；备份文件生成；Prompt 约定生效（行为级） |
| 预留接口 | NoopLongTermStore 行为稳定；ContextBuilder 注入点不影响现状 |

---

## 11. 未来扩展（v1 不实现）

### 11.1 群聊

- 增加 `scope=group`、群 ID；记录 `sender_id`（字段已预留）；
- 会话 = 群（可选 group+thread）；触发规则（@/前缀/白名单）在渠道层；
- 长期记忆命名空间 `group:<id>` 与 `user:<id>` 并存；
- 工具权限收紧（群聊默认禁 `exec`）；
- 目标零迁移：接口与文件格式不变，只加渠道层与策略。

### 11.2 结构化长期记忆（LongTermStore）

- 实现 JSONL/SQLite/MySQL 后端，命名空间 `user:default → user:<id>`；
- 写入策略：显式"记住/忘记"优先，自动提炼后置且受控；
- `MEMORY.md` 可作为迁移来源（解析为 `MemoryRecord`），也可保留为人工/Agent 共写的补充；
- `ContextBuilder` 注入 `recall()` 结果，与 `MEMORY.md` 并存或替代。

### 11.3 其他模态

- 图片/语音/附件元数据与本地缓存路径；是否接入多模态模型另行评估。

### 11.4 多用户 / 服务化与存储升级（D28）

- 触发条件：多用户、多渠道常驻服务、群聊、需要查询/事务/审计；
- 路径：**JSONL → SQLite → MySQL**；Redis 仅做缓存/锁/去重/队列；
- 接口不变，替换后端；迁移按 `storage_id/session_key` 去重导入；
- 并发从"文件所有权互斥"升级为"DB 事务 + 分布式锁"。

### 11.5 加密

- 在 0700/0600 之外引入信封加密/密钥管理；
- 加密单元（会话文件或整库）、检索与迁移另做设计；
- v1 不做，风险已记录。

---

## 12. 实施里程碑（v1）

| 里程碑 | 内容 | 退出标准 |
|---|---|---|
| M0 | 本文档评审通过 | 决策表确认 |
| M1 | `SessionKey/SessionMessage/SessionMeta/SessionSummary` 类型 + `SessionStore` 接口 + JSONL 实现 | 契约测试通过 |
| M2 | `ConversationService` + AgentLoop 历史快照改造 | 装载/回写/窗口正确 |
| M3 | CLI 接入：进程会话、`/sessions`、`/new`、`/clear [id] [--purge]` | 命令与文件行为测试通过 |
| M4 | QQ 私聊接入：active 指针、6h 惰性轮换、提示语、最小指令集 | 超时/重启/跨渠道删除测试通过 |
| M5 | `MEMORY.md` 长期记忆：写入备份、Prompt 约定、ContextBuilder 拼接 | 备份/合并行为符合约定 |
| M6 | `LongTermStore` Protocol + Noop + 注入点 | 现状 Prompt 行为不变 |
| M7 | 并发/崩溃/工具边界/文档收口 | 全部测试通过、文档同步 |

---

## 13. 执行细节确认（无待决项）

以下细节已按默认值确认，直接作为实现约束：

| 项 | 确认结果 |
|---|---|
| `MEMORY.md.bak` | 单份滚动备份，每次 Agent 写入前覆盖 |
| `/sessions` 输出 | 默认活跃 + 归档；短 ID、状态、渠道、创建/最后活动时间、轮数；完整逻辑键可复制 |
| 短 ID | 最短唯一前缀，最少 8 位，冲突自动加长 |
| "已开始新对话"提示 | 超时轮换、`/new`、跨渠道删除导致重建，三场景统一使用 |
| `--purge` | 直接执行，输入短 ID 即视为确认 |

M1 实施范围：`SessionKey` / `SessionMessage` / `SessionMeta` / `SessionSummary` 类型 + `SessionStore` 抽象接口 + JSONL 实现 + 契约测试。


---

## 14. 实现状态（M1-M7）

| 里程碑 | 状态 | 实现位置 | 测试 |
|---|---|---|---|
| M1 类型 / SessionStore / JSONL | ✅ | `meowmeowclaw/memory/{models,store,jsonl}.py` | `tests/memory/{contract.py,test_jsonl_store.py}` |
| M2 编排 / 历史快照 | ✅ | `meowmeowclaw/conversation.py`、`agent/loop.py::run_turn` | `tests/test_conversation.py`、`tests/agent/test_loop.py` |
| M3 CLI 接入 | ✅ | `config.py`/`paths.py`(memory_*)、`bootstrap.py`、`cli.py` | `tests/test_cli.py`、`tests/test_bootstrap.py`、`tests/test_config.py` |
| M4 QQ 私聊 | ✅ | `meowmeowclaw/channels/{base,qq_private}.py` | `tests/channels/test_qq_private.py` |
| M5 MEMORY.md 备份 / 约定 | ✅ | `tools/filesystem.py`、`agent/context.py` | `tests/tools/test_filesystem.py`、`tests/agent/test_context.py` |
| M6 LongTermStore / Noop / 注入点 | ✅ | `memory/{models,store,noop}.py`、`agent/context.py` | `tests/memory/test_long_term.py`、`tests/agent/test_context.py` |
| M7 并发 / 崩溃 / 工具边界 | ✅ | `memory/filelock.py`、`jsonl.py`(文件锁/补行)、`tools/filesystem.py`(realpath) | `tests/memory/test_filelock.py`、`tests/memory/test_jsonl_store.py`、`tests/tools/test_filesystem.py` |

尚未实现（超出 v1 范围）: 结构化长期记忆的持久化后端；QQ 的 OneBot/NapCat 传输适配器；群聊；记忆加密。
