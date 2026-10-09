# MeowMeowClaw A+ 优化设计：会话 IO 尾读与运行时生命周期（v1）

> 状态：**设计已确认（全部默认），待实现**。本文只定义 A+ 的行为、接口、阶段与验收，
> **不改 JSONL 文件格式、不改 AgentLoop/压缩语义**；实现按 §5 阶段推进。
> 关联：`docs/GATEWAY_DESIGN.md` ADR-1 §15.8/§16.5、`meowmeowclaw/memory/jsonl.py`、
> `meowmeowclaw/conversation.py`、`meowmeowclaw/bootstrap.py`。

---

## 0. 背景与目标

### 0.1 背景

ADR-1 决策为"当前采用方案 A（`ConversationService` 独占会话运行时），不迁移方案 B（Gateway 常驻上下文）"，
并把方案 B 的合理诉求收敛为 **A+ 路线**：

1. **会话 IO 从 O(总会话长度) 降到 O(窗口)** —— 这是 A 当前唯一的真实性能短板；
2. **会话运行时生命周期 API** —— 显式缓存/淘汰/销毁，替代"永不释放"；
3. **可观测指标** —— `load_recent` p50/p95、缓存数、文件大小、RSS。

量化依据见 ADR-1 §15.3：2000 轮 / 15.47MB 时 `load_recent` ≈ 39.8ms，且成本随文件线性增长；
而真实 LLM 单轮 1~5s 时占比仍小，高频/长会话/本地快模型下才会被放大。A+ 的目标是让性能不再成为
迁移方案 B 的理由。

### 0.2 目标

| # | 目标 | 验收指标 |
|---|---|---|
| G1 | `load_recent` 成本与总会话长度解耦 | 2000 轮文件 p95 ≤ ~3ms，且 50→2000 轮基本平坦 |
| G2 | `get_meta` / `append_turn` 单次成本 O(1) | 两者 p95 ≤ ~1ms（2000 轮文件） |
| G3 | `list_sessions` 不再全量扫描每个文件 | 每会话只读 header + 末条有效 turn |
| G4 | 会话运行时可控 | `close_session / evict_idle / max_cached_sessions` 可用；无并发/串行回归 |
| G5 | 可观测 | `ConversationService.stats()` 暴露延迟分位、缓存数、文件大小、RSS（平台允许时） |
| G6 | 不退化 | 全量回归 + 崩溃恢复语义 + 真实 CLI/QQ 冒烟通过 |

### 0.3 非目标

- 不引入 offset 索引文件、不引入 SQLite/Redis 等新后端（ADR D28 另议）；
- 不让内存成为事实源（JSONL 仍是唯一真相；淘汰只丢进程内辅助状态）；
- 不改 `SessionStore` 协议签名、不改 JSONL schema、不改压缩/审计语义。

---

## 1. 已确认决策

| # | 决策 | 结论 |
|---|---|---|
| A+1 | 元数据计数口径 | `turn_count` 取末条有效 turn 的 `seq`、`message_count` 取 `message_total`（符合 `jsonl.py:11` 原注释意图）；中间行损坏时口径由"有效行计数"变为"累计序号"，在文档注明 |
| A+2 | 尾读实现 | **反向块读**（无索引文件、无 schema 变更、跨进程安全），异常时回退现有全量扫描 |
| A+3 | 淘汰粒度 | **`_SessionRuntime` + 引用计数**（agent + lock 一起管理），避免"锁被替换导致同会话并发" |
| A+4 | 默认配置 | `memory_max_cached_sessions=256`、`memory_cache_idle_seconds=0`（0 = 关闭自动 TTL，仅显式清理 + 容量 LRU） |

---

## 2. 现状审计（代码级瓶颈）

| 位置 | 行为 | 复杂度 |
|---|---|---|
| `jsonl.py::load_recent()` → `_read_turns()` | 每轮装载解析整个 JSONL，再做 `turns[-N:]` + `_trim_by_chars` | O(总长度) |
| `jsonl.py::get_meta()` → `_read_file()` | QQ `_resolve_active_session` 每条消息调用；active 不存在时再扫 archive | O(总长度) |
| `jsonl.py::append_turn()` → `_read_file()` | 文件锁内全量扫描，仅为取 `previous_seq / message_total` | O(总长度) |
| `jsonl.py::list_sessions()` → `_read_file()` × N | `/sessions`、短 ID 解析要扫所有会话全部内容 | O(所有会话字节) |
| `conversation.py::_agents/_locks` | 只增不减；`archive_session/purge_session` 不释放；无容量上限 | 会话数线性内存 |
| 观测 | 仅 `Gateway.stats()`；存储层无延迟/缓存指标 | — |

