# MeowMeowClaw 记忆系统设计（v1 设计稿）

> 状态：**设计分析稿，未实施**
> 前提：单用户、多渠道（CLI / 飞书 / QQ 等）；短期用 JSONL 落地，接口预留 MySQL / Redis 等后端
> 关联：`docs/ARCHITECTURE.md`（项目分层与技能设计）

---

## 1. 目标与非目标

### 目标

1. 支持同一用户从多个渠道持续对话，且：
   - **短期对话上下文按会话隔离**（不同渠道/群聊/私聊互不串话）；
   - **长期记忆跨渠道共享**（换渠道仍记得"我是谁、我的偏好、正在做的事"）。
2. 提供稳定的存储接口：v1 用 JSONL 即可跑，未来换 MySQL / Redis / SQLite 时不改调用方。
3. 明确并发模型：同一用户多端并发、同会话连续消息、跨进程写入都不损坏数据。
4. 明确存储位置、权限、安全边界与演进路线。

### 非目标（v1 不做）

- 多用户 / 多租户的产品化（但接口与命名空间必须预留）；
- 向量检索、embedding 语义召回（长期记忆先做结构化存取，检索后置）；
- 记忆的自动"人格演化"与复杂冲突消解；
- 渠道接入本身（见前文渠道分析，不在此文档展开）。

---

## 2. 核心结论（决策记录）

| # | 决策 | 说明 |
|---|---|---|
| D1 | **短期记忆按会话隔离** | 键 = 渠道 + 会话范围 + 会话 ID；群聊以群为会话，私聊以用户为会话 |
| D2 | **长期记忆跨渠道共享** | 命名空间 = `user:<user_id>`；记录保留来源渠道/会话，便于审计与冲突处理 |
| D3 | **短期历史与长期记忆分仓、分层** | 短期 = 原始对话流水（transcript）；长期 = 提炼后的可复用事实，二者不混存 |
| D4 | **接口全异步（async）** | JSONL 后端用 `asyncio.to_thread` 包装同步 IO；为 aiomysql / redis.asyncio 留路 |
| D5 | **门面 + 仓储** | `MemoryService` 门面对外；`SessionStore` / `LongTermStore` 两个仓储各自可替换 |
| D6 | **存储位置：`<workspace>/memory/`** | 默认 workspace 内，`memory_dir` 可配置；不放在项目根，也不与普通文件工具区域混用 |
| D7 | **文件工具禁止访问记忆目录** | 防止 Agent 通过 read/write/list 读取、污染或泄露自己的记忆 |
| D8 | **并发 = 会话内串行 + 命名空间锁 + 单写者优先** | 同一会话同一时刻只跑一轮；长期记忆按 namespace 串行；多进程优先收敛为单写者服务 |
| D9 | **JSONL 一轮一行（turn 原子）** | 记录 `schema_version`、`turn_id`、`seq`、UTC 时间戳；而不是一条消息一行 |
| D10 | **AgentLoop 不直接持有存储** | 由 `ConversationService` 负责装载/回写；AgentLoop 只负责模型↔工具循环 |
| D11 | **JSONL → (SQLite) → MySQL/Redis** | 接口不变；Redis 只做缓存/锁/去重/队列，不作为唯一持久层 |
| D12 | **长期记忆写入受控** | 默认仅显式 `记住…` 或高置信度提炼写入；不自动保存模型的任意输出 |

---

## 3. 概念模型

```
                      ┌──────────────────────────────┐
                      │        MemoryService         │  门面：策略 / 编排 / 命令
                      └──────────────┬───────────────┘
              ┌──────────────────────┴──────────────────────┐
              ▼                                             ▼
   ┌────────────────────────┐                    ┌────────────────────────┐
   │      SessionStore      │                    │     LongTermStore      │
   │  短期：每会话的对话流水   │                    │  长期：跨会话的事实/偏好  │
   │  Key: SessionKey       │                    │  Key: namespace        │
   │  Backend: JSONL / SQL  │                    │  Backend: JSONL / SQL  │
   └────────────────────────┘                    └────────────────────────┘
              ▲                                             ▲
              │ 最近 N 轮 / 摘要                              │ 召回结果
   ┌──────────┴─────────────────────────────────────────────┴──────────┐
   │                ConversationService（会话编排）                      │
   │  载入历史 → 组装 Context → 调 AgentLoop → 按轮次回写 → 触发提炼     │
   └────────────────────────────────────────────────────────────────────┘
```

### 关键概念

