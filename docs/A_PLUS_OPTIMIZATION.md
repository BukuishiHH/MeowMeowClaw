# MeowMeowClaw A+ 优化设计：会话 IO 尾读与运行时生命周期（v1）

> 状态：**A1–A4 已完成（2026-10-12），A5 待实现**。本文只定义 A+ 的行为、接口、阶段与验收，
> **不改 JSONL 文件格式、不改 AgentLoop/压缩语义**；实现按 §5 阶段推进，记录见 §8.1–§8.4。
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

- [x] A1：`stats()` 可用；基准脚本输出与 §2.1 一致（实测偏差 ≤11%，见 §8.1）；
- [x] A2：`get_meta/append_turn/list_sessions` 尾读路径通过全部契约与崩溃恢复用例（实测 O(1)，见 §8.2）；
- [x] A3：2000 轮 `load_recent` p95 ≤3ms 且平坦；差分测试通过（实测 p95 1.57ms、50→2000 差值 0.09ms，见 §8.3）；
- [x] A4：`close_session/evict_idle/max_cached_sessions` 可用；归档/轮换/关停可释放；无并发回归（实测生命周期用例全绿，见 §8.4）；
- [ ] A5：配置/README/ADR 同步；全量回归 + 真实 CLI/QQ 冒烟通过；
- [ ] 更新 `docs/GATEWAY_DESIGN.md` ADR-1 §15.8/§16.5 与本文"实现记录"。

---

## 8. 实施状态（待更新）

| 阶段 | 状态 | 提交 | 备注 |
|---|---|---|---|
| A1 | 已完成 | `4f4d0bc` | metrics + `stats()` + 基准脚本；新增 28 用例；实测偏差 ≤11% |
| A2 | 已完成 | `2fd8cd1` | 尾读工具 + 三路径改造；新增 37 用例；2000 轮 `get_meta` p95 0.53ms |
| A3 | 已完成 | `bf90797` | `load_recent` 反向块读 + 窗口裁剪；新增 212 用例；2000 轮 p95 1.57ms、50→2000 差值 0.09ms |
| A4 | 已完成 | 待回填 | `_SessionRuntime` + refcount + 生命周期 API；新增 22 用例；归档/轮换/关停均释放 |
| A5 | 待实现 | — | 依赖 A3+A4 |

### 8.1 A1 实现记录（2026-10-12）

**交付物**

| 文件 | 变更 |
|---|---|
| `meowmeowclaw/memory/metrics.py` | 新增：`LatencyStats`（有界样本 count/last/avg/p50/p95，最近秩分位）、`StoreMetrics`（三项主路径）、`measure()` 计时上下文管理器 |
| `meowmeowclaw/memory/jsonl.py` | `load_recent / get_meta / append_turn` 拆为"公开计时 wrapper + 私有实现"；新增 `stats()`（指标快照 + `active_file_count` + `last_file_size_bytes`） |
| `meowmeowclaw/memory/__init__.py` | 导出 `LatencyStats / StoreMetrics / DEFAULT_MAX_SAMPLES` |
| `meowmeowclaw/conversation.py` | 新增 `ConversationService.stats()`（`cached_agents / locks / evictions / store / rss_mb`）与 `current_rss_mb()` |
| `scripts/bench_session_store.py` | 新增：50/200/500/1000/2000 轮基准脚本（生成 + 三项主路径 avg/p95，输出与 §2.1 对照），不进 CI |
| `tests/memory/test_metrics.py` / `test_jsonl_store_stats.py` / `tests/test_conversation.py` | 新增 28 用例（分位/有界/异常也记录/stats 契约/观测降级） |

**基准复测（本机 WSL2 / Python 3.11 / 20 次采样）**

| 轮数 | 文件 | `load_recent` avg/p95 (ms) | `get_meta` avg/p95 | `append_turn` avg/p95 | §2.1 基线 avg/p95 | 偏差 |
|---:|---:|---:|---:|---:|---:|---:|
| 50 | 0.36 MB | 1.31 / 1.67 | 1.00 / 1.28 | 1.77 / 2.08 | 1.32 / 1.64 | **-1%** |
| 200 | 1.42 MB | 4.15 / 5.00 | 3.23 / 3.92 | 4.21 / 4.71 | 3.99 / 4.40 | +4% |
| 500 | 3.56 MB | 9.21 / 10.18 | 7.20 / 7.79 | 8.72 / 10.82 | 9.39 / 10.53 | -2% |
| 1000 | 7.12 MB | 17.96 / 19.37 | 14.89 / 17.23 | 15.56 / 17.85 | 18.12 / 20.70 | -1% |
| 2000 | 14.23 MB | 35.64 / 42.09 | 27.74 / 29.13 | 31.70 / 39.55 | 39.83 / 44.64 | **-11%** |