> 关键事实：一次普通 QQ 轮次会触发 **3 次全文件扫描**（get_meta + load_recent + append），文件越大越明显。
> `_read_file` 的 docstring 本就写着"读文件头与末轮统计"（O(1) 意图），但实现是全量遍历——A2 即修正该偏差。

### 2.1 基准基线（ADR-1 §15.3，实测）

窗口 `max_turns=50 / max_chars=120000`、每轮 user+assistant 各约 1200 字符、`/tmp` 存储：

| 总会话数 | JSONL 文件 | `load_recent`+投影 avg | p95 |
|---:|---:|---:|---:|
| 50 | 0.39 MB | 1.32 ms | 1.64 ms |
| 200 | 1.55 MB | 3.99 ms | 4.40 ms |
| 500 | 3.87 MB | 9.39 ms | 10.53 ms |
| 1000 | 7.73 MB | 18.12 ms | 20.70 ms |
| 2000 | 15.47 MB | 39.83 ms | 44.64 ms |

---

## 3. 详细设计

### 3.1 A1：指标与基准（先行，无行为变化）

**新增 `meowmeowclaw/memory/metrics.py`**

```python
class LatencyStats:
    """有界延迟样本统计(count / last / avg / p50 / p95)."""
    def __init__(self, maxlen: int = 256) -> None: ...
    def record(self, elapsed_ms: float) -> None: ...
    def snapshot(self) -> dict[str, float | int]: ...

class StoreMetrics:
    """JsonlSessionStore 三项主路径的耗时与调用计数."""
    load_recent: LatencyStats
    get_meta: LatencyStats
    append_turn: LatencyStats
    def snapshot(self) -> dict[str, dict]: ...
```

**`JsonlSessionStore`**：`load_recent/get_meta/append_turn` 首尾包 `time.perf_counter()`，写入 `self.metrics`；
新增 `def stats(self) -> dict[str, Any]`，附带 `active_file_count`、`last_file_size_bytes`。

**`ConversationService.stats()`**

```python
{
  "cached_agents": int, "locks": int, "evictions": int,
  "store": store.stats() if hasattr(store, "stats") else {},
  "rss_mb": float | None,   # resource.getrusage 可用时; Windows 省略
}
```

**`scripts/bench_session_store.py`**：生成 50/200/500/1000/2000 轮文件，分别测量
`load_recent / get_meta / append_turn` 的 avg/p95（20 次采样），输出与 §2.1 对比表；不进 pytest CI。

**验收**：现有用例全绿；`stats()` 可用；基线脚本可复现 §2.1（±20%）。

### 3.2 A2：尾读基础设施 + meta/append/list 改造

**反向块读工具**（`jsonl.py` 私有方法或 `memory/tail.py`）：

```python
def read_last_lines(path, *, max_lines: int | None = None,
                    max_bytes: int | None = None) -> list[bytes]: ...
def read_last_valid_turn(path) -> dict | None: ...
def read_header(path) -> dict | None: ...
```

算法要点：

1. 二进制 `open(path, "rb")`，`seek(0, SEEK_END)` 后按固定块（如 64KB）向前读；
2. 按 `b"\n"` 切分，**只解码完整行**（避免 UTF-8 多字节边界被截断）；
3. 末尾无换行 → 丢弃半行（崩溃残留），继续向前找完整行；
4. 逐行 `json.loads`，跳过损坏行 → 返回最后 N 条有效 turn / header；
5. `OSError`、`UnicodeDecodeError`、找不到有效 header/turn 等异常 → **回退现有全量 `_read_file`**，
   保证行为与兼容性优先。

**改造点**

| API | 新路径 | 回退条件 |
|---|---|---|
| `get_meta()` | 首行 header + `read_last_valid_turn()`；计数取 `last_turn["seq"]` / `["message_total"]` | 无有效 header/turn、读失败 |
| `append_turn()` | 文件锁内：header 存在性 + 末尾有效 turn 的 `seq/message_total` | 尾行异常/无有效 turn |
| `list_sessions()` | 每个文件 header + 末条有效 turn | 同上 |

- `_read_file` / `_read_turns` 全量路径**保留**为兜底与修复路径；
- `_append_records` 的"补换行"逻辑不变；
- 语义变化（A+1）：正常文件计数完全一致；中间行损坏时 `turn_count` 取 `seq`（累计口径）。