| 概念 | 含义 | 粒度 |
|---|---|---|
| Session（会话） | 一段连续对话的容器；上下文、清空、并发锁的作用单位 | 渠道 + 会话范围 + 会话 ID |
| SessionKey | 会话的结构化标识 | 见 §6 |
| Turn（轮次） | 一次"用户输入 → 模型最终回答"的完整过程；可能包含多次工具调用 | 原子写入单位 |
| Transcript（流水） | 短期记忆中按顺序保存的消息集合（含 tool 消息） | 每会话一个逻辑流 |
| LongTermRecord | 长期记忆中的一条事实/偏好/摘要 | 每用户命名空间内 |
| Namespace | 长期记忆的隔离域 | v1 恒为 `user:default`；未来为 `user:<id>` / `tenant:<id>` |

### 短期 vs 长期的边界（重要）

- **短期**：原始消息、工具调用与结果；用于"接得上话"；**按会话隔离**；会随对话增长而被窗口裁剪/摘要。
- **长期**：跨会话可复用的信息（"用户偏好 Python""正在做 MeowMeowClaw 项目""不要用 exec"）；**跨渠道共享**；写入受策略控制。
- 跨渠道的连续感应通过**长期记忆**获得，而不是把不同渠道的原始对话混进同一个 transcript。

---

## 4. 短期记忆是否区分渠道？

### 结论：必须区分

理由：

1. **上下文语义不同**：CLI 里可能在调试代码，飞书里在讨论工作，QQ 群里在闲聊；混在一起会让模型"答非所问"，并把 A 渠道的内容泄露到 B 渠道。
2. **并发安全**：若所有渠道共用一个会话，两条并发消息会交错写入同一历史，模型看到的消息顺序可能错乱，甚至互相"抢答"。
3. **群聊与私聊必须隔离**：群里其他人可见的消息不应进入私聊上下文；不同群之间也必须隔离。
4. **渠道格式差异**：@、附件、卡片、消息长度限制等属于渠道层语义，不应污染通用上下文。

### 会话粒度建议

| 渠道场景 | SessionKey 形态（示例） | 说明 |
|---|---|---|
| CLI | `cli:direct` | 单用户单会话；需要并行时用 `/new` 生成 `cli:direct:<n>` |
| 飞书私聊 | `feishu:p2p:<open_id>` | 按用户；同一用户多设备共享同一会话 |
| 飞书群聊 | `feishu:group:<chat_id>` | 按群；发言者信息逐条记录在消息元数据里 |
| 飞书话题 | `feishu:group:<chat_id>:<thread_id>` | 若启用话题线程，可细分到 thread |
| QQ 私聊 | `qq:private:<uin>` | 按用户 |
| QQ 群聊 | `qq:group:<group_id>` | 按群 |

补充规则：

- 群聊的"会话"属于群，不属于某个用户；消息记录需附 `sender_id`，长期记忆仍按用户命名空间写入（只记录与该用户相关的部分）。
- CLI 默认延续上一次会话（否则每次启动都失忆）；提供 `/new`（新会话）与 `/clear`（清空当前会话）两个不同语义。
- 同一用户跨渠道不共享短期历史；长期记忆承担"跨渠道连续感"。

---

## 5. 长期记忆是否区分渠道？

### 结论：不区分渠道，但按"命名空间 + 来源元数据"设计

- 单用户多渠道 → 长期记忆天然应共享：偏好、身份、项目状态等与渠道无关。
- 但**接口必须按 namespace 隔离**，v1 使用 `user:default` 即可；未来接多用户/租户时直接切 `user:<id>`、`tenant:<id>`。
- 每条记录保留**来源元数据**：`source_channel`、`source_session`、`source_message_id`、`created_at`，用于：
  - 冲突时判断谁更新、谁更可信；
  - 用户说"忘掉刚才在飞书说的那条"时可精准删除；
  - 审计与调试。
- 渠道特有、临时性的信息不要进长期记忆；如需保存，用 `kind` / `tags` 标注作用域（例如 `scope=channel:qq`），而不是另开一套存储。

---

## 6. 会话键（SessionKey）设计

### 6.1 结构化字段

```
SessionKey:
  channel:        str    # cli / feishu / qq / ...
  scope:          str    # direct / p2p / group / thread
  conversation_id:str    # 渠道内的会话标识（用户 open_id、群 chat_id、CLI 名称）
  user_id:        str?   # 渠道内的用户标识（私聊必填；群聊为发送者）
  tenant_id:      str?   # 飞书等平台租户（未来多租户用）
```

- 内存里始终用结构化对象，序列化为规范字符串（canonical key）用于日志、索引与持久化；
- 规范字符串建议包含版本前缀：`v1:feishu:p2p:ou_xxx`，为将来键格式变化留迁移空间。

### 6.2 文件名不能直接用 `session_key`

用户设想 `_get_session_path` 把 `:` 替换为 `_`，**作为 JSONL 后端的私有实现可以理解，但作为持久标识不安全**：