- **结论**：全部落在 §2.1 基线的 ±20% 内，且再次印证 A+ 的目标 G1 —— `load_recent` 成本随总轮数线性增长
  （50 轮 1.31ms → 2000 轮 35.64ms），A2/A3 之后应降到"与总长度解耦"；
- 复现命令：`python scripts/bench_session_store.py`（可用 `--sizes/--samples/--keep` 调整）；
- 观测入口：`JsonlSessionStore.stats()` 与 `ConversationService.stats()`（纯观测，取值失败自动降级为空/None）；
- **行为零变化**：A1 只加计时与快照，未改任何读写路径、schema 与语义；全量回归通过。

### 8.2 A2 实现记录（2026-10-12）

**交付物**

| 文件 | 变更 |
|---|---|
| `meowmeowclaw/memory/tail.py`（新增） | 反向块读工具：`read_header` / `read_last_lines(max_lines,max_bytes)` / `read_last_valid_turn`；内部 `_iter_last_lines` 惰性生成器（命中即停） |
| `meowmeowclaw/memory/jsonl.py` | 新增 `_read_tail_state()`（首行 header + 末条有效 turn；异常回退全量）；`get_meta / append_turn / list_sessions` 改走尾读 |
| `meowmeowclaw/memory/__init__.py` | 导出三个尾读函数 |
| `scripts/bench_session_store.py` | 增加 `list_sessions` 采样列（A2 验收项） |
| `tests/memory/test_tail.py` / `test_jsonl_tail_path.py` | 新增 37 用例（27 尾读工具 + 10 存储层：打桩证明不走全量、回退路径、A+1 口径、归档回退） |

**实现要点**

1. 二进制反向块读（64KB/块），按 `b"\n"` 切分后**只在整行上解码** → 不会截断 UTF-8 多字节；
2. 末尾无换行的那一段作为**候选行**（写全缺尾换行 / 崩溃半行都交给 JSON 解析判定），
   与全量路径行为逐字节一致（§4 的差分约束）；
3. `read_last_valid_turn` 用惰性生成器**命中即停** → 正常文件只读末尾一个块，与文件总长度无关
   （首版实现忘了这点，被基准暴露：`get_meta` 仍是 O(文件)，已修复）；
4. 回退条件（保持旧行为）：文件不存在、首行不是合法 header、末轮缺 `seq/message_total`、
   `OSError/UnicodeDecodeError/ValueError`；
5. A+1 口径：`turn_count = last_turn["seq"]`、`message_count = last_turn["message_total"]`
   （正常文件与全量扫描一致；中间行损坏时为累计口径，已在文档与用例中注明）。

**基准复测（同机、20 次采样；A1 = A2 前，A2 = 本次）**

| 轮数 | `get_meta` avg/p95 (ms) | `append_turn` avg/p95 (ms) | `list_sessions` avg/p95 (ms) | `load_recent` avg/p95 (ms) |
|---:|---:|---:|---:|---:|
| 50 | 1.00/1.28 → **0.37/0.52** | 1.77/2.08 → **0.91/1.16** | 0.90/1.22 → **0.35/0.43** | 1.31/1.67 → 1.34/1.92（A3 待改） |
| 200 | 3.23/3.92 → **0.39/0.62** | 4.21/4.71 → **0.94/1.11** | 1.91/2.20 → **0.37/0.48** | 4.15/5.00 → 4.12/5.00 |
| 500 | 7.20/7.79 → **0.36/0.43** | 8.72/10.82 → **0.91/1.26** | 5.86/7.15 → **0.33/0.39** | 9.21/10.18 → 9.55/11.64 |
| 1000 | 14.89/17.23 → **0.41/0.55** | 15.56/17.85 → **0.92/1.03** | 7.33/8.00 → **0.28/0.37** | 17.96/19.37 → 22.51/32.98（机器波动） |
| 2000 | 27.74/29.13 → **0.48/0.53** | 31.70/39.55 → **0.96/1.19** | 14.21/15.41 → **0.34/0.44** | 35.64/42.09 → 35.27/41.80 |