**必须保持的场景（现有测试）**

- 末尾半行 JSON（无换行）：读取忽略、追加补换行、`seq` 从有效记录继续；
- 末尾损坏行/非 dict JSON：跳过，计数取最后有效 turn；
- 双 store 实例并发追加：文件锁 + 尾读保证 seq 不重复。

### 3.3 A3：`load_recent` 尾读 + 窗口裁剪

```
反向读取（新 → 旧）：
  for line in read_last_lines(path, max_bytes=预估上限):
      record = parse(line); 非 turn/损坏 → skip
      turns.append(record)
      if max_turns and len(valid_turns) >= max_turns: break
      if max_chars and 累计字符 > max_chars and 至少保留了最新 1 轮: break
  turns.reverse()
  再做现有 _trim_by_chars（保证与旧路径逐字节一致的裁剪结果）
```

- 保持语义：不切开 turn、`max_turns=0/None`、`max_chars=0/None`、损坏消息跳过；
- 预估读取上限：`max_chars` 是字符而块读按字节，按 UTF-8 最坏 3~4 字节/字符取上界（如 `max_chars * 4 + 64KB`），
  不足时继续向前读，直到满足窗口或到达文件头；
- 读取异常/解码异常 → 回退 `_read_turns` 全量路径；
- **差分测试**（关键）：随机生成含损坏行/半行/中文/工具调用的会话文件，
  断言"尾读结果 == 全量读取后窗口裁剪结果"。

**验收**：2000 轮文件 `load_recent` p95 ≤3ms；50→2000 轮 p95 差值 ≤2ms；差分测试 200 组通过。

### 3.4 A4：会话运行时生命周期 API

**内部结构（`conversation.py`）**

```python
@dataclass
class _SessionRuntime:
    agent: AgentLoop
    lock: asyncio.Lock
    refcount: int = 0          # _acquire 同步 +1, finally -1
    last_used_ms: int = 0
    evict_pending: bool = False

class ConversationService:
    _runtimes: OrderedDict[str, _SessionRuntime]   # 按 last_used 排序, 供 LRU
```

**refcount 协议（单线程 asyncio 下无锁竞态）**

1. `_acquire(key)`：同步取/建 runtime → `refcount += 1` → 更新 `last_used_ms`；
2. `handle_message`：`try: async with runtime.lock: ... finally: self._release(key, runtime)`；
3. `_release`：`refcount -= 1`；若为 0 且 `evict_pending` → 立即淘汰；
   若缓存数超上限 → 从最久未使用、`refcount == 0` 的 runtime 开始淘汰；
4. **淘汰永远不会发生在 `refcount > 0`（持锁/排队/执行中）的 runtime 上**，因此不会出现"锁被替换"的并发窗口。

**新增 API**

```python
async def close_session(self, key: SessionKey) -> bool:
    """释放指定会话运行时; busy 时标记 pending, 由 _release 完成. 返回是否已释放."""

async def evict_idle(self, ttl_seconds: float | None = None) -> int:
    """按 last_used_ms 淘汰空闲 runtime; ttl 为 None 时使用构造参数."""

def stats(self) -> dict[str, Any]: ...   # 见 3.1
```

**构造参数与配置**

- `max_cached_sessions: int = 256`（<=0 视为不限制）；
- `cache_idle_seconds: float = 0`（0 = 不自动 TTL；仅显式 `evict_idle` + 容量 LRU）。

**挂钩子（谁触发释放）**

| 时点 | 调用 |
|---|---|
| `archive_session()` / `purge_session()` 成功后 | `close_session(key)`（跨渠道 /clear / purge 自动生效） |
| `QqPolicy._rotate()` 归档旧会话后 | `close_session(old_session)`（空会话也能释放） |
| `CliPolicy.new_session()`（/new、/clear 切新） | `close_session(old_session)` |
| `ConversationService.close()` | 清空全部 runtime |

> 淘汰只丢 `AgentLoop` 实例（工具防爆滑窗 + 压缩器滚动摘要缓存）；JSONL 事实源不变；
> 代价是下次触发上下文压缩时可能重摘一次，正确性不受影响。

**兼容**：`tests/test_conversation.py` 中直接访问 `service._agents[...]` 的用例改为 `stats()` 或
`_runtimes`；`ConversationService.close()` 语义不变。

### 3.5 A5：配置、集成、文档与验收