| 问题 | 说明 |
|---|---|
| 碰撞 | `a:b_c` 与 `a_b:c` 都变成 `a_b_c`；渠道与 ID 中可能出现 `_` |
| 越界 | key 若含 `../` 或 `/`，拼接后可逃逸出 sessions 目录 |
| Windows 兼容 | `:` 在 Windows 文件名中非法；`?`、`*`、`<`、`>`、`"` 等也需处理 |
| 长度/Unicode | 群/用户 ID 可能很长或含非 ASCII，直接进文件名不稳 |
| 不可逆 | `list_sessions()` 无法从文件名可靠还原原始 key |

### 6.3 建议的存储 ID

- 逻辑 key：保留完整规范字符串，写入会话元数据/索引；
- 物理文件名：`<storage_id>.jsonl`，其中
  - 优先方案：`storage_id = base32(sha256(canonical_key))[:26]`（定长、无特殊字符、跨平台）；
  - 可读性方案：`<channel>__<urlquote(conversation_id, safe="")>__<short_hash>`，便于人工排查；
- `sessions/index.jsonl`（或 `meta.json`）保存 `storage_id ↔ canonical_key ↔ 元数据` 的映射，`list_sessions()` 读索引而不是反解文件名。

---

## 7. 对预设 5 个方法的逐条分析

> 总评：5 个方法作为 **JSONL 后端的内部实现**是合理的起点；但不适合直接作为对外接口。
> 主要问题：文件路径泄漏进接口、缺少轮次原子性与锁、缺少窗口化、缺少会话元数据、把"清空"简单等同于删文件。

### 7.1 `_get_session_path(session_key) -> str`

- **合理**：后端内部需要从逻辑会话映射到物理文件；私有命名（`_`）方向正确。
- **问题**：
  1. `:` → `_` 有碰撞与越界风险（见 §6.2）；
  2. 返回 `str` 把"路径"暴露为契约，换 MySQL/Redis 后无意义；
  3. 目录不存在时的创建时机、权限、锁文件位置未定义。
- **建议**：留在 `JsonlSessionStore` 内部，改名为 `_path_for(storage_id)`；对外只接受 `SessionKey`，路径概念不出存储层。

### 7.2 `save_message(session_key, message: dict)`

- **合理**：append-only JSONL + `ensure_ascii=False` 适合中文；追加写入天然适合"日志型"数据。
- **问题**：
  1. **原子性**：一轮对话包含 user / assistant(tool_calls) / tool / assistant(final) 多条消息，逐条 append 时进程崩溃会产生"半轮"；恢复后模型可能看到不完整上下文；
  2. **并发**：多进程/多协程同时 append 会交错甚至写坏行；需要锁或单写者；
  3. **时间**：本地 ISO 时间不利于排序与跨时区，建议 **UTC epoch millis 或 UTC ISO8601**，另存来源时区；
  4. **元数据不足**：缺少 `id`、`turn_id`、`seq`、`schema_version`、`channel/sender`、`token 估算`等，后续做窗口化/去重/审计会缺信息；
  5. **幂等**：IM 渠道会重推事件；同一 `message_id` 重复写入会污染历史，需要入口去重或写入幂等键；
  6. **校验**：任意 dict 直接落盘，坏数据会一直留在流水中，读取方必须容错。
- **建议**：改为 `append_turn(session_key, messages, *, turn_id, meta)`，一轮写一行、一次 `write()` 调用；行内含 `schema_version / seq / ts / messages[]`；写入前置校验与幂等键。

### 7.3 `get_history(session_key) -> list[dict]`

- **合理**：逐行解析、跳过空行、文件不存在返回空列表；"剥掉 timestamp 再返回"符合 OpenAI messages 契约。
- **问题**：
  1. **全量读取**：历史无限增长时每次全读，O(n) 且上下文会超模型窗口；接口必须支持 `limit` / `max_chars`（或 token 预算）与"从最近往前取"；
  2. **字段剥离硬编码**：未来还有 `turn_id/seq/channel` 等内部字段；应做显式投影 `to_llm_messages()`，而不是删一个 timestamp；
  3. **并发读**：写入方正在 append 时可能读到半行；需跳过最后一条不完整 JSON 并告警；
  4. **可变性**：直接返回内部列表/字典引用会被调用方误改，应返回新对象；
  5. **性能**：大文件每次解析开销大；JSONL 后端可加"最近 N 条"的尾部读取优化，但接口语义不应依赖文件实现。
- **建议**：`load_recent(session_key, *, max_messages=None, max_chars=None) -> list[SessionMessage]`；由上层 `ContextWindowPolicy` 决定预算；坏行跳过 + 计数上报。

### 7.4 `clear(session_key)`