- **G2（`get_meta`/`append_turn` O(1)，p95 ≤ ~1ms）达标**：2000 轮 p95 分别为 0.53ms / 1.19ms；
- **G3（`list_sessions` 不再全量扫描）达标**：2000 轮 p95 0.44ms；
- 顺带收益：批量生成（`append_turn` × 2000）从 30.9s 降到 2.1s；
- `load_recent` 本轮未改（35.27ms @2000，仍 O(总长度)）→ **A3 的目标**；
  1000 轮那一行的偏高属同机波动（同一代码路径两次采样 17.96ms / 22.51ms）。

### 8.3 A3 实现记录（2026-10-12）

**交付物**

| 文件 | 变更 |
|---|---|
| `meowmeowclaw/memory/jsonl.py` | 新增 `_read_turns_tail()`（反向块读 + 字节预算按需翻倍 + 窗口命中即停）；`load_recent` 改走尾读，异常回退 `_read_turns` 全量路径；空窗口（0）直接返回 |
| `scripts/bench_session_store.py` | 新增 50→2000 轮 `load_recent p95` 平坦性检查；A3 后基线偏差为负时判定为"优于 §2.1 基线（提速）"，不再误报 |
| `tests/memory/test_jsonl_load_recent_tail.py`（新增） | 212 用例：200 组随机差分（每组 10 个窗口组合）＋ 12 个尾读/回退/预算/边界用例 |

**实现要点**

1. 复用 A2 反向块读从文件尾向前读；正常窗口在命中 `max_turns`、或"累计字符 > max_chars"（至少保留最新 1 轮）时立即停止，成本 O(窗口)，与文件总长度解耦；
2. 初始字节预算 `max_chars * 4 + 64KB`（UTF-8 最坏 4 字节/字符）；预算内凑不齐窗口时翻倍重读，直到满足窗口或**确认到达文件头**（`read_last_lines` 实际返回字节数 < 当前预算）；`max_chars=None` 时从 64KB 起步同样翻倍；
3. 尾读结果是全量 `_read_turns` 结果的**后缀**：累计字符已超上限那一轮之后，更旧轮必被 `_trim_by_chars` 丢弃，因此继续调用原裁剪函数即可保证与旧路径逐字节一致；不切断 turn、损坏行/半行跳过、`max_chars` 至少保留最新一轮等语义不变；
4. 崩溃半行/损坏行/非 dict JSON 由 JSON 解析自然跳过；`max_turns=0 / max_chars=0` 不触碰文件；`OSError / UnicodeDecodeError / ValueError` 回退全量路径（非法 UTF-8 时两条路径最终都抛 `UnicodeDecodeError`，与旧行为一致）；
5. **差分测试**：200 组随机文件 × 10 个窗口组合（含损坏行、非 dict JSON、空行、半行、中文、工具调用、无尾换行、超大行），尾读结果与"全量读取 + 相同 max_turns/max_chars 裁剪"逐消息一致。

**基准复测（同机、20 次采样；A2 = A3 前，A3 = 本次；窗口 `max_turns=50 / max_chars=120000`）**

| 轮数 | 文件 | `load_recent` A2 avg/p95 (ms) | `load_recent` A3 avg/p95 (ms) | p95 改进 |
|---:|---:|---:|---:|---:|
| 50 | 0.36 MB | 1.34 / 1.92 | **1.27 / 1.66** | 基本持平（文件小于初始预算） |
| 200 | 1.42 MB | 4.12 / 5.00 | **1.77 / 2.44** | ~2.0x |
| 500 | 3.56 MB | 9.55 / 11.64 | **1.72 / 2.43** | ~4.8x |
| 1000 | 7.12 MB | 22.51 / 32.98 * | **1.54 / 1.82** | ~10.6x（对比另一采样 19.37ms） |
| 2000 | 14.23 MB | 35.27 / 41.80 | **1.33 / 1.57** | ~26.6x |

\* A2 的 1000 轮行有同机波动（同一代码路径另一次采样为 17.96 / 19.37ms），仅作粗略对照。