- 配置键（`config.py` / `.env.example` / README）：
  - `memory_max_cached_sessions=256`
  - `memory_cache_idle_seconds=0`
- `bootstrap.build_application()` 透传到 `ConversationService`；
- `docs/GATEWAY_DESIGN.md` ADR-1 §15.8/§16.5 标注 A+ 已落地（实现后），本文追加"实现状态"；
- 复测 §2.1 基准并记录新版；全量回归；真实 CLI/QQ 冒烟各一次。

---

## 4. 测试策略

| 层 | 用例 |
|---|---|
| `metrics` | 分位计算/空样本/有界 deque/快照字段 |
| 尾读工具 | 空文件/仅 header/无末尾换行/末尾损坏行/中间损坏行/空行/UTF-8 跨块边界/超大块 |
| `get_meta` | 与全量路径元数据一致性；损坏文件；archive 回退 |
| `append_turn` | 半行恢复后 seq 连续；并发双实例；消息计数 |
| `list_sessions` | 多文件/归档优先/与旧实现结果一致 |
| `load_recent` | **差分测试**：随机文件（含损坏/半行/中文/工具调用）尾读 == 全量+裁剪 |
| 生命周期 | `close_session`（idle/busy）、`evict_idle`、LRU 上限、归档与在途并发、refcount 不变量 |
| 回归 | 现有 1037 用例全绿；真实 CLI/QQ 冒烟 |

---

## 5. 阶段排期与依赖

| 阶段 | 内容 | 依赖 | 建议工期 | 并行 |
|---|---|---|---|---|
| A1 | metrics + `ConversationService.stats()` + 基准脚本 | — | 0.5d | 否（先取基线） |
| A2 | 尾读工具 + `get_meta/append_turn/list_sessions` 改造 | A1 | 1.5–2d | 可与 A4 并行 |
| A3 | `load_recent` 尾读 + 窗口裁剪 + 差分测试 | A2 | 1–1.5d | 可与 A4 并行 |
| A4 | `_SessionRuntime` + refcount + 生命周期 API + 钩子 | A1 | 1.5–2d | 可与 A2/A3 并行 |
| A5 | 配置/文档/基准复测/真实冒烟 | A3+A4 | 1d | 否 |

- 单线程推进：约 **5.5–7 个理想人日**；A2/A3 与 A4 双线并行：约 **4–5 天**。
- 每阶段独立提交；A2/A3 的基准数据写入本文"实现记录"。

---

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| UTF-8 多字节被块边界截断 | 只解码完整行；解码异常回退全量路径 |
| 崩溃半行/末尾损坏行 | 反向扫描跳过无效行；追加补换行；现有测试覆盖 |
| 读期间跨进程追加/归档 | 只读快照，不回写；`FileNotFoundError` 视为空会话；文件锁仅保护写 |
| 中间行损坏导致计数口径变化 | A+1 已确认：`seq/message_total` 为累计口径，文档注明；正常路径无差异 |
| 淘汰与在途 turn 竞态 | refcount 同步增减 + 仅 `refcount==0` 淘汰；归档在途场景测试 |
| 淘汰导致压缩摘要缓存丢失 | 正确性不受影响；日志记录 `evictions`，必要时调大 `max_cached_sessions` |
| 指标开销 | `perf_counter` 纳秒级；有界 deque；默认不打印 |
| 测试依赖机器性能 | 性能只在基准脚本与人工验收，不做 CI 硬阈值 |

---

## 7. 验收清单

- [ ] A1：`stats()` 可用；基准脚本输出与 §2.1 一致；
- [ ] A2：`get_meta/append_turn/list_sessions` 尾读路径通过全部契约与崩溃恢复用例；
- [ ] A3：2000 轮 `load_recent` p95 ≤3ms 且平坦；差分测试通过；
- [ ] A4：`close_session/evict_idle/max_cached_sessions` 可用；归档/轮换/关停可释放；无并发回归；
- [ ] A5：配置/README/ADR 同步；全量回归 + 真实 CLI/QQ 冒烟通过；
- [ ] 更新 `docs/GATEWAY_DESIGN.md` ADR-1 §15.8/§16.5 与本文"实现记录"。

---

## 8. 实施状态（待更新）

| 阶段 | 状态 | 提交 | 备注 |
|---|---|---|---|
| A1 | 待实现 | — | — |
| A2 | 待实现 | — | — |
| A3 | 待实现 | — | — |
| A4 | 待实现 | — | — |
| A5 | 待实现 | — | — |