- **合理**：用户需要"清空上下文"；文件删除最简单。
- **问题**：
  1. **语义**：删除 = 不可恢复；是否需要归档（审计、误删恢复、"清空但保留长期记忆摘要"）应先定义；
  2. **并发**：必须与写入共用同一把锁；否则 clear 与 append 竞争会复活旧数据或写回被删文件；
  3. **一致性**：内存中的 `AgentLoop._session_history` 也要同步清空，否则"删了文件但模型还记得"；
  4. **长期记忆**：`/clear` 是否清长期记忆？两者应分开：`/clear` 只清当前会话；`/forget` 才删除指定长期记忆。
- **建议**：接口区分 `clear_session(session_key, archive: bool = True)` 与 `purge_all()`；文档化语义；实现用 tmp+rename 重写或移动到 `archive/`。

### 7.5 `list_sessions() -> list[str]`

- **合理**：管理/调试需要枚举会话；扫描 `.jsonl` 简单直接。
- **问题**：
  1. 需要反解文件名 → 不可靠（§6.2）；
  2. 只返回 key，没有 `updated_at / message_count / last_message`，无法排序、清理与展示；
  3. 空会话（清空后留下的空文件）是否算一个会话未定义；
  4. 每次全目录扫描，会话多时慢；MySQL/Redis 后端语义不同。
- **建议**：`list_sessions() -> list[SessionMeta]`；JSONL 后端维护 `index.jsonl`；返回按 `updated_at` 倒序。

### 7.6 接口还缺什么

| 缺失能力 | 用途 |
|---|---|
| `get_meta(session_key)` / `upsert_meta` | 会话展示、恢复、列表排序 |
| `exists(session_key)` | 避免无意义建文件 |
| `append_turn`（批量 + 原子） | 半轮崩溃问题、减少锁次数 |
| `load_recent(limit/budget)` | 上下文窗口控制 |
| `compact(session_key)` / `rotate` | 长会话压缩、摘要、归档 |
| `close()` / `health()` | 生命周期与可观测性 |
| `namespace` 级长期记忆接口 | 跨渠道共享（与 SessionStore 职责不同） |
| 锁/事务钩子 | 并发正确性（见 §10） |

---

## 8. 接口设计（示意，非实现）

### 8.1 数据模型

```
SessionKey:  channel, scope, conversation_id, user_id?, tenant_id?
SessionMessage:
    id, role, content, name?, tool_call_id?, tool_calls?,
    ts, turn_id, seq, sender_id?, channel?, raw?
SessionMeta:
    storage_id, key, channel, scope, conversation_id, user_id?,
    created_at, updated_at, message_count, turn_count,
    schema_version, last_message_preview?, extra?
MemoryRecord:
    id, namespace, kind(fact/preference/summary/…), content,
    tags[], confidence, source_session?, source_message_id?,
    created_at, updated_at, expires_at?, extra?
```

- `SessionMessage` 是"存储视角"，`to_llm_messages()` 负责投影成 OpenAI messages；
- `MemoryRecord` 是"长期记忆视角"，与原始消息解耦。

### 8.2 SessionStore（短期，按会话）

```
async append_turn(key, messages, *, turn_id=None, meta=None) -> SessionMeta
async load_recent(key, *, max_messages=None, max_chars=None) -> list[SessionMessage]
async get_meta(key) -> SessionMeta | None
async clear(key, *, archive=True) -> None
async list_sessions() -> list[SessionMeta]
async compact(key) -> None
async close() -> None
```

语义要点：

- `append_turn`：一轮原子写入；内部加锁/事务；
- `load_recent`：越界参数有默认；坏尾行跳过；
- `clear`：幂等；默认归档；
- 所有方法失败抛统一的 `MemoryStoreError`，由上层决定降级策略。

### 8.3 LongTermStore（长期，按命名空间）

```
async add(namespace, record) -> MemoryRecord
async upsert(namespace, record) -> MemoryRecord        # 按 id/内容指纹去重
async search(namespace, *, query=None, kinds=(), tags=(), limit=20) -> list[MemoryRecord]
async delete(namespace, record_id) -> None
async clear(namespace) -> None
async list_namespaces() -> list[str]
async close() -> None
```

- v1 的 JSONL 实现 `search` 只做关键词/标签过滤；
- 未来 SQL 后端可用 `LIKE/FULLTEXT`，或另加向量索引，接口保持不变。

### 8.4 MemoryService（门面，业务策略）

职责：

1. `recall(session_key) -> str`：长期记忆 → 注入 System Prompt 的文本（按 token 预算裁剪）；
2. `record_turn(session_key, user_message, agent_messages)`：写短期流水 + 触发长期提炼策略；
3. 命令语义：`clear_session`、`new_session`、`remember(text)`、`forget(id/query)`、`export()`；
4. 策略：何时摘要、何时提炼长期记忆、敏感信息过滤、写入失败降级。

### 8.5 为什么接口现在就要 async