- **G1 达标**：2000 轮 `load_recent` p95 1.57ms（≤3ms）；50→2000 轮 p95 差值 0.09ms、avg 差值 0.06ms（验收 ≤2ms），文件大小增长已不影响窗口读取；
- 全量回归 **1521 passed / 6 skipped / 2 xfailed**；`ruff check .` 通过；既有崩溃恢复、双实例并发、归档/删除用例全绿；
- 复现命令：`python scripts/bench_session_store.py`（可用 `--sizes/--samples/--keep` 调整）。

### 8.4 A4 实现记录（2026-10-12）

**交付物**

| 文件 | 变更 |
|---|---|
| `meowmeowclaw/conversation.py` | 新增 `_SessionRuntime`（agent + lock + refcount + last_used_ms + evict_pending）与 `OrderedDict` LRU；`_acquire / _release / _evict_runtime / _enforce_capacity / _sweep_expired`；新增 `close_session / evict_idle`；`archive_session / purge_session / close` 同步释放运行时；`stats()` 改按运行时统计 |
| `meowmeowclaw/channels/cli_policy.py` | `new_session()` 改为 async，切走旧会话时 `close_session(old)` |
| `meowmeowclaw/channels/qq_policy.py` | `_rotate()` 创建/激活新会话后 `close_session(old)`（空会话也能释放） |
| `meowmeowclaw/cli.py` | 网关关闭时的 legacy 路径：`/new`、`/clear` 切新前释放当前运行时（与 CliPolicy 对齐） |
| `tests/test_conversation.py` | 新增 16 个生命周期用例（idle/busy close、refcount 排队、evict_idle、自动 TTL、LRU 容量、归档/删除/在途归档、在途 close、无限容量、非法 key） |
| `tests/channels/test_cli_policy.py` / `test_qq_policy.py` | 新增 6 个渠道钩子用例（`/new`、`/clear`、QQ 超时轮换均释放运行时） |
| `tests/test_bootstrap.py` / `test_cli.py` | 适配 `_runtimes`/`_acquire` 内部访问，并断言 `/new`、`/clear` 释放旧运行时 |

**实现要点**

1. **agent + lock 同生命周期**：每个 `_SessionRuntime` 持有一把锁，淘汰整个 runtime，不再存在"锁被替换但旧 agent 仍在执行"的并发窗口；
2. **refcount 协议**：`_acquire()` 同步 +1（含排队任务），`handle_message` 的 `try/finally` 保证 `_release()` -1；淘汰只处理 `refcount == 0` 的运行时；
3. **`close_session()`**：空闲立即淘汰并返回 `True`；在途置 `evict_pending` 并返回 `False`，由最后一次 `_release` 完成淘汰（幂等，运行时不存在返回 `False`）；
4. **LRU 容量**：`_runtimes` 按 `last_used` 排序（`_acquire` 时 `move_to_end`），`_enforce_capacity` 在 acquire/release 两处按"最久未使用且空闲"淘汰；`max_cached_sessions <= 0` = 不限制；
5. **TTL**：`cache_idle_seconds > 0` 时在 acquire/release 顺带清理过期空闲运行时；命中的旧运行时若已过期则先淘汰重建；默认 `0` 关闭自动 TTL，显式 `evict_idle(ttl<=0)` 清理全部空闲；
6. **钩子**：`archive_session / purge_session / ConversationService.close`、`CliPolicy.new_session`、`QqPolicy._rotate`、legacy CLI `/new`/`/clear` 均触发释放；`_evictions` 随实际淘汰递增并出现在 `stats()`；
7. **兼容**：`stats()` 字段形状不变（`cached_agents / locks` 取自运行时数量，默认每条 runtime 一把锁）；全量回归、同会话串行与在途场景无回归。

**验证**

- 全量回归 **1543 passed / 6 skipped / 2 xfailed**；`ruff check .` 通过；
- 生命周期用例覆盖：`close_session`（idle/busy/重复）、refcount 排队不变量、`evict_idle`（显式 0/TTL/在途跳过）、自动 TTL、LRU 容量与无限容量、归档/删除/在途归档/关停释放、非法输入；
- 渠道钩子用例覆盖 CLI `/new`/`/clear`、QQ `/new`/`/clear`/超时轮换，切走后 `cached_agents` 归零；
- A5 待办：`memory_max_cached_sessions` / `memory_cache_idle_seconds` 配置键透传 bootstrap、README/ADR 同步与真实 CLI/QQ 冒烟。