- 当前 AgentLoop 已全异步；JSONL 很快，但一旦调用方按同步接口写，将来换 aiomysql / redis.asyncio 就要改 AgentLoop、Service、测试与渠道层；
- JSONL 后端用 `await asyncio.to_thread(...)` 包装即可，代价可接受；
- 若担心事件循环阻塞，也可先用同步实现 + 上层 `to_thread`，但**接口签名保持异步**。

---

## 9. JSONL 后端设计

### 9.1 文件布局

```
<workspace>/memory/
├── sessions/
│   ├── index.jsonl              # storage_id ↔ canonical_key ↔ meta
│   ├── <storage_id>.jsonl       # 每会话一个 turn 流水
│   └── <storage_id>.jsonl.lock  # 跨进程锁(可选)
├── long_term/
│   ├── facts.jsonl              # 长期记忆记录(追加写)
│   └── namespaces.json          # 命名空间索引(可选)
├── archive/                     # clear/compact 后的归档
└── MEMORY.md                    # 兼容旧版的人设式记忆(迁移后只读或废弃)
```

### 9.2 流水行 schema（示意）

```json
{
  "schema_version": 1,
  "seq": 12,
  "turn_id": "01J8Z...",
  "ts": 1760000000123,
  "messages": [
    {"role": "user", "content": "帮我看下 README", "ts": 1760000000000},
    {"role": "assistant", "content": null, "tool_calls": [ ... ], "ts": 1760000000050},
    {"role": "tool", "tool_call_id": "call_1", "content": "...", "ts": 1760000000080},
    {"role": "assistant", "content": "看完了...", "ts": 1760000000120}
  ],
  "meta": {"channel": "feishu", "sender_id": "ou_xxx", "token_estimate": 812}
}
```

- **一轮一行**：工具调用与结果跟随该轮一起落盘，避免半轮；
- `seq` 在会话锁内单调递增，是排序的可靠依据；`ts` 仅作展示；
- `schema_version` 支持未来字段演进；未知字段读取时忽略但保留。

### 9.3 写入与恢复

- append 一行：`json.dumps(..., ensure_ascii=False, separators=(",", ":")) + "\n"`；
- 同一进程内用 `asyncio.Lock`；跨进程用文件锁（`fcntl.flock` / `portalocker`），锁文件与数据文件同目录；
- 需要更强持久性时 `flush + os.fsync`（每轮一次，性能可接受）；崩溃时最多丢最后一轮；
- 读取时遇到不完整行：跳过并 `logger.warning`，不阻塞后续读取；
- 重写类操作（clear/compact）一律 `写 .tmp → fsync → os.replace`，避免"删到一半"；
- 文件权限：目录 `0700`、文件 `0600`。

### 9.4 索引与列举

- `index.jsonl` 每条记录：`storage_id, key, channel, scope, conversation_id, user_id, created_at, updated_at, counts`；
- 每次 append/clear 后顺手更新（同一会话锁内），或采用"惰性重建"（扫描所有文件首尾行）；
- `list_sessions()` 只读索引，不反解文件名；索引损坏时可重建。

### 9.5 压缩、轮转与摘要

- 触发条件：文件行数/字节数超阈值，或加载时超出上下文预算；
- 流程：把最旧的 K 轮交给模型生成摘要 → 写入 `long_term`（`kind=summary`）或会话头部 `summary` 记录 → 归档原文件并重写保留尾部；
- 摘要失败不得丢数据：先写归档，再做替换；
- `MEMORY.md` 的旧内容在迁移时转为 `kind=fact` 记录或保留为手工维护的"人设补充"，由 LongTermStore 读取并参与 `recall()`。

### 9.6 JSONL 的局限与退出条件

| 局限 | 触发换后端的信号 |
|---|---|
| 无索引/查询弱 | 需要按内容/时间/标签检索，或会话数 > 数千 |
| 无事务、改写成本高 | 需要编辑/删除单条消息、跨表一致性 |
| 并发依赖文件锁 | 多进程/多服务同时写同一会话 |
| 无原生 TTL/自增/聚合 | 需要统计、限流、审计报表 |
| 明文存储 | 需要加密/权限分级/合规 |

出现上述任一信号，优先升级到 **SQLite**（本地、事务、零运维），再视部署形态升级 MySQL。

---

## 10. 并发设计（同一用户多渠道）

### 10.1 场景矩阵

| 场景 | 风险 | 处理 |
|---|---|---|
| 不同渠道并发 | 短期本应隔离；长期共享会并发写 | 短期按会话互不影响；长期按 namespace 锁 |
| 同渠道同会话连续消息 | 上下文交错、重复回复 | 会话内串行（队列/锁） |
| 同渠道多设备 | 同一 SessionKey | 同上，串行 + 消息按 seq 排序 |
| CLI 进程 + 渠道服务进程 | 文件级竞争 | 单写者优先；否则文件锁 |
| IM 平台重推事件 | 重复写入/重复回复 | `event_id` 去重 + 幂等键 |
| 一轮中途崩溃 | 半轮上下文 | 按轮原子写；待回复消息进入待处理队列 |
| `/clear` 与写入并发 | 数据复活/丢失 | clear 与 append 共用同一把会话锁 |

### 10.2 锁的分层

```
全局              → 不使用（会串行化所有会话）
每 SessionKey     → 进程内 asyncio.Lock（弱引用字典）+ 跨进程文件锁（可选）
每 Namespace      → 长期记忆写入锁（user:<id>）
每存储实例        → close() 时统一释放
```

- **进程内**：用一个 `dict[SessionKey, asyncio.Lock]`（弱引用或带 LRU 清理），保证同一会话同时只有一轮在跑；
- **跨进程**：`fcntl.flock` 包住"读历史 → 追加一轮"的临界区；锁粒度是单个会话文件；
- **不要**把全局锁放在模型调用外层：不同会话必须能并行；
- 同一会话是否要在整个模型调用期间持锁？
  - **v1 推荐：持锁整轮**。单用户场景下同会话本来就不该并行；实现简单、顺序严格；
  - 未来高并发可改为"短锁读快照 + 写时乐观校验（seq/version）+ 冲突重试"。

### 10.3 单写者原则（推荐演进方向）

- **阶段 1（CLI 单进程）**：进程内锁即可，无需文件锁；
- **阶段 2（多渠道服务）**：一个常驻 gateway 进程拥有所有渠道与存储；CLI 若同时运行，改为连接该服务（本地 socket/HTTP），不再直接写文件；
- **阶段 3（多进程/多机）**：MySQL 事务 + Redis 分布式锁；文件锁退化为迁移期兼容。

单写者能同时解决：文件锁复杂度、事件顺序、长期记忆冲突、限流与审计。

### 10.4 长期记忆的并发与冲突

- 同一 namespace 的写入用锁串行；
- 记录级去重：`id`（ULID）+ `content_hash` + `(namespace, kind, normalized_content)` 唯一约束（SQL 后端）；
- 冲突策略：
  - 显式 `记住` 优先于自动提炼；
  - 新记录覆盖旧记录时保留 `supersedes` 链，不物理删除；
  - 时间相近的冲突交给用户确认（渠道回复"有两条冲突的偏好，采用哪条？"）；
- `forget` 支持按 id、关键词、来源会话删除；删除也要走锁与审计。

### 10.5 幂等与事件去重

- 渠道层收到事件先按 `(channel, event_id)` 查重（内存 + 持久化去重表）；
- 记忆层的 `append_turn` 接受 `turn_id`/`message_id`，重复写入直接跳过；
- 回复失败重试时，用 `reply_id` 防止重复发送（渠道适配层职责）。

### 10.6 崩惯恢复

- 写入顺序：**先落待处理事件（pending）→ 生成回复 → 落 turn → 标记完成**；
- 若在模型调用中崩溃：重启后能看到 pending，可选择重试或回复"刚才处理中断了"；
- 若在落 turn 时崩溃：turn 原子写保证要么完整要么没有；不完整行读取时跳过；
- `clear`/`compact` 用 tmp+rename，崩溃不会留下半改文件。

---

## 11. 存储位置放哪里？

### 候选对比

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| A. 项目根 `meowmeowclaw/memory/` | 直观 | 污染代码库；wheel 安装后不可写；备份/权限混乱 | ❌ |
| B. `<workspace>/memory/` | workspace 已是运行时数据目录；可被 `.env` 绝对路径整体迁移；现有 `MEMORY.md` 预留位置一致 | 与 Agent 可操作的文件区重叠，需禁止工具访问 | ✅ **推荐** |
| C. XDG `~/.local/share/meowmeowclaw/` | 符合 Linux 应用规范；安装形态友好 | 与当前"仓库根 workspace"心智不一致；需要新配置 | 后续可选 |
| D. 独立 `data_dir` 配置 | 最灵活 | v1 多一个配置项与概念 | 以 B 为默认，保留 `memory_dir` 覆盖 |

### 推荐

- 默认：`<workspace>/memory/`，并新增配置项 `memory_dir`（默认取 `<workspace>/memory`）；
- 用户已经可以通过 `.env workspace=/abs/path` 整体迁移数据；将来要拆开时只需设 `memory_dir=/var/lib/...`；
- 项目根只保留 `identity.md` 等源码级资源，不存运行时记忆；
- **工具隔离**：在文件工具的路径策略中把 `memory_dir` 加入拒绝列表（读/写/列举都拒绝），或至少禁止写入与列举；否则 Agent 可以读取自己的记忆（隐私/注入风险）或改写记忆文件。
- 权限与备份：目录 `0700`、文件 `0600`；`memory/` 不入 git；建议随 workspace 一起备份；
- 安装形态下（wheel）`workspace` 已建议配置绝对路径，记忆自然跟着走。

---

## 12. 与现有代码的集成点

| 位置 | 现状 | 记忆系统接入方式 |
|---|---|---|
| `paths.py` | 项目根/workspace/identity 唯一来源 | 增加 `DEFAULT_MEMORY_DIR = DEFAULT_WORKSPACE / "memory"` 或由 config 解析 |
| `config.py` | 纯解析 Settings | 增加 `memory_dir`、`memory_backend`（jsonl/sqlite/mysql）、`session_limit` 等；仍保持无副作用 |
| `bootstrap.py` | 组合根装配 Application | 构造 `MemoryService` 并注入 `Application`；按配置选择后端 |
| `agent/loop.py` | 持有 `_session_history` | 两种路线：① 注入 store+key；② 抽出 `ConversationService`，AgentLoop 接收历史快照（推荐②） |
| `agent/context.py` | 直接读 `workspace/memory/MEMORY.md` | 改为注入 `long_term_provider`/`recall` 文本；存储细节下沉到 LongTermStore |
| `cli.py` | `/clear` 调 `agent.clear_history()` | 改为调 `MemoryService.clear_session()`，并同步内存历史；新增 `/new`、`/remember`、`/forget`（后两者可选） |
| `tools/filesystem.py` | 工作区路径防护 | 在 `resolve_in_workspace` 或 ToolPolicy 中拒绝记忆目录 |
| 渠道适配层（未来） | 无 | 只调用 `ConversationService.handle(session_key, incoming)`，不直接碰存储 |
| `tests/` | 现有 loop/context 测试 | 增加 SessionStore/LongTermStore 契约测试、并发测试、崩溃恢复测试、迁移测试 |

### AgentLoop 改造建议（二选一）

- **方案 A（改动小）**：`AgentLoop(store, session_key, ...)`，`run()` 内 load/append。优点是快；缺点是控制流与持久化耦合，并发/测试复杂。
- **方案 B（推荐）**：新增 `ConversationService`，负责 `load_recent → ContextBuilder → AgentLoop.run(history) → append_turn`；`AgentLoop.run(user_message, *, history=None)` 改为可接收外部历史快照，内部仍保留无 store 的纯逻辑。
- 方案 B 让 AgentLoop 保持"可替换、可并发（每会话一实例）"，也为将来换编排/多 Agent 留空间。

---

## 13. 演进到 MySQL / Redis

### 13.1 接口不变，替换后端

- `MemoryService` 只依赖 `SessionStore` / `LongTermStore` 协议；
- `bootstrap` 根据 `memory_backend` 选择实现；
- 数据迁移工具（JSONL → SQL）作为一次性脚本/模块，按 `schema_version` 与 `storage_id` 去重导入。

### 13.2 MySQL 表结构草案

| 表 | 关键字段 | 索引 |
|---|---|---|
| `sessions` | `storage_id PK, session_key UNIQUE, channel, scope, conversation_id, user_id, created_at, updated_at, message_count, turn_count, meta JSON` | `UNIQUE(session_key)`、`(user_id, updated_at)` |
| `messages` | `id PK, storage_id FK, seq, turn_id, role, content JSON, ts, sender_id, meta JSON` | `(storage_id, seq)`、`(turn_id)` |
| `memory_records` | `id PK, namespace, kind, content, tags JSON, confidence, source_session, source_message_id, created_at, updated_at, expires_at, superseded_by` | `(namespace, kind, updated_at)`、`FULLTEXT(content)`（可选） |
| `events`（去重/幂等） | `channel, event_id PK/UNIQUE, received_at, status` | 主键/唯一键 |

- `append_turn` 在事务内插入 messages 并更新 sessions 计数；
- `load_recent` 用 `WHERE storage_id=? ORDER BY seq DESC LIMIT ?` 再反转；
- `clear` 支持事务内删除或归档表；
- 迁移期间可"双写 + 读新后端回退旧后端"。

### 13.3 Redis 的定位

- **适合**：热会话缓存（最近 N 条）、分布式锁、事件去重（SETNX+TTL）、限流、任务队列/Streams；
- **不适合**：作为长期记忆与审计的唯一持久层（持久化策略与容量约束）；
- 推荐组合：**SQL 主存储 + Redis 缓存/锁/队列**；
- 接口无需感知 Redis，可在 `MemoryService` 层加缓存装饰器（cache-aside）。

### 13.4 迁移顺序建议

```
JSONL → SQLite（本地、事务、零运维，接口不变） → MySQL（服务化/多进程）
                     ↘ Redis 仅做缓存/锁/队列（可选，提前或并行）
```

即使单用户，只要进入"多进程多渠道"，SQLite 也比 JSONL 更省心（事务、索引、单文件备份）。

---

## 14. 记忆写入策略与安全

### 14.1 什么进入长期记忆

| 来源 | 是否写入 | 说明 |
|---|---|---|
| 用户显式"记住…/以后都…" | ✅ 直接写 | 最高优先级 |
| 系统提炼（对话摘要、稳定偏好） | ⚠️ 受控写 | 需置信度阈值、去重、可追溯；可先入"候选区" |
| 模型自由输出 | ❌ 默认不写 | 防止幻觉/注入污染 |
| 渠道原始消息 | ❌ 不进长期 | 只进短期 transcript |
| 工具结果 | ❌ | 除非提炼出结论且被确认 |

### 14.2 安全要点

- 渠道消息默认**不可信**：prompt injection 可能诱导模型调用工具或写记忆；长期写入必须受策略与权限约束；
- 记忆目录禁止被 read/write/list 工具访问（§11）；
- 敏感信息过滤：API key、密码、证件号等在落盘前打码/拒绝写入（至少长期记忆禁止）；
- 权限：目录 0700、文件 0600；备份加密可选；
- 审计：记录来源会话、写入时间、策略版本；删除采用软删/归档；
- 命令权限：远程渠道默认禁用 `exec`，文件工具限制到会话工作区；`/forget`、`/clear` 仅私聊/白名单可用。

---

## 15. 上下文窗口与摘要策略

- `load_recent` 只保证"取最近若干"；上层 `ContextWindowPolicy` 负责：
  1. 预留 System Prompt 与工具定义预算；
  2. 优先保留最近 N 轮完整对话；
  3. 超出预算时用会话摘要（`kind=summary`）替换更早的轮次；
  4. 必要时触发 `compact` 落盘，避免每次重新摘要；
- 摘要记录带 `covers_seq_range`，加载时"摘要 + 尾部原始轮次"组合；
- 不把长期记忆全量塞进 Prompt：`recall()` 按当前输入做过滤/打分，限制条数与字符数。

---

## 16. 测试策略（设计阶段先定契约）

| 测试类型 | 内容 |
|---|---|
| 契约测试 | 同一组用例对 JSONL/SQLite/MySQL 后端参数化，接口行为一致 |
| 崩溃恢复 | 半行 JSON、部分 turn、进程中断后恢复 |
| 并发 | 同会话两任务串行、不同会话并行、多进程文件锁 |
| 幂等 | 重复 event_id/turn_id 不重复写入 |
| 迁移 | JSONL → SQLite/MySQL 导入后消息数、顺序、元数据一致 |
| 安全 | 工具无法访问 memory 目录；长期写入策略拒绝敏感内容 |
| 上下文 | 超预算时摘要+尾部组合正确、不丢最近轮次 |

---

## 17. 分期落地建议（不涉及实现）

| 里程碑 | 内容 | 退出标准 |
|---|---|---|
| M0 | 本文档评审、字段/命名/键格式冻结 | 决策表评审通过 |
| M1 | `SessionKey`/`SessionMessage`/`SessionMeta` 类型 + JSONL `SessionStore` | 契约测试 + 崩溃/并发测试通过 |
| M2 | `ConversationService` + AgentLoop 历史注入；CLI 接入；`/clear` `/new` | CLI 多轮、重启恢复一致 |
| M3 | `LongTermStore` + `MemoryService.recall/remember/forget`；ContextBuilder 注入 | 跨渠道共享记忆、显式写入可用 |
| M4 | 并发完善：文件锁、事件去重、待处理队列 | 两进程并发写不坏数据 |
| M5 | 压缩/摘要/窗口策略 | 长会话不超模型窗口 |
| M6 | SQLite 后端 + 迁移工具；Redis 缓存/锁可选 | 后端可切换、数据可迁移 |
| M7 | 渠道接入（飞书/QO 等）使用 `ConversationService` | 多渠道闭环 |

---

## 18. 待决问题

1. CLI 每次启动的默认会话：延续上次，还是按日期自动新会话？（建议默认延续 + `/new`）
2. 群聊中 bot 的触发规则（@、前缀、白名单）与会话粒度（群 vs 群+用户）？
3. 长期记忆写入是否需要"候选区 + 用户确认"两步，还是高置信度直接写？
4. 摘要使用哪个模型/是否单独预算，摘要失败时如何降级？
5. 是否需要向量检索（若需要，嵌入模型与存储从哪一层引入）？
6. 记忆是否需要加密（v1 依赖文件权限，还是直接引入加密）？
7. `/remember` 的持久对象：用户级（跨渠道）还是会话级？两者如何共存？
8. 何时从 JSONL 升级 SQLite（按会话数、消息量还是多进程需求触发）？
