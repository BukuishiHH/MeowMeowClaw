# MeowMeowClaw Git MCP Server 设计（v1）

> 状态：**评审通过，GM1–GM6 已实现，v0.1 收尾（2026-10-13）**。本文定义 Git MCP Server 的协议形态、工具清单、安全策略、
> Cline 接入方式与里程碑计划；**不修改 MeowMeowClaw 主项目现有行为**。
> 按 §10 里程碑（GM1–GM6）逐步实现，每阶段独立提交；实施状态与实现记录见 §14。
> 关联：`README.md`、`docs/SUBAGENT_DESIGN.md`（workspace 变更锁/子代理工作区）、
> `meowmeowclaw/memory/filelock.py`、`pyproject.toml`。

---

## 0. 背景与目标

### 0.1 背景

Cline 等 MCP（Model Context Protocol）客户端可以通过 stdio / HTTP 连接外部 MCP Server，获得
Tools / Resources / Prompts 能力。当前 MeowMeowClaw 主项目提供了 `read_file`/`write_file`/`exec`
等通用工具，但没有专门的 Git 语义层：Git 操作只能通过 `exec` 拼 shell 命令，既不安全也不结构化。

本设计将 Git 能力从主 Agent 中解耦为**独立的 Git MCP Server**：

- 对 Cline：提供一个可配置、可审计、默认只读的 Git 工具服务；
- 对 MeowMeowClaw：未来可作为 MCP Client 消费同一服务，或复用其 workspace 锁与安全策略；
- 对用户：以 `git` CLI 为唯一执行后端，保持与本地 `.gitconfig`、hooks、credential helper 兼容。

### 0.2 目标

| # | 目标 | 验收指标 |
|---|---|---|
| G1 | 提供完整、结构化的 Git 工具集 | Cline 可通过 Tools 完成状态/历史/差异/提交/分支/暂存等常用操作 |
| G2 | 默认安全 | 只读默认开放；写操作显式开关；危险/破坏性操作默认不暴露 |
| G3 | 输出可被模型消费 | 统一 JSON 返回、结构化字段、错误码、输出截断与上下文预算 |
| G4 | 与 Cline 集成顺畅 | `cline_mcp_settings.json` 可直接接 stdio；只读工具可 autoApprove |
| G5 | 并发可控 | 每仓库串行化写操作；可与 MeowMeowClaw workspace 变更锁协同 |
| G6 | 可测试、可回归 | 本地临时 git 仓库单测；不依赖网络；ruff + pytest 通过 |
| G7 | 不污染主项目 | MCP 依赖与 Git Server 独立；主项目不引入 MCP client/server 运行时依赖 |

### 0.3 非目标（v1 明确不做）

- 不做 Git Web UI / PR 管理 / Issue 管理；不做 GitHub/GitLab API 客户端；
- 不做任意 shell 命令执行或任意 `git` 参数透传；
- 不做自动冲突解决、自动 rebase 策略、自动 push/rebase 的高风险链路；
- 不做远程 MCP HTTP 服务（v1 以 stdio 为准，HTTP 预留优化项）；
- 不把 Git MCP Server 内置进 `AgentLoop` 的工具注册表（后续如需，以 MCP Client 方式接入）；
- 不替代 MeowMeowClaw 现有 `exec`/filesystem 安全模型；两者是独立工具面。

---

## 1. 已确认决策与默认假设

| # | 决策项 | 默认结论 |
|---|---|---|
| D1 | 服务边界 | 独立包/模块（建议 `meowmeowclaw-mcp-git`），不并入主 Agent |
| D2 | 实现语言 | Python + 官方 `mcp` SDK（stdio transport） |
| D3 | Git 后端 | 调用 `git` CLI 子进程；参数以列表传递，不经 shell |
| D4 | 默认权限 | `read_only=true`，写/远端/破坏性各自独立开关 |
| D5 | 仓库模型 | 支持多仓库 **alias allowlist**；工具只接收 alias/repo_id，不接受任意绝对路径 |
| D6 | Cline 集成 | 只读工具可 autoApprove；写操作由 Cline 用户逐次确认 |
| D7 | v1 远端 | 仅 `git_fetch` + 只读 remote 列表；push 默认不提供 |
| D8 | 返回协议 | 统一 `{ok,data,meta}`；结构化 + 原始片段；错误码可编程处理 |
| D9 | 并发 | 每 repo 一把锁；写操作串行；预留与 workspace 变更锁协同 |
| D10 | 文档位置 | 本文；实现阶段在 README/`.env.example` 补接入说明 |

> 以上为默认假设，评审时可逐项调整；本文其余部分按该默认编写。

---

## 2. 总体架构

### 2.1 部署形态

```
┌──────────────┐        JSON-RPC over stdio        ┌───────────────────────┐
│    Cline     │  ───────────────────────────────▶ │  Git MCP Server       │
│  (MCP Host)  │  ◀─────────────────────────────── │  (Python, stdio)      │
└──────────────┘        tools/resources/prompts    └───────────┬───────────┘
                                                                │ subprocess args
                                                                ▼
                                                      ┌───────────────────┐
                                                      │   git CLI         │
                                                      │  (本地仓库/配置)   │
                                                      └───────────────────┘
```

- Cline 作为 MCP Host 启动 Server 进程，维护会话与确认 UI；
- Server 不监听网络端口（v1），所有 Git 操作在本机允许根内执行；
- Server 的 stdout 只用于 MCP 协议消息，日志一律写 stderr，避免污染协议流。

### 2.2 组件职责

| 组件 | 职责 | 不负责 |
|---|---|---|
| `StdioServer` / MCP 适配层 | 初始化 MCP、注册 Tools/Resources/Prompts、协议错误映射 | Git 业务逻辑 |
| `RepoRegistry` | alias → 绝对路径映射、允许根校验、is_git_repo 检查 | Git 操作 |
| `PolicyEngine` | 只读/写/远端/破坏性开关、分支保护、路径与参数白名单 | 命令执行 |
| `GitRunner` | 安全调用 `git` CLI、超时、输出截断、环境清洗、错误解析 | 结构化解析 |
| `GitParsers` | `status --porcelain=v2`、`log`、`diff`、`show` 等结构化解析 | 策略 |
| `ToolHandlers` | 每个 MCP 工具的参数校验、策略检查、调用 Runner/解析器 | 协议生命周期 |
| `LockManager` | 每 repo 进程内锁 + 可选跨进程锁 | 业务 |
| `AuditSink` | 可选 JSONL 审计：工具、repo、参数摘要、结果、耗时 | 业务决策 |

### 2.3 目录结构（GM1–GM2 已落地部分）

```
meowmeowclaw_mcp_git/      # 顶层独立包(不并入主 Agent; 主项目运行时不依赖 mcp)
├── __init__.py            # 版本与分层说明(import 无副作用)
├── __main__.py            # python -m ... 入口(缺 SDK 时 stderr 提示 + 退出码 3)
├── app.py                 # 协议无关核心: 装配 + 权限矩阵 + 统一信封 + 派发 + 信封兜底
├── server.py              # MCP stdio 适配(唯一 import mcp 的模块)
├── config.py              # 启动参数与环境变量(CLI > env > 默认)
├── registry.py            # RepoRegistry(alias/允许根/工作树顶层/路径校验)
├── runner.py              # GitRunner(参数列表/环境清洗/硬超时/有界流式截断)
├── prompts.py             # MCP Prompts(提交信息/评审/分支汇总/冲突解释)
├── resources.py           # MCP Resources(git://<repo>/status|head|log|file)
├── policy.py              # PolicyEngine(只读开关/保护分支/dirty/删除保护)
├── locks.py               # RepoLockManager(每 repo asyncio.Lock + 可选跨进程锁)
├── budget.py              # 文本/列表输出预算(字节/行裁剪 + truncated)
├── validation.py          # 工具参数校验(JSON Schema 子集)
├── envelope.py            # {ok,data,meta} / {ok,error_code,message,meta}
├── errors.py              # 错误码与异常类型
├── parsers/
│   ├── status.py          # git status porcelain v2
│   ├── diff.py            # diff/show --numstat -z
│   ├── log.py             # log/search 记录(US/RS 分隔)
│   ├── refs.py            # for-each-ref / tag / remote + URL 脱敏
│   ├── grep.py            # git grep -z
│   └── blame.py           # blame --line-porcelain
└── tools/
    ├── base.py            # ToolSpec / ToolContext / ToolOutcome
    ├── common.py          # git 调用/错误映射/meta/路径与自由参数防护
    ├── read.py            # status/diff/log/show/blame/file_at_revision
    ├── refs.py            # list_branches/list_tags/list_remotes/rev_parse/config_get
    ├── search.py          # grep/search_history
    ├── write.py           # stage/unstage/restore/commit/branch_create/branch_switch/branch_delete/stash
    └── remote.py          # git_fetch(仅 --allow-remote=true 注册)
```

> GM6 新增 `audit.py`; 目录按里程碑增量演进。

### 2.4 与 MeowMeowClaw 主项目的关系

- v1 **不修改** `meowmeowclaw/` 现有模块；
- 未来两种协同方式：
  1. Git MCP Server 作为 MeowMeowClaw 的 MCP Client 后端，提供 Git 工具；
  2. 与 `docs/SUBAGENT_DESIGN.md` 的 workspace 变更锁共用同一把 lock 文件，
     让 Cline 的 Git 写操作与 MeowMeowClaw 子代理的 write/exec 不互相踩踏；
- 上述协同不在 GM1–GM6 首版范围内，作为 §12 优化项。

---

## 3. 配置与仓库策略

### 3.1 启动参数与环境变量

| 配置 | 默认 | 说明 |
|---|---|---|
| `--root PATH` / `MCP_GIT_ROOT` | 当前目录 | 单仓库模式根目录 |
| `--roots PATH1:PATH2` / `MCP_GIT_ROOTS` | 空 | 多根模式允许根（按平台注入分隔符） |
| `--repos alias=path,...` | 空 | 显式 alias；未配置时用根目录 basename 作为 alias |
| `--read-only {true,false}` | `true` | false 时允许写工具（仍需逐工具确认） |
| `--allow-remote {true,false}` | `false` | 是否暴露 fetch/remote 写（fetch 本身只读远端状态） |
| `--allow-destructive {true,false}` | `false` | reset --hard/clean/push --force 等，v1 即使 true 也不实现 |
| `--protected-branches main,master` | `main,master` | 禁止直接 commit/push 的分支名，可配置 |
| `--timeout SECONDS` | `30` | 单条 git 命令超时 |
| `--max-output-bytes` | `131072` | 单工具输出上限（超出截断并标记） |
| `--audit-log PATH` | 空 | 可选审计 JSONL 路径 |
| `--lock-dir PATH` | 空 | 跨进程锁目录；与 MeowMeowClaw workspace 锁共用时填写同一路径 |

### 3.2 RepoRegistry 规则

- 允许根统一 `realpath` 规范化；
- 所有 repo 路径必须落在允许根内，且通过 `git rev-parse --is-inside-work-tree`；
- alias 唯一，大小写敏感策略由配置决定；
- 工具调用只接受 `repo`（alias）与仓库内相对路径，禁止绝对路径、`..`、工作区外路径；
- worktree/submodule 额外校验（见 §6.5）。

GM1 实现补充：

- repo 路径必须是 git 工作树**顶层**（`git rev-parse --show-toplevel` 的 realpath 与配置路径一致）；
  指向仓库子目录或裸仓库时启动即拒绝，避免 `git -C <subdir>` 把作用域扩大到父仓库；
- 只配置 `--repos`（无 `--root` / `--roots`）时，每个 repo 路径自身作为允许根；
- alias 精确匹配（大小写敏感），大小写策略开关留待后续里程碑；
- `resolve_path` 拒绝绝对路径、任何 `..` 段（即使最终不越界）以及 realpath 后逃逸出仓库的符号链接。

### 3.3 安全等级

每个工具注册时标记 `read | write | remote | destructive` 等级：

- `read`：只读查询，可 autoApprove；
- `write`：修改工作区/index/本地历史，Cline 需逐次确认；
- `remote`：访问远端（fetch/pull/push），默认不暴露或需显式确认；
- `destructive`：不可逆或高风险，v1 不实现。

---

## 4. MCP 能力设计

### 4.1 Tools 总览

#### A. 只读/分析类（默认可用，可 autoApprove）

| 工具 | 作用 | 关键参数 | 备注 |
|---|---|---|---|
| `git_status` | 工作区/暂存区/分支状态 | `repo`、`short` | porcelain v2 结构化；**GM1 已实现** |
| `git_diff` | 工作区/暂存/commit/range 差异 | `repo`、`mode`、`base`、`head`、`paths`、`stat_only`、`context_lines` | 默认限制输出；**GM2 已实现** |
| `git_log` | 提交历史 | `repo`、`max_count`、`author`、`since`、`until`、`path`、`grep` | 默认 20 条；**GM2 已实现** |
| `git_show` | 单个 commit/tag/对象 | `repo`、`ref`、`stat_only` | 限制大小；**GM2 已实现** |
| `git_blame` | 行级 blame | `repo`、`file`、`start_line`、`end_line` | 大文件需范围；**GM2 已实现** |
| `git_file_at_revision` | 指定 ref 的文件内容 | `repo`、`ref`、`path` | 返回文本+编码/大小；**GM2 已实现** |
| `git_list_branches` | 分支列表 | `repo`、`remote`、`verbose` | 含 tracking；**GM2 已实现** |
| `git_list_tags` | tag 列表 | `repo`、`pattern` | —；**GM2 已实现** |
| `git_list_remotes` | remote 列表 | `repo` | URL 脱敏；**GM2 已实现** |
| `git_grep` | 仓库内文本搜索 | `repo`、`pattern`、`paths`、`ref` | 正则/大小写选项；**GM2 已实现** |
| `git_search_history` | 历史 diff 搜索 | `repo`、`pattern`、`pickaxe`、`max_count` | `-S`/`-G`；**GM2 已实现** |
| `git_rev_parse` | ref 解析 | `repo`、`ref`、`short` | —；**GM2 已实现** |
| `git_config_get` | 读取配置 | `repo`、`key` | 只读；**GM2 已实现** |

> GM1–GM2 已注册全部 A 组只读工具。`short=true` 只返回分支信息与各组计数、省略逐文件列表，
> 用于大工作区的上下文预算；`short=false`（默认）返回 staged/unstaged/untracked/conflicts/
> renames 结构化路径列表。

#### B. 安全写类（默认不 autoApprove；需 `read_only=false`）

| 工具 | 作用 | 关键参数 | 安全约束 |
|---|---|---|---|
| `git_stage` | 暂存文件 | `repo`、`paths`、`all` | 路径必须在 repo 内；**GM3 已实现** |
| `git_unstage` | 取消暂存 | `repo`、`paths` | —；**GM3 已实现** |
| `git_restore` | 恢复工作区/暂存区 | `repo`、`paths`、`staged`、`force` | dirty 检查；默认拒绝覆盖；**GM3 已实现** |
| `git_commit` | 创建提交 | `repo`、`message`、`paths`、`amend`、`signoff`、`allow_empty` | 禁止空 message；保护分支策略；**GM3 已实现** |
| `git_branch_create` | 创建分支 | `repo`、`name`、`start_point`、`switch` | 名字校验；**GM3 已实现** |
| `git_branch_switch` | 切换分支 | `repo`、`name`、`force` | 默认 dirty 拒绝；**GM3 已实现** |
| `git_branch_delete` | 删除分支 | `repo`、`name`、`force` | 默认拒绝未合并/当前分支；**GM3 已实现** |
| `git_stash` | stash 管理 | `repo`、`action(list/push/pop/apply/drop)`、`message`、`paths` | drop 需显式确认；**GM3 已实现** |
| `git_fetch` | 拉取远端对象 | `repo`、`remote`、`prune` | 默认不合并/rebase；**GM4 已实现**；忽略 `<remote>/HEAD` 符号引用 |

> 写工具默认不注册（`--read-only=true` 时 `tools/list` 只有 13 个只读工具）；
> `--read-only=false` 后出现上述 8 个写工具，Cline 应保持逐次确认（`autoApprove` 只放只读工具）。
> `git_fetch` 由 `--allow-remote=true` 单独开启（与 read_only 无关），默认不注册。
> 除 `git_stash action=list` 外，写/fetch 操作都会在仓库锁内执行并返回 `before`/`after` 状态摘要。

#### C. 预留 v2（v1 不实现或默认关闭）

| 工具 | 说明 |
|---|---|
| `git_pull` / `git_push` | 远端同步；push 默认禁 force、目标分支白名单 |
| `git_merge` / `git_cherry_pick` / `git_revert` | 返回结构化冲突，不自动解决 |
| `git_rebase` | 仅显式确认；默认不暴露 |
| `git_worktree` | list/add/remove；与子代理独立工作区结合 |
| `git_submodule` | list/update；默认只读 |
| `git_reset` / `git_clean` / `git_gc` | 破坏性/重量级，默认永久不暴露或独立高权限开关 |

### 4.2 工具参数与返回约定

- 所有工具都接受 `repo`（alias）；
- 路径类参数一律是**仓库相对路径**；
- 所有写工具返回 `before` / `after` 状态摘要（HEAD、分支、dirty 文件数）；
- 文本/diff 类工具支持 `max_bytes` / `max_lines` / `stat_only` / `paths` 过滤；
- 失败时返回 `ok=false`、`error_code`、`message`，不抛未处理异常。

统一返回示例：

```json
{
  "ok": true,
  "data": {
    "branch": "main",
    "ahead": 1,
    "behind": 0,
    "staged": ["src/a.py"],
    "unstaged": [],
    "untracked": ["notes.md"],
    "conflicts": []
  },
  "meta": {
    "repo": "my-app",
    "cwd": "/allowed/roots/my-app",
    "command": ["git", "--no-pager", "-C", "/allowed/roots/my-app", "status", "--porcelain=v2", "--branch", "-z"],
    "exit_code": 0,
    "truncated": false,
    "elapsed_ms": 12
  }
}
```

错误码建议：

`not_a_repo` / `repo_not_found` / `policy_denied` / `dirty_worktree` / `protected_branch` /
`invalid_path` / `invalid_argument` / `command_timeout` / `command_failed` / `output_truncated` /
`unsupported_in_v1` / `lock_timeout`（GM3 新增：等待仓库写锁超时）。

> GM1 实现补充：失败信封为**扁平结构** `{ok:false, error_code, message, meta}`，`meta` 恒存在
> （可能为空对象）；新增 `parse_failed`（git 输出无法解析，§5.3）与 `internal_error`
> （未处理异常兜底，绝不把堆栈泄漏给协议层）。可选参数显式传 `null` 视为未提供。

> GM2 实现补充：文本类工具（diff/show patch、file、blame、grep）统一受 `max_bytes`/`max_lines`
> 预算裁剪并在 data/meta 置 `truncated=true`；`git_log`/`git_search_history` 用 `has_more` 标记
> 还有更多；工具自身预算之外，app 层对最终信封再做一次 `--max-output-bytes` 兜底（超限返回
> `output_truncated`，不把巨型 JSON 交给模型）。

> GM3 实现补充：写工具统一返回 `data.action` 与 `data.before`/`data.after`（`branch`/`head_oid`/
> `dirty_files` + 分组计数）；策略类失败（`protected_branch`/`dirty_worktree`/`policy_denied`/
> `lock_timeout`）的 `meta` 至少带 `repo` alias，便于客户端定位被拒绝的仓库。

### 4.3 Resources

MCP Resources 适合低频、可读、可订阅的资源，但 Git 工作区变化频繁，v1 只做少量只读资源：

| URI | 内容 |
|---|---|
| `git://{repo}/status` | 当前状态摘要 |
| `git://{repo}/head` | HEAD 与当前分支 |
| `git://{repo}/file/{ref}/{path}` | 指定 revision 的文件内容 |
| `git://{repo}/log/{ref}` | 最近提交列表 |

Resources 仅作为补充；Cline 的主路径仍是 Tools。

GM4 实现细节：

- 静态资源按已配置仓库展开：`git://<alias>/status`、`git://<alias>/head`；
  另注册两个 **Resource Template**：`git://{repo}/log/{ref}`、`git://{repo}/file/{ref}/{path}`；
- `status`/`head`/`log` 返回紧凑 JSON（`application/json`）；`file` 在文本且未截断时返回
  `text/plain`，二进制或超预算时返回 JSON 元信息（`binary`/`size`/`truncated`，不含原始二进制）；
- `ref` 可含 `/`（如 `origin/main`、`feature/x`）：按“最长可解析前缀”切分 `ref`/`path`，
  也支持 URL 编码 `%2F`；`log` 额外支持 range（如 `HEAD~1..HEAD`）；
- alias 匹配大小写不敏感（URL host 会被规范化），存在大小写歧义时报错；
- 路径经 `RepoRegistry.resolve_path` 校验，拒绝绝对路径/`..`/符号链接逃逸；
- 读取只走只读命令，不修改 index/工作区/refs。

### 4.4 Prompts

| Prompt | 参数 | 用途 |
|---|---|---|
| `write_commit_message` | `repo`、`staged_only` | 基于 staged diff 生成规范提交信息 |
| `review_staged_changes` | `repo` | 生成代码 review 提示词（diff 摘要 + 风险点） |
| `summarize_branch` | `repo`、`base` | 汇总当前分支相对 base 的变更 |
| `explain_conflict` | `repo`、`path` | 解释冲突文件与解决思路（不自动写） |

Prompts 只生成消息内容，不执行写操作。

GM4 实现细节：

- `write_commit_message`：`staged_only` 默认 `true`（只看 index），`false` 时看工作区；
  输出 = 变更统计 + diff(受 16KB 预算裁剪) + conventional commits 要求；
- `review_staged_changes`：staged diff + 严重度分级/风险点/测试缺口要求；
- `summarize_branch`：`base` 必填（ref 校验）；提交列表(`base..HEAD`, 最多 30 条)+ numstat + diff；
- `explain_conflict`：读取冲突文件(预算 16KB), 展示冲突标记与上下文, 要求只解释不修改;
- 参数按 MCP 约定是字符串, 布尔参数接受 `true/false/1/0/yes/no`;
- 提示词文本统一受 24KB 字节预算(超出截断并注明); 全部为只读查询。

---

## 5. Git 执行层

### 5.1 子进程规则

`GitRunner` 统一执行 Git，禁止任何 shell：

```text
git --no-pager -C <repo_path> <subcommand> [args...]
```

- `subprocess` 使用参数列表，禁止 `shell=True`；
- 命令与参数由各 ToolHandler 拼装，不接受模型传入的任意 `git` 子命令/flag；
- 每个工具只允许自己的参数白名单（例如 `git_diff.mode` 枚举）；
- 默认 `--no-pager`，同时设置环境 `GIT_PAGER=cat`；
- 统一超时（默认 30s，可在配置中调整）与输出上限（默认 128KB）。

### 5.2 环境变量策略

| 变量 | 设置 | 原因 |
|---|---|---|
| `GIT_PAGER` / `PAGER` | `cat` | 防交互分页 |
| `GIT_EDITOR` / `EDITOR` | `true` | 防止命令弹编辑器；提交统一用 `-m` |
| `GIT_TERMINAL_PROMPT` | `0` | 禁止凭证弹窗卡死 |
| `LC_ALL` | `C` | 结构化解析不被本地化字符串影响 |
| `HOME` | 继承 Host 环境 | 保持 `.gitconfig`/credential helper 行为 |
| 其他 secret 变量 | 不主动注入、不打印 | 避免 API key 泄漏到日志 |

> 注意：Git credential helper 可能依赖 Host 环境；Server 不主动清理 `HOME`，但审计与日志必须脱敏。

### 5.3 结构化解析

| 数据 | 解析方式 |
|---|---|
| status | `git status --porcelain=v2 --branch -z`（NUL 分隔，文件名含空格/换行不被误切） |
| diff | `git diff --numstat -z`（结构化 files，rename 为 `\0old\0new`）+ `--patch`（预算内片段） |
| log | `git log --pretty=format:%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1f%D%x1e`（字段 US、记录 RS，空 subject 不歧义） |
| branch | `git for-each-ref --format=%(refname:short)%x1f…%x1e`（tracking/ahead/behind/symref/subject） |
| tag | `git tag --format=%(refname:short)%x1f%(objecttype)%x1f…%x1e` |
| blame | `git blame --line-porcelain -L <start>,<end>`（按行窗口采样） |
| grep | `git grep -n -z`（`<path>\0<line>\0<content>`，固定串 `-F` / 扩展正则 `-E`） |
| remote | `git remote -v` + `sanitize_url`（URL 脱敏，§6.4） |
| 文件内容 | `git cat-file -s`（尺寸）+ `git show <ref>:<path>` + NUL 二进制判定 + 编码探测 |

解析失败时保留原始 stdout/stderr，返回 `command_failed` 或 `parse_failed`，不做静默丢数据。

### 5.4 超时、截断与取消

- 所有命令有硬超时；超时后 kill 进程组（POSIX `killpg` / Windows `taskkill` 兜底）；
- 输出超过 `max-output-bytes` 时截断并设置 `meta.truncated=true`；
- GM2 起改为**有界流式读取**：内存只保留前 `max-output-bytes` 字节，超限立即终止进程组（不等命令
  写完），`stdout_total_bytes`/`stderr_total_bytes` 仍记录完整尺寸；
- 大 diff/log 默认优先返回结构化/stat 摘要，完整 patch 由参数显式请求；
- MCP 请求取消时不留下后台 git 进程。

### 5.5 并发与锁

- 每 repo 一把 `asyncio.Lock`，写操作（stage/commit/branch/switch/stash/restore/fetch）串行；
- 可选跨进程锁：通过 `--lock-dir` 指向目录，使用 `flock`/`msvcrt`（与 MeowMeowClaw `memory/filelock.py` 同思路）；
- 与 MeowMeowClaw 子代理协同：若配置同一 lock 目录/文件，Cline Git 写操作与子代理 `write_file`/`exec`
  会互相排队；v1 只提供配置接口，不强制耦合；
- 只读操作不持锁（避免大仓 log/diff 阻塞）。

GM3 已实现：每 repo 一把进程内 `asyncio.Lock`（按 realpath 隔离）；`--lock-dir` 配置后叠加跨进程
文件锁（POSIX `flock` / Windows `msvcrt`，锁文件按 repo realpath 哈希命名、权限 0600），
获取超时（默认 10s）返回 `lock_timeout`。dirty 检查与写命令在同一把锁内原子执行。
> 备注：MeowMeowClaw 子代理的锁文件是 `<memory_dir>/subagents/workspace.lock`，与本实现的
> 按仓库哈希命名不同；同一 `--lock-dir` 的多个 MCP Server 进程可互斥，与主项目 workspace 锁
> 完全打通（共用同一文件）仍属 §12 优化项。

---

## 6. 安全设计

### 6.1 威胁模型

| # | 威胁 | 例子 | 对策 |
|---|---|---|---|
| T1 | 越界仓库 | 模型传 `/etc` 或其它项目路径 | allowlist 根 + alias + realpath 校验 |
| T2 | 覆盖未提交工作 | `switch/restore/checkout` 丢失本地修改 | dirty 检查 + 默认拒绝 + `force` 显式 |
| T3 | 破坏历史 | `reset --hard`、`clean -fdx`、force push | v1 不暴露；保护分支 |
| T4 | 凭证泄漏 | 日志打印 remote URL/token | URL 脱敏、stdout 不写 stderr、不注入 secret |
| T5 | Shell 注入 | 文件名/路径含 shell 元字符 | subprocess 参数列表，禁止 shell |
| T6 | hooks 执行任意代码 | commit/push 触发 hooks | hooks 信任边界归属用户；可选 `--no-verify` 开关 |
| T7 | 大输出耗尽上下文 | `git log` 全历史 | 默认 limit + 截断 + `stat_only` |
| T8 | 并发写坏 index | 多 tool 同时 add/commit | 每 repo 锁 + Git 自身 index.lock |
| T9 | 远端误操作 | push 到错误分支/force | v1 无 push；fetch 只更新 remote refs |

### 6.2 默认权限矩阵

| 能力 | v1 默认 | 开启方式 |
|---|---|---|
| 只读 tools | ✅ 开放 | 默认 |
| 写 tools | ❌ 关闭 | `--read-only=false`；Cline 逐次确认 |
| fetch | ❌ 默认关闭 | `--allow-remote=true`（GM4 已实现） |
| push/pull | ❌ 不实现 | v2 |
| destructive | ❌ 不实现 | v2 独立高权限开关 |
| resources | ✅ 只读 | 默认 |
| prompts | ✅ 只读 | 默认 |

### 6.3 分支与 dirty 保护

- `protected_branches` 默认 `main,master`：禁止直接 `git_commit`（可通过配置覆盖）；
- `git_commit` 前检查 staged 文件非空（`allow_empty` 显式）；
- `git_branch_switch` / `git_restore` / `git_stash apply`：工作区 dirty 时默认拒绝；
- `git_branch_delete`：当前分支、未合并分支默认拒绝；
- `git_commit --amend` 默认关闭，需显式参数。

GM3 实现补充：

- 保护分支上拒绝 `git_commit`（含 `amend`）与 `git_branch_delete`；
- `git_restore`（工作区覆盖）与 `git_branch_switch`：dirty 时拒绝，`force=true` 才放行
  （分别对应覆盖工作区 / `git switch --discard-changes`）；`git_stash pop|apply` 无 force 参数，dirty 一律拒绝；
- `git_branch_delete`：当前分支/保护分支拒绝；未合并分支用 `git merge-base --is-ancestor` 预检后
  `policy_denied`，`force=true` 才 `git branch -D`；
- `git_commit` staged 为空时要求 `allow_empty=true`（`amend` 与 `paths` 模式不受限）；message
  去空白后必须非空；新分支名经 `git check-ref-format --branch` 校验；
- `git_stash drop` 需要 `confirm=true`（仅作用于最近一条 stash），不提供 `git stash clear`。

### 6.4 凭证与 URL 脱敏

- 返回 remote URL 时去掉 userinfo（`https://user:token@host` → `https://***@host`）；
- 不打印 `git config` 中的 `http.*.extraheader`、credential 配置值；
- 审计记录只存命令摘要与结果状态，不存完整环境变量；
- 若用户显式配置 `--audit-log`，审计文件权限 0o600。

> GM2 已实现：`git_list_remotes` / `git_config_get` 的 URL 统一经 `sanitize_url`（去 userinfo +
> token/password 查询参数打码）；`http.*.extraheader`、`credential.*` 及键名含 token/password/secret
> 的配置只返回 `redacted=true`；含 `://` 的 URL 作用域配置键 v1 不支持（返回 `invalid_argument`）。
> GM4 补充：`git_fetch` 的进度输出与失败消息经 `sanitize_text` 做同样的整段脱敏。
> GM6 补充：`--audit-log` 已实现（JSONL，新建文件权限 0600，详见 §8）。

### 6.5 worktree / submodule / 路径

- worktree/submodule 操作在 v2 才实现；v1 只读 `git_list_worktrees` / `git_list_submodules`（可选）；
- 未来实现时：worktree 路径必须落在 allowlist 根内；
- 对 `git show <ref>:<path>` 与文件工具做同一套路径规范化，拒绝 `..` 与绝对路径；
- 符号链接：使用 `realpath` 校验；不跟随指向工作区外的链接。

### 6.6 残余风险（诚实记录）

- Git hooks、credential helper、用户全局配置属于 Host 信任边界，Server 不隔离其代码执行；
- 远端 fetch 可能引入大对象/慢网络；需要超时与磁盘监控（v2）；
- 模型可能通过多次合法小操作组合出风险行为（如逐文件删除）——依赖 Cline 确认与审计；
- Windows 路径/行尾/信号语义与 POSIX 有差异，测试需覆盖。

---

## 7. Cline 集成

### 7.1 传输与进程约定

- v1 采用 **stdio**：Cline 启动 Server 进程，通过 stdin/stdout 交换 JSON-RPC；
- Server 的 **stdout 只允许 MCP 协议消息**；日志/调试输出一律走 stderr；
- Server 收到 SIGINT/SIGTERM 时优雅退出，不遗留 git 子进程；
- 首次握手由 MCP SDK 负责，Server 声明 `tools`、`resources`（含 resource templates）、`prompts` 能力
  （GM1 起 tools；GM4 起 resources/prompts）。

### 7.2 Cline 配置示例

> GM5 已落地：可直接复制的模板在 `docs/cline_mcp_settings.readonly.json` 与
> `docs/cline_mcp_settings.writable.json`；完整步骤、参数表、autoApprove 原则、上下文预算建议与排障
> 见 `docs/CLINE_MCP_GIT.md`；POSIX 启动包装为 `scripts/run_git_mcp_server.sh`（`MCP_GIT_PYTHON` 指定解释器）。
> 下面保留设计期示例，字段含义与模板一致。

Cline 的 MCP 配置通常保存在其扩展全局设置文件（不同版本/系统路径不同，实现阶段确认）。
配置形态：

```json
{
  "mcpServers": {
    "git": {
      "command": "/path/to/python",
      "args": [
        "-m",
        "meowmeowclaw_mcp_git",
        "--root",
        "/path/to/workspace",
        "--read-only=false",
        "--allow-remote=false",
        "--protected-branches",
        "main,master"
      ],
      "env": {
        "MCP_GIT_AUDIT_LOG": "/path/to/workspace/.mcp-git-audit.jsonl"
      },
      "disabled": false,
      "autoApprove": [
        "git_status",
        "git_diff",
        "git_log",
        "git_show",
        "git_blame",
        "git_list_branches",
        "git_list_tags",
        "git_list_remotes",
        "git_grep",
        "git_search_history",
        "git_rev_parse",
        "git_config_get"
      ]
    }
  }
}
```

- `autoApprove` **只放只读工具**；
- `git_stage`/`git_commit`/`git_branch_*`/`git_stash`/`git_restore`/`git_fetch` 留给 Cline 用户确认；
- 如果只做只读使用：配置 `--read-only=true`，写工具不注册；
- GM1 阶段只注册 `git_status`，最小可用配置（`autoApprove: ["git_status"]`）见 §14.1 实现记录。

### 7.3 上下文预算

- `git_log`/`git_search_history` 默认 20 条，多取 1 条探测并用 `has_more` 标记还有更多；
- `git_diff`/`git_show` 始终返回 numstat 结构化 `files`，默认只附带 32KB 以内的 patch 片段；
  需要完整 patch 时显式放大 `max_bytes`（≤ `--max-output-bytes`）或按 `paths`/range 分段；
- `git_blame` 默认前 200 行（`max_lines`）、`git_grep` 默认 100 条匹配，超出置 `truncated=true`；
- `git_file_at_revision` 默认 64KB，二进制文件只回 `size/binary`；
- 工具返回保留 `truncated`/`has_more`；app 层对最终信封再做 `--max-output-bytes` 兜底；
- 对超大文件/大 diff：先拿摘要（stat_only/numstat），再按行/字节窗口分段读取。

### 7.4 常见接入问题排查

| 现象 | 排查 |
|---|---|
| Cline 看不到 `git` server | 检查配置 JSON、command 绝对路径、Python 环境是否有 `mcp` 依赖 |
| Server 启动即退出 | 看 stderr 日志；确认 `--root` 存在且是 git 仓库 |
| 工具报“repo 未找到” | 检查 alias/root 配置 |
| 命令超时 | 大仓/网络；调 `--timeout` 或缩小查询范围 |
| 输出截断 | 使用 `paths`/`max_count`/`stat_only` 缩小请求 |
| 写操作被拒绝 | 检查 `--read-only=false`、保护分支、dirty 状态、错误码 `policy_denied`/`protected_branch`/`dirty_worktree`/`lock_timeout` |

> GM5 起完整排障表（含真实 stderr 文案与错误码）见 `docs/CLINE_MCP_GIT.md` §8。

---

## 8. 观测与审计

- 日志：stderr 结构化/文本，级别 INFO；参数中的路径与 URL 做脱敏；
- 工具调用审计（可选 JSONL）：`{ts, tool, repo, args_digest, status, exit_code, elapsed_ms, truncated}`；
- 不把 Git 输出全文写入审计（只存摘要/哈希），避免敏感内容落盘；
- 观测指标（v2 可选）：工具调用数、失败码分布、超时数、锁等待时间、输出截断次数；
- 审计路径建议不与 repo `.git` 混放：默认用户配置，未配置则不写。

GM6 最小实现（`meowmeowclaw_mcp_git/audit.py`）：

- 事件字段：`ts`(ISO8601 UTC) / `tool` / `repo` / `args_digest`(参数规范 JSON 的 sha256 前 16 位) /
  `status`(`ok`/`error`) / `error_code`(失败时) / `exit_code` / `elapsed_ms` / `truncated`；
- **不落盘参数值与 git 输出全文**（message/paths 等只以摘要出现，测试覆盖）；
- 只审计**工具调用**；prompts/resources 为只读查询，不写审计（需要时可查 stderr 日志）；
- 新建文件权限 `0600`；启动期初始化失败 → `ConfigError`（stderr 提示 + 退出码 2）；
  运行期写失败 fail-soft（只 warning，不影响工具返回）；无轮转/大小上限（最小实现）。

---

## 9. 测试策略

### 9.1 单元测试

- `RepoRegistry`：alias 解析、越界路径、非 git 仓库；
- `PolicyEngine`：只读/写/远端矩阵、保护分支、dirty 拒绝、destructive 拒绝；
- `GitRunner`：参数列表、环境变量、timeout、输出截断、错误码；
- `GitParsers`：status/log/diff/branch 等固定 fixture 输入；
- 每个 ToolHandler：参数校验、策略失败、成功返回结构。

### 9.2 集成测试（本地临时 git 仓库）

- 用 `git init` + 本地提交构造 fixture，不依赖网络；
- 覆盖：status/diff/log/show/blame/stage/commit/branch/stash/restore/fetch（本地 remote）；
- 并发测试：两个写工具同时调用，索引不损坏、串行化生效；
- 失败注入：git 命令超时、非 git 仓库、路径越界、dirty 切换。

### 9.3 MCP 协议测试

- 使用官方 MCP SDK 的客户端测试工具握手/调用/错误映射；
- 验证 stdout 无日志污染；
- 验证 `autoApprove` 只读工具列表与 `read_only=true` 时不注册写工具。

### 9.4 Cline 冒烟（人工验收）

- 在 Cline 中安装/配置 MCP Server；
- 验证：`git_status`/`git_diff`/`git_log` 可用且 autoApprove 生效；
- 验证写操作会弹出确认，确认后 commit/switch 正常；
- 验证只读模式下写工具不可见；
- 记录 Cline 版本、配置路径与截图/日志（实现阶段）。

> GM5 已提供 `docs/CLINE_MCP_GIT.md` §10 的可勾选冒烟清单与两套模板；本机环境无法启动 Cline GUI，
> 真实 GUI 冒烟的记录在 GM6 回填。模板与服务器实现的一致性由 `tests/mcp_git/test_mcp_git_cline_config.py`
> 自动校验（用模板参数真实启动 stdio server 比对工具列表）。

---

## 10. 里程碑计划

> 命名：**GM = Git MCP**。每个里程碑独立提交；默认 `read_only=true`，未完成写能力前不暴露写工具。
> 单线程预计 **8–11 个理想人日**；GM1/GM2 与 GM4 可部分并行。

### 10.1 总览

| 里程碑 | 内容 | 依赖 | 工期 |
|---|---|---|---|
| GM1 | MCP stdio 骨架 + RepoRegistry + `git_status` | — | 1–1.5d |
| GM2 | 只读工具集 + 结构化解析 + 输出预算 | GM1 | 2–2.5d |
| GM3 | 安全写工具 + dirty/分支保护 + 锁 | GM1+GM2 | 2d |
| GM4 | fetch/remote 只读 + Prompts + Resources | GM1+GM2 | 1.5d |
| GM5 | Cline 接入 + 配置模板 + 文档 | GM3+GM4 | 1–1.5d |
| GM6 | 全量验收、真实 Cline 冒烟、打包收尾 | GM5 | 1d |

### 10.2 GM1：MCP 骨架与最小可用（1–1.5d）

**交付物**

- `meowmeowclaw_mcp_git` 包骨架、`python -m` 入口、官方 MCP SDK 依赖可选组；
- `RepoRegistry`（alias/allow roots/is_git_repo）；
- `GitRunner` 最小实现（参数列表、timeout、stdout/stderr 捕获）；
- `git_status` 工具（porcelain v2 结构化）；
- Cline `cline_mcp_settings.json` 最小配置示例；
- 单元测试：registry、runner、status parser。

**验收**

- `python -m meowmeowclaw_mcp_git --root /tmp/repo` 可由 MCP 客户端握手并列出 `git_status`；
- 对临时 git 仓库返回正确 branch/ahead/behind/staged/unstaged/untracked；
- stdout 无日志污染，stderr 日志正常；
- 默认只读。

**提交建议**：`feat(mcp-git): GM1 stdio 骨架与 git_status`

**验收结果（2026-10-13）**：已落地。真 SDK 客户端可握手并列出/调用 `git_status`；临时仓库的
branch/ahead/behind/staged/unstaged/untracked/conflicts 返回正确；stdout 仅协议消息、日志全在
stderr；默认只读（`read_only=true` 时不注册写工具）。详见 §14.1。

### 10.3 GM2：只读工具集（2–2.5d）

**交付物**

- `git_diff` / `git_log` / `git_show` / `git_blame` / `git_file_at_revision`；
- `git_list_branches` / `git_list_tags` / `git_list_remotes` / `git_rev_parse` / `git_config_get`；
- `git_grep` / `git_search_history`；
- 结构化解析、`max_bytes`/`max_count`/`stat_only`/`paths` 过滤、输出截断标记；
- 测试 fixture 与 parser 单测、临时仓库集成测试。

**验收**

- 所有只读工具注册且可调用；
- 大仓/大文件不会一次输出爆炸（截断/limit 生效）；
- 错误码稳定；路径越界被拒绝；
- `read_only=true` 下无任何写工具注册。

**提交建议**：`feat(mcp-git): GM2 只读工具集与结构化解析`

**验收结果（2026-10-13）**：已落地。13 个只读工具全部注册可调用；numstat/log/refs/grep/blame
结构化解析；大 diff/文件按 `max_bytes`/`max_lines` 裁剪并标记 `truncated`，app 层
`--max-output-bytes` 兜底；路径越界统一 `invalid_path`；`read_only=true` 下无写工具注册。
详见 §14.1 GM2 实现记录。

### 10.4 GM3：安全写工具（2d）

**交付物**

- `git_stage` / `git_unstage` / `git_restore` / `git_commit` / `git_branch_create` / `git_branch_switch` / `git_branch_delete` / `git_stash`；
- dirty 检查、保护分支、force 显式参数、commit message 校验；
- 每 repo 锁 + 跨进程锁配置接口；
- 写操作前后状态摘要；
- 单元/集成/并发测试。

**验收**

- `read_only=false` 时写工具注册；默认仍逐工具需 Cline 确认；
- dirty 状态下 switch/restore 默认拒绝；
- protected branch 默认拒绝直接 commit（可配置）；
- 并发写不损坏 index；
- commit/branch/stash 正常且返回 before/after。

**提交建议**：`feat(mcp-git): GM3 安全写工具与仓库锁`

**验收结果（2026-10-13）**：已落地。`read_only=false` 时 21 个工具（13 只读 + 8 写）注册；
dirty 下 switch/restore/apply 默认拒绝并可用 force 显式放行；保护分支默认拒绝 commit/删除；
并发写经每 repo 锁串行、index 完整；写工具返回 before/after 摘要。详见 §14.1 GM3 实现记录。

### 10.5 GM4：远端只读、Prompts 与 Resources（1.5d）

**交付物**

- `git_fetch`（显式开启 `--allow-remote=true` 才注册）；
- remote URL 脱敏；
- MCP Prompts：`write_commit_message` / `review_staged_changes` / `summarize_branch` / `explain_conflict`；
- MCP Resources：status/head/file@ref/log；
- 文档：工具清单与 prompt 使用说明。

**验收**

- 默认不注册 fetch；开启后可 fetch 本地/远端 remote；
- prompts 能基于 staged diff 生成有效消息；
- resources 读取不修改仓库状态。

**提交建议**：`feat(mcp-git): GM4 fetch 与 prompts/resources`

**验收结果（2026-10-13）**：已落地。默认不注册 `git_fetch`，`--allow-remote=true` 后对本地 bare
remote 可 fetch/prune 并返回 ref 变化；4 个 prompts 可列出与构建（staged diff/分支汇总/冲突解释）；
resources 静态项与模板可列出并可读取，读取不修改仓库。详见 §14.1 GM4 实现记录。

### 10.6 GM5：Cline 接入与文档（1–1.5d）

**交付物**

- Cline 配置模板（只读/可写两套）；
- README/独立接入说明：安装、`mcp` 依赖、命令/参数、autoApprove 清单、排障；
- 可选的 `scripts/` 启动包装；
- 只读模式下配置示例可直接复制。

**验收**

- 在真实 Cline 中完成：工具列表出现、只读查询可 autoApprove、写操作弹确认；
- 配置错误时 stderr 有明确提示；
- 只读模式写工具不可见。

**提交建议**：`docs(mcp-git): GM5 Cline 接入指南与配置模板`

**验收结果（2026-10-13）**：配置模板/接入指南/启动包装已落地并自动校验：两套模板均为合法 JSON、
autoApprove 只含 13 个只读工具、用模板参数真实启动 stdio server 后工具列表与 `--read-only` 一致
（只读 13 / 可写 21）；`docs/CLINE_MCP_GIT.md` 覆盖安装、路径、参数、预算、排障与勾选清单。
**真实 Cline GUI 冒烟（工具列表出现、autoApprove 不弹确认、写操作弹确认）留待 GM6 人工执行并回填。**

### 10.7 GM6：全量验收与收尾（1d）

**交付物**

- 全量 pytest + ruff；
- 临时仓库端到端脚本；
- Cline 人工冒烟记录（版本/配置/结果）；
- 发布说明与已知限制。

**验收**

- 所有 GM1–GM5 用例通过；
- `read_only=true` / `false` 两种模式均验证；
- 文档与实现一致；
- 无残留 git 子进程、无 stdout 污染。

**提交建议**：`docs(mcp-git): GM6 验收记录与首版收尾`

**验收结果（2026-10-13）**：全量 **2135 passed / 6 skipped / 2 xfailed**、ruff 通过；
端到端脚本 `scripts/verify_git_mcp_server.py` **24/24 PASS**（只读 13 工具 / 可写 21 + 审计 /
远端 22 + fetch 更新 refs 不合并 / stdout 0 字节 / 无残留进程）；`read_only` 两模式均已验证；
最小审计落地。完整记录（环境、目标 G1–G7、里程碑、模式矩阵、Cline GUI 记录表、发布说明与
已知限制）见 **`docs/GIT_MCP_ACCEPTANCE.md`**。

---

## 11. 验收清单

- [x] GM1：MCP 握手/工具列表/`git_status` 通过（2026-10-13，见 §14.1）；
- [x] GM2：只读工具齐全、结构化、有输出预算（2026-10-13，见 §14.1）；
- [x] GM3：写工具默认关闭、dirty/保护分支/锁生效（2026-10-13，见 §14.1）；
- [x] GM4：fetch 默认关闭、prompts/resources 可用（2026-10-13，见 §14.1）；
- [ ] GM5：真实 Cline 接入并完成只读 autoApprove + 写确认（配置模板/指南/自动校验已完成，GUI 冒烟待 GM6）；
- [ ] GM6：全量 pytest + ruff + 人工冒烟记录（pytest/ruff/端到端脚本已完成，Cline GUI 记录待人工回填）；
- [x] 所有路径参数在 allowlist 根内（`invalid_path` 用例覆盖，GM2/GM3）；
- [x] stdout 仅 MCP 协议消息（空 stdin 启动 stdout 0 字节；真 stdio 会话无解析错误）；
- [x] `read_only=true` 下无写工具注册（只读 13 工具；模板/协议用例断言）；
- [x] 残余风险已写入文档（§6.6 与 `docs/GIT_MCP_ACCEPTANCE.md` §6）。

---

## 12. 后期优化建议

1. **远端能力**：push/pull/rebase/cherry-pick，分支白名单、dry-run、PR 辅助；
2. **worktree/submodule**：与 MeowMeowClaw 子代理独立工作区结合，降低共享工作区锁竞争；
3. **HTTP/streamable transport**：远程 Cline/多客户端接入，鉴权与 TLS；
4. **仓库索引/缓存**：大仓 log/diff 缓存、引用索引；
5. **MCP Client 接入 MeowMeowClaw**：把 Git MCP Server 作为主 Agent 的可选工具来源；
6. **权限审批整合**：把 Git destructive 操作接入 Cline/主项目的审批 UI；
7. **观测导出**：审计 JSONL 之外的 metrics/OpenTelemetry；
8. **多仓库批量操作**：workspace 级 status/diff 汇总，但写操作仍按 repo 隔离。

---

## 13. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| MCP SDK 版本/接口变化 | 启动/握手失败 | 锁定版本；用官方 client 做协议测试 |
| Git 版本差异 | 参数/输出不兼容 | 记录最低版本（建议 git ≥2.30）；parser 容错 |
| 路径/编码/CRLF | 文件解析错误 | `realpath` + bytes/text 明确编码 + Windows 测试 |
| hooks/credential 阻塞 | 命令卡死 | 超时、`GIT_EDITOR=true`、`GIT_TERMINAL_PROMPT=0` |
| 模型误调用写工具 | 工作区被改 | 默认只读、写工具需确认、dirty 保护、审计 |
| Cline autoApprove 误配 | 写操作自动执行 | 文档明确只放只读工具；代码层不依赖 autoApprove |
| 大输出撑爆上下文 | 工具失败/成本 | limit/截断/结构化摘要 |
| 与主项目锁不协调 | 并发写冲突 | 预留 lock-dir 共用，后续与 `WorkspaceMutationGuard` 对齐 |

---

## 14. 实施状态

| 里程碑 | 状态 | 提交 | 备注 |
|---|---|---|---|
| GM1 | 已完成 | `d010f63` | MCP stdio 骨架 + RepoRegistry + `git_status`；新增 136 用例；默认只读、stdout 无污染 |
| GM2 | 已完成 | `43391ea` | 13 个只读工具 + 6 个解析器 + 输出预算/有界截断；新增 141 用例 |
| GM3 | 已完成 | `8b187d5` | 8 个写工具 + PolicyEngine + 每仓库锁(进程内/跨进程)；新增 85 用例 |
| GM4 | 已完成 | `eba419f` | `git_fetch`(默认关闭) + URL 脱敏收口 + 4 prompts + 4 resources/模板；新增 52 用例 |
| GM5 | 已完成 | `bf2b356` | 两套 Cline 模板 + 接入指南 + 启动包装 + 模板自动校验；GUI 冒烟待 GM6 |
| GM6 | 已完成 | `195d71e` | 最小审计(`--audit-log`) + 端到端脚本 24/24 + 验收记录/发布说明；新增 15 用例 |
| GM6.1 | 已完成 | `6d209a5` | 补丁: `git_fetch` 忽略远端 HEAD 符号引用(git ≥2.46/CI 兼容)；新增 1 用例 |

### 14.1 实现记录

#### GM1 实现记录（2026-10-13）

**交付物**

| 文件 | 变更 |
|---|---|
| `meowmeowclaw_mcp_git/__init__.py` / `__main__.py`（新增） | 顶层独立包 + `python -m` 入口；缺 `mcp` 时 stderr 提示 `pip install 'meowmeowclaw[mcp-git]'` 并返回退出码 3；包 import 不依赖 SDK |
| `meowmeowclaw_mcp_git/config.py`（新增） | §3.1 启动键解析（CLI > env > 默认）：`--root`/`--roots`/`--repos`、只读/远端/破坏性开关、保护分支、超时、输出上限、日志级别；布尔/正数/枚举校验；`--audit-log`/`--lock-dir` 先收下（GM6/GM3 消费） |
| `meowmeowclaw_mcp_git/registry.py`（新增） | `RepoEntry` + `RepoRegistry`：允许根 realpath、alias 唯一、工作树顶层校验、非 git/子目录/裸仓库启动即拒；`resolve_path` 拒绝绝对路径/`..`/符号链接逃逸 |
| `meowmeowclaw_mcp_git/runner.py`（新增） | `GitRunner`：`git --no-pager -C <repo> …` 参数列表（无 shell）、环境清洗（§5.2）、硬超时杀进程组、输出上限截断标记、`GitCommandError`/`CommandTimeoutError` |
| `meowmeowclaw_mcp_git/parsers/status.py`（新增） | porcelain v2（`--branch -z`）解析 branch/upstream/ahead/behind 与 staged/unstaged/untracked/conflicts/renames；行模式 C-style 引号解码；未知行报 `parse_failed`，截断尾段丢弃 |
| `meowmeowclaw_mcp_git/errors.py` / `envelope.py` / `validation.py`（新增） | 错误码与异常、统一信封、JSON Schema 子集参数校验 |
| `meowmeowclaw_mcp_git/tools/base.py` / `tools/read.py`（新增） | `ToolSpec`/`ToolContext`/`ToolOutcome` 声明式工具表 + `git_status` 处理器（含 `short` 摘要模式） |
| `meowmeowclaw_mcp_git/app.py`（新增） | 协议无关装配与派发：权限矩阵工具选择、统一信封、未处理异常兜底 `internal_error` |
| `meowmeowclaw_mcp_git/server.py`（新增） | MCP stdio 适配：`list_tools`/`call_tool`（`validate_input=False` 走自有信封）；日志只写 stderr |
| `pyproject.toml` / `.github/workflows/ci.yml` | 新增可选依赖组 `mcp-git = ["mcp>=1.0,<2"]`（主项目运行时不引入）；CI 安装 `.[dev,mcp-git]` 并把新包纳入 ruff |
| `tests/mcp_git/*`（新增 7 个文件） | 136 用例：配置/registry/runner/status parser/app 权限矩阵/`git_status` 端到端/协议握手与 stdout 纯净性 |
| `README.md` | 规模数据、测试表、目录结构、Roadmap 同步 |

**关键实现决策**

- **默认只读**：`read` 始终注册，`write` 仅 `read_only=false`、`remote` 仅 `allow_remote=true`、
  `destructive` v1 永不注册——权限矩阵在 `app.select_tools` 一处收口（§3.3/§6.2）；
- **仓库边界**：配置路径必须是工作树顶层，避免 alias 指向子目录后 git 操作越权到父仓库（§6.1 T1）；
- **`git_status` 解析**：生产路径用 `--porcelain=v2 --branch -z`，文件名含空格/换行不被误切；
  行分隔输入按 C-style 引号解码（`\t`/`\NNN`），供 fixture 与调试使用；
- **输出预算**：`short=true` 只回计数；输出被截断时返回 `output_truncated` 错误而不是残缺文件
  列表，避免模型把“截断后的干净”当成真的干净（G3/T7）；
- **错误契约**：失败统一扁平信封且 `meta` 恒存在；`internal_error` 兜底未处理异常；
- **空值与缺省**：可选参数显式 `null` 视为未提供；单仓库模式下 `repo` 可省略；
- **alias 大小写**：GM1 精确匹配（大小写敏感），大小写开关留待后续；
- `--allow-destructive=true` 也不注册破坏性工具；`mcp` 只在 `server.py` 导入，其余层可无 SDK 单测（G7）。

**验证**

- `python -m meowmeowclaw_mcp_git --root <repo>`：真 SDK 客户端握手成功，`tools/list` 仅
  `git_status`，`tools/call` 返回结构化 branch/ahead/behind/staged/unstaged/untracked/conflicts；
- stdout 纯净性：空 stdin 启动/退出后 stdout 为 0 字节、日志全在 stderr；真实 stdio 子进程会话可握手/调用；
- 错误路径：未知 alias → `repo_not_found`；多仓库缺 `repo`、参数类型错误、未知参数/工具 →
  `invalid_argument`；非仓库根/子目录/裸仓库 → 启动退出码 2 且 stderr 有明确说明；
- 全量回归 **1825 passed / 6 skipped / 2 xfailed**（新增 136 用例）；`ruff check meowmeowclaw
  meowmeowclaw_mcp_git tests` 通过；
- Cline 最小配置（GM1 只注册 `git_status`）：

```json
{
  "mcpServers": {
    "git": {
      "command": "/path/to/python",
      "args": ["-m", "meowmeowclaw_mcp_git", "--root", "/path/to/repo", "--read-only=true"],
      "disabled": false,
      "autoApprove": ["git_status"]
    }
  }
}
```

- 本阶段不做：其余只读工具（GM2）、写工具与锁（GM3）、fetch/prompts/resources（GM4）、
  Cline 配置模板与真实 GUI 冒烟（GM5/GM6）。

**备注**：`python -m` 需要包可导入（源码运行需项目根在 `PYTHONPATH`，或执行
`pip install -e ".[mcp-git]"`）；`--audit-log`/`--lock-dir` 当前只记录并提示未接入。

#### GM2 实现记录（2026-10-13）

**交付物**

| 文件 | 变更 |
|---|---|
| `meowmeowclaw_mcp_git/parsers/{diff,log,refs,grep,blame}.py`（新增） | numstat(-z)/log(US+RS)/for-each-ref/tag/remote(URL 脱敏)/grep(-z)/blame(porcelain) 结构化解析；截断尾段统一丢弃，未知格式报 `parse_failed` |
| `meowmeowclaw_mcp_git/budget.py`（新增） | `fit_text`/`fit_items`：按字节/行裁剪并返回 total/returned 尺寸；UTF-8 边界安全（不产生半个字符） |
| `meowmeowclaw_mcp_git/runner.py` | 输出改为**有界流式读取**（`_BoundedReader`）：内存只保留前 cap 字节，超限立即终止进程组；`truncated`/`stdout_total_bytes` 语义保留 |
| `meowmeowclaw_mcp_git/tools/common.py`（新增） | 只读工具共享件：git 调用/错误映射/meta/相对路径校验/ref 与 pattern 防选项注入/预算取小 |
| `meowmeowclaw_mcp_git/tools/read.py` | `git_diff`（worktree/staged/commit/range + paths/stat_only/context_lines/max_bytes）、`git_log`（默认 20 + has_more + author/since/until/path/grep）、`git_show`（commit 元信息 + numstat + patch）、`git_blame`（行窗口，默认 200 行）、`git_file_at_revision`（编码/大小/二进制/截断） |
| `meowmeowclaw_mcp_git/tools/refs.py`（新增） | `git_list_branches`（tracking/remote 作用域）、`git_list_tags`、`git_list_remotes`（URL 脱敏）、`git_rev_parse`、`git_config_get`（敏感键 `redacted`） |
| `meowmeowclaw_mcp_git/tools/search.py`（新增） | `git_grep`（正则/固定串、大小写、ref/路径、行/字节预算）、`git_search_history`（-S/-G + has_more） |
| `meowmeowclaw_mcp_git/app.py` | 注册全部只读工具；成功信封超过 `--max-output-bytes` 时返回 `output_truncated` 兜底 |
| `tests/mcp_git/*`（新增 6 个文件 + 扩充 runner） | 141 用例：解析器、diff/log/show/blame/file 端到端、refs/config、grep/历史搜索、有界截断、信封兜底 |
| `README.md` / 本文 | 工具表、解析格式、预算策略、目录树、实施状态同步 |

**关键实现决策**

- **输出预算分层**：工具参数（`max_bytes`/`max_lines`/`max_count`/`stat_only`）→ 工具级裁剪并标记
  → runner 有界流式截断（内存有界、超限早停）→ app 层 `--max-output-bytes` 信封兜底（超限报
  `output_truncated`，不截 JSON）；
- **patch 默认受限**：`git_diff`/`git_show` 始终回 numstat 结构化 `files`，默认附带 32KB 以内 patch；
  完整 patch 需显式放大 `max_bytes` 或按 paths/range 分段；
- **分页标记**：`git_log`/`git_search_history` 多取 1 条，用 `has_more` 表示还有更多（不引入
  next_cursor；模型用过滤条件/`max_count` 收敛）；
- **blame 行窗口**：未给 `end_line` 时用 `-L start,+max_lines`，取满才标 `truncated`；显式范围超过
  `max_lines` 时自动收窄并标记；
- **文本编码**：`git_file_at_revision` 按 NUL 判二进制，文本依次尝试 UTF-8/GB18030，返回
  `encoding/size`；字节裁剪后尾字符用替换符兜底；
- **选项注入防护**：ref 类参数拒绝 `-` 开头/空白；pattern 以 `-e`/`-S` 粘连传递；tag pattern 拒绝
  `-` 开头；config key 限字符集，含 `://` 的 URL 作用域键 v1 不支持；
- **敏感信息**：remote/config URL 去 userinfo 并打码 token 查询参数；`http.*.extraheader`、
  `credential.*`、键名含 token/password/secret 的配置只回 `redacted=true`；
- **错误码稳定**：路径越界 `invalid_path`；参数组合/枚举/边界 `invalid_argument`；git 非零退出
  `command_failed`；输出截断 `output_truncated`；解析失败 `parse_failed`。

**验证**

- 13 个只读工具在临时仓库全部可调用，结构化字段与 git 原始输出对照一致（numstat rename/binary、
  tag 类型与主题、upstream track 的 ahead/behind/gone、blame 行/作者/时间、grep 行号/内容）；
- 大输出：500KB 假 git 输出在 1000 字节上限下 1 秒内被截断并终止进程组；patch/file/grep 的
  `truncated` 生效；400 字节上限下 app 层兜底返回 `output_truncated`；
- 路径/参数安全：`..`/绝对路径/符号链接逃逸 → `invalid_path`；`-` 开头的 ref/pattern 不会成为
  git 选项；二进制/GB18030 文件按预期返回；
- `read_only=true` 下写工具仍不注册（GM3 才引入写工具）；
- 全量回归 **1969 passed / 6 skipped / 2 xfailed**（新增 141 用例）；`ruff check meowmeowclaw
  meowmeowclaw_mcp_git tests` 通过；
- 本阶段不做：写工具与锁（GM3）、fetch/prompts/resources（GM4）、Cline GUI 冒烟（GM5/GM6）、
  `git_list_worktrees`/`git_list_submodules`（§6.5 可选，未排期）。

#### GM3 实现记录（2026-10-13）

**交付物**

| 文件 | 变更 |
|---|---|
| `meowmeowclaw_mcp_git/policy.py`（新增） | `PolicyEngine`：只读开关二次校验、保护分支（默认 main/master）、dirty 保护（force 显式放行）、删除保护（当前/保护分支） |
| `meowmeowclaw_mcp_git/locks.py`（新增） | `RepoLockManager`：每 repo 进程内 `asyncio.Lock`（按 realpath 隔离）+ `--lock-dir` 跨进程 flock/msvcrt；锁文件按 repo 哈希命名、0600；获取超时 → `lock_timeout` |
| `meowmeowclaw_mcp_git/tools/write.py`（新增） | 8 个写工具：stage/unstage/restore/commit/branch_create/branch_switch/branch_delete/stash；全部在仓库锁内执行并返回 before/after |
| `meowmeowclaw_mcp_git/tools/base.py` / `common.py` | `ToolContext` 注入 policy/locks；`read_status` 统一读取写前状态；`require_paths` 等参数校验 |
| `meowmeowclaw_mcp_git/app.py` | 写工具按 `read_only` 注册（默认 13 → `--read-only=false` 后 21）；失败信封补充 repo alias meta；装配 policy/locks；`--lock-dir` 启动日志 |
| `meowmeowclaw_mcp_git/errors.py` | 新增 `lock_timeout` 错误码与 `LockTimeoutError` |
| `meowmeowclaw_mcp_git/parsers/{status,refs}.py` | status 增加 `dirty_paths`/`dirty_count`/`to_state_dict`；stash list 解析（`%gd/%H/%gs`） |
| `tests/mcp_git/*`（新增 4 个文件） | 85 用例：策略矩阵/锁（进程内+跨进程）/8 个写工具端到端（dirty/保护分支/force/amend/confirm）/并发写与 index 完整性 |
| `README.md` / 本文 | 工具表、锁与保护说明、目录树、实施状态同步 |

**关键实现决策**

- **两段式权限**：工具注册按 `read_only` 过滤（默认只读 13 个，`--read-only=false` 才出现 8 个写工具），
  handler 内再 `ensure_writes_enabled` 二次校验（防御直接调用）；
- **检查与写入原子**：dirty/保护分支检查与 git 写命令在同一把仓库锁内执行，避免 TOCTOU；
- **危险开关必须显式**：`git_restore --force` 覆盖工作区、`git_branch_switch --force` 丢弃本地修改、
  `git_branch_delete --force` 强删未合并分支、`git_commit --amend`/`allow_empty`、`git_stash drop confirm`
  全部默认拒绝；仍不实现 reset --hard/clean/push 等 destructive 工具（§6.2）；
- **commit 保护**：保护分支拒绝直接 commit/amend；staged 为空时要求 `allow_empty`（`amend` 与 `paths`
  模式不受限）；message 去空白后必须非空；`paths` 模式按 git 语义提交这些路径的工作区内容；
- **分支保护**：新分支名经 `git check-ref-format --branch` 校验并拒绝 `-` 开头与 `@{`；删除时当前分支/
  保护分支拒绝，未合并分支用 `git merge-base --is-ancestor` 预检后 `policy_denied`，`force=true` 才 `-D`；
- **stash 语义**：`list` 只读不持锁；`push` 支持 message/paths（未指定 paths 默认不含 untracked）；
  `pop`/`apply` 要求工作区干净；`drop` 需要 `confirm=true` 且只作用于最近一条；不提供 `stash clear`；
- **锁实现**：只读工具不取锁；写操作在锁内执行，进程内 `asyncio.Lock` 按 repo realpath 隔离，
  `--lock-dir` 时叠加 flock/msvcrt 文件锁（0600），超时返回 `lock_timeout` 而不是无限等待；
- **与主项目锁的关系**：本实现按 repo 哈希命名锁文件，MeowMeowClaw 子代理用
  `<memory_dir>/subagents/workspace.lock`；同一 `--lock-dir` 的多个 MCP Server 进程可互斥，
  与主项目 workspace 锁完全打通仍属 §12 优化项（§5.5 已备注）。

**验证**

- 8 个写工具端到端：stage（路径/全部）→ commit（普通/amend/signoff/paths/allow_empty）→
  branch_create（含 switch/start_point）→ branch_switch → branch_delete（merged/unmerged/force）→
  stash（push/list/apply/pop/drop）全部通过，并返回 before/after 状态摘要；
- 保护与拒绝路径：保护分支 commit/删除 → `protected_branch`；当前分支/未合并分支删除、stash drop 无
  confirm → `policy_denied`；dirty 时 switch/restore/apply → `dirty_worktree`；只读模式下写工具不可见；
  `..`/绝对路径/符号链接 → `invalid_path`；
- 并发：10 个并发 stage 后 index 内容完整且无 `.git/index.lock` 残留；第二个写操作在锁上等待；
  锁超时返回 `lock_timeout`；同一 `--lock-dir` 的跨进程锁互斥且释放后可获取；失败路径不泄漏锁；
- 真实 stdio 会话（`--read-only=false`）：`tools/list` 返回 21 个工具，`git_stage`/`git_commit`/`git_log`
  经 MCP 协议完成一次真实提交；
- 全量回归 **2054 passed / 6 skipped / 2 xfailed**（新增 85 用例）；`ruff check meowmeowclaw
  meowmeowclaw_mcp_git tests` 通过；
- 本阶段不做：fetch/prompts/resources（GM4）、Cline 配置模板与真实 GUI 冒烟（GM5/GM6）、审计（GM6）。

#### GM4 实现记录（2026-10-13）

**交付物**

| 文件 | 变更 |
|---|---|
| `meowmeowclaw_mcp_git/tools/remote.py`（新增） | `git_fetch`：仅 `--allow-remote=true` 注册；仓库锁内执行；`--prune` 可选；返回 before/after + remote ref 变化（created/updated/deleted）；输出与错误经 `sanitize_text` 脱敏 |
| `meowmeowclaw_mcp_git/prompts.py`（新增） | 4 个 MCP Prompts：`write_commit_message`/`review_staged_changes`/`summarize_branch`/`explain_conflict`；只读取数 + 24KB 字节预算 |
| `meowmeowclaw_mcp_git/resources.py`（新增） | 静态资源（status/head，按仓库展开）+ 资源模板（log/file）；只读读取，路径经 registry 校验，ref 支持含 `/` 与 range |
| `meowmeowclaw_mcp_git/server.py` | 注册 `list_prompts`/`get_prompt`/`list_resources`/`list_resource_templates`/`read_resource`；instructions 补充能力说明 |
| `meowmeowclaw_mcp_git/app.py` | prompts/resources 的协议无关装配与派发 |
| `meowmeowclaw_mcp_git/policy.py` | `ensure_remote_enabled`：`allow_remote=false` 的二次防线 |
| `meowmeowclaw_mcp_git/runner.py` | 新增 `kill_on_limit`：fetch 等有副作用命令完整执行、仅截断输出（不被早停杀死） |
| `meowmeowclaw_mcp_git/parsers/refs.py` | `sanitize_text`：整段文本（进度/报错）URL 脱敏；userinfo 正则改为非锚定（可匹配句子中的 URL） |
| `tests/mcp_git/*`（新增 3 个文件 + 扩充协议/runner/parser） | 52 用例：fetch（本地 bare remote/prune/脱敏）、prompts（含真实冲突）、resources（状态/文件/日志/越界）、协议与 runner |
| `README.md` / 本文 | 工具/prompt/resource 说明、目录树、实施状态同步 |

**关键实现决策**

- **fetch 等级与开关**：`git_fetch` 属 `remote` 等级，仅 `--allow-remote=true` 注册（与 `read_only`
  无关，只读模式下也可显式开启远端）；handler 内 `ensure_remote_enabled` 二次校验；
- **只拉不合并**：只执行 `git fetch [--prune] [remote]`，不动工作区/本地分支；用 fetch 前后的
  remote-tracking refs 对比生成结构化 `refs`（created/updated/deleted），比解析进度文本更稳定；
  对比时跳过远端 HEAD 符号引用（`refs/remotes/<remote>/HEAD`，见 GM6 补丁记录）；
- **fetch 不被早停杀死**：GM2 的有界流式读取默认超限杀进程组；fetch 有副作用，改用
  `kill_on_limit=False`（读完、只截断输出），避免半更新状态；
- **脱敏**：fetch 的进度输出与失败消息统一 `sanitize_text`（userinfo → `***`，token/password
  查询参数打码）；
- **Prompts 只读**：四个 prompt 只调用只读工具/解析器收集上下文并拼文本，不做任何写操作；
  MCP prompt 参数是字符串，布尔按 `true/false/1/0/yes/no` 解析；提示词文本 24KB 预算，
  diff/文件各自 16KB 预算；
- **Resources 只读**：status/head 静态列出，log/file 以 Resource Template 暴露；`file` 文本未截断
  时直接回原文，二进制/截断时回 JSON 元信息；`ref`/`path` 用“最长可解析前缀”切分以支持
  `origin/main` 这类含 `/` 的 ref；
- **错误语义**：tools 仍是 `{ok,...}` 信封；prompts/resources 走 MCP 协议错误（消息保留业务错误
  文案，如 `repo_not_found`/`invalid_path`）。

**验证**

- fetch：本地 bare remote 场景下 fetch 更新 `origin/main`、prune 删除远端已删分支、无变更时
  `refs_changed=0`；凭证不会出现在工具返回（错误消息经脱敏）；`--allow-remote=false` 时工具不注册；
- prompts：staged diff 提示词包含文件/改动/要求；真实 merge 冲突下 `explain_conflict` 展示冲突
  标记与解决要求；大 diff 触发 24KB 预算截断；调用后仓库 HEAD/status 不变；
- resources：status/head/log/file 可读；含 `/` 的 ref、range、二进制、超 64KB 文本、中文路径、
  大小写 alias 均按预期；`..` 越界 → `invalid_path`；读取前后仓库状态不变；
- 真实 stdio 会话（`--allow-remote=true`）：`tools/list` 14 个工具、4 个 prompts、2 个 resources
  + 2 个 templates，`git_fetch`/`get_prompt`/`read_resource` 全部可用；
- 全量回归 **2106 passed / 6 skipped / 2 xfailed**（新增 52 用例）；`ruff check meowmeowclaw
  meowmeowclaw_mcp_git tests` 通过；
- 本阶段不做：Cline 配置模板与真实 GUI 冒烟（GM5）、审计与全量验收收尾（GM6）。

#### GM5 实现记录（2026-10-13）

**交付物**

| 文件 | 变更 |
|---|---|
| `docs/cline_mcp_settings.readonly.json`（新增） | 只读模板：`--read-only=true`；`autoApprove` = 13 个只读工具；可直接复制（替换 `/path/to/python`、`/path/to/repo`） |
| `docs/cline_mcp_settings.writable.json`（新增） | 可写模板：`--read-only=false` + `--allow-remote=false` + `--protected-branches main,master` + `--timeout 30`；`autoApprove` 仍只放只读工具 |
| `docs/CLINE_MCP_GIT.md`（新增） | 接入指南：安装/自检、Cline 配置文件路径、五分钟只读接入、可写模式与确认策略、多仓库、参数/环境变量表、autoApprove 原则、上下文预算建议、排障表、安全边界、人工冒烟勾选清单 |
| `scripts/run_git_mcp_server.sh`（新增） | POSIX 启动包装：`MCP_GIT_PYTHON` 指定解释器，其余参数透传；Windows 直接用 python.exe（指南注明） |
| `README.md` | §1 新增「Git MCP Server(Cline 接入)」小节（安装/入口/模板/指南指引）；目录树与 Roadmap 同步 |
| `.env.example` | 说明该服务**不读取本 .env**，列出 `MCP_GIT_*` 环境变量与指南位置 |
| `tests/mcp_git/test_mcp_git_cline_config.py`（新增） | 14 用例：模板 JSON/占位符/autoApprove 白名单、指南覆盖、包装脚本可执行，以及**用模板参数真实启动 stdio server** 比对工具列表 |
| `docs/GIT_MCP_DESIGN.md` | §7.2/§7.4/§9.4/§10.6/§11/§14/附录 A 与状态同步 |

**关键实现决策**

- **模板即验收用例**：测试直接读取 `docs/cline_mcp_settings.*.json`，替换占位符后用官方 SDK
  `stdio_client` 启动服务器，断言只读模板=13 只读工具、可写模板=13 只读+8 写（不含 fetch），
  保证文档与实际注册矩阵不会漂移；
- **autoApprove 白名单代码化校验**：断言模板里的 `autoApprove` 恰好等于 `build_read_tools()` 的 13 个
  名字，且不含任何写/远端工具（防误配导致写操作自动执行）；
- **可写模板默认保守**：`--allow-remote=false`、`--protected-branches main,master`、`--timeout 30`；
  危险开关（force/amend/allow_empty/confirm）仍由每次调用显式传参 + Cline 确认；
- **真实 GUI 冒烟与自动校验分离**：本机环境无法启动 Cline GUI，因此 GM5 交付“模板 + 指南 + 可自动执行的
  一致性校验”，真实 Cline 的 autoApprove/写确认留到 GM6 人工执行并回填；
- **启动包装最小化**：只做解释器选择与 `exec`，不引入额外依赖；Windows 明确不用该脚本。

**验证**

- `tests/mcp_git/test_mcp_git_cline_config.py` 14 用例通过：模板结构/占位符、autoApprove 白名单、
  指南覆盖关键参数与工具、包装脚本可执行并能 `--version`、模板真实启动 stdio 后工具列表一致；
- 全量回归 **2120 passed / 6 skipped / 2 xfailed**（新增 14 用例）；`ruff check meowmeowclaw
  meowmeowclaw_mcp_git tests` 通过；
- 配置错误提示：`--root` 指向非仓库/不存在 → stderr `启动失败: ...` + 退出码 2；缺 `mcp` →
  stderr 安装提示 + 退出码 3（指南 §8 排障表）；
- 未完成：真实 Cline GUI 冒烟（记录版本/配置路径/截图）→ GM6。

#### GM6 实现记录（2026-10-13，v0.1 收尾）

**交付物**

| 文件 | 变更 |
|---|---|
| `meowmeowclaw_mcp_git/audit.py`（新增） | 最小审计：`AuditSink`（JSONL、0600、fail-soft）+ `digest_arguments`；事件 = `ts/tool/repo/args_digest/status/error_code/exit_code/elapsed_ms/truncated` |
| `meowmeowclaw_mcp_git/app.py` | 派发拆成 `_dispatch`（返回 payload）+ 审计钩子 `_record_audit`；审计失败不影响工具返回；`--audit-log` 初始化失败在启动期报 `ConfigError` |
| `meowmeowclaw_mcp_git/server.py` | 启动日志提示审计已启用 |
| `scripts/verify_git_mcp_server.py`（新增） | 端到端验收脚本：临时仓库 + 本地 bare remote；只读/可写/远端三阶段 + 审计 + stdout 纯净性 + 残留进程检查（24 项） |
| `tests/mcp_git/test_mcp_git_audit.py`（新增） | 15 用例：0600/JSONL/事件字段/错误码/截断标记/参数不落盘/fail-soft/启动校验/prompts-resources 不审计 |
| `docs/GIT_MCP_ACCEPTANCE.md`（新增） | GM6 验收记录：环境、自动结果、端到端 24 项、目标与里程碑、模式矩阵、Cline GUI 记录表、发布说明与已知限制、残余风险、回滚 |
| `README.md` / `.env.example` / 本文 | 审计与验收文档、规模数据、目录树、Roadmap、实施状态同步 |

**关键实现决策**

- **审计最小而有用**：只审计工具调用，事件只存参数摘要与结果状态，不写参数值/输出全文（避免提交
  message 与路径泄漏）；初始化失败 fail-fast（退出码 2），运行期写失败 fail-soft；
- **不破坏 stdout 纯净性**：审计只写文件；文件权限 0600；无轮转/大小上限（最小实现，已列为已知限制）；
- **prompts/resources 不审计**：属只读查询，保持审计语义聚焦工具调用；
- **GM5 GUI 冒烟未闭环**：本环境无 Cline GUI，因此 GM6 以“模板参数真实启动 stdio server 的自动校验
  + 端到端脚本 + 人工记录模板”替代；Cline GUI 记录表保留待人工回填（验收记录 §5）；
- **验收可重复**：`scripts/verify_git_mcp_server.py` 纯本地（无网络），24 项检查覆盖两种模式、审计、
  stdout 与残留进程，可随时重跑。

**验证**

- 全量回归 **2135 passed / 6 skipped / 2 xfailed**（Git MCP 446 用例，GM6 新增 15）；`ruff check
  meowmeowclaw meowmeowclaw_mcp_git tests` 通过；
- 端到端脚本 24/24 PASS：只读 13 工具；可写 21 工具 + 真实提交 + 审计（0600、不落盘 message）；
  远端 22 工具 + fetch 更新 refs 且不合并；空 stdin stdout 0 字节；无残留 server 进程；
- Cline GUI 冒烟记录表（9 项）见 `docs/GIT_MCP_ACCEPTANCE.md` §5，待人工执行；
- 明确未完成：真实 Cline GUI 冒烟、Windows 实机验证（验收记录 §6 已知限制 #8/#9）。


**v0.1 补丁（2026-10-13）**：修复 git ≥2.46 下 `git_fetch` 误报远端 HEAD 符号引用导致 CI 失败的问题。
新版 git 的 `git fetch` 会按 `remote.<name>.followRemoteHEAD`（默认 `create`）自动创建/更新
`refs/remotes/<remote>/HEAD`；它本身是符号引用，且 `%(refname:short)` 会把它缩写成远端名（如 `origin`），
旧实现把它当成一次 ref 变化（CI 上表现为 `refs_changed=1` 与 `refs=[origin, origin/main]`）。
修复：`_remote_refs` 改为读取 `%(refname)/%(refname:short)/%(objectname)/%(symref)` 并跳过符号引用与
`*/HEAD` 条目；新增 1 个回归用例（用 `git remote set-head origin --auto` 确定性复现新 git 行为）。
提交号：`6d209a5`。

---

## 附录 A：只读模式 Cline 配置模板

> 可复制文件：`docs/cline_mcp_settings.readonly.json`（可写模板：`docs/cline_mcp_settings.writable.json`）；
> 接入步骤见 `docs/CLINE_MCP_GIT.md`。下面为等价示例（占位符需替换为绝对路径）。

```json
{
  "mcpServers": {
    "git": {
      "command": "/path/to/python",
      "args": ["-m", "meowmeowclaw_mcp_git", "--root", "/path/to/repo", "--read-only=true"],
      "disabled": false,
      "autoApprove": [
        "git_status",
        "git_diff",
        "git_log",
        "git_show",
        "git_blame",
        "git_list_branches",
        "git_list_tags",
        "git_list_remotes",
        "git_grep",
        "git_search_history",
        "git_rev_parse",
        "git_config_get"
      ]
    }
  }
}
```

## 附录 B：常用 Git 操作 → MCP Tool 映射

| Git 命令 | MCP Tool |
|---|---|
| `git status` | `git_status` |
| `git diff` / `git diff --cached` | `git_diff` |
| `git log` | `git_log` |
| `git show` | `git_show` |
| `git blame` | `git_blame` |
| `git show ref:path` | `git_file_at_revision` |
| `git branch` | `git_list_branches` |
| `git tag` | `git_list_tags` |
| `git remote -v` | `git_list_remotes` |
| `git grep` | `git_grep` |
| `git log -S/-G` | `git_search_history` |
| `git add` | `git_stage` |
| `git restore --staged` | `git_unstage` |
| `git restore` | `git_restore` |
| `git commit` | `git_commit` |
| `git switch -c` | `git_branch_create` |
| `git switch` | `git_branch_switch` |
| `git branch -d` | `git_branch_delete` |
| `git stash` | `git_stash` |
| `git fetch` | `git_fetch` |

---

> 本文是 Git MCP Server 的实现依据。按 §10 里程碑（GM1–GM6）推进；
> 每个阶段完成后回填 §14 实施状态，并同步对应 README/接入说明。
> **GM1–GM6 已完成（2026-10-13，v0.1 收尾），实现记录见 §14.1，完整验收记录见
> `docs/GIT_MCP_ACCEPTANCE.md`；唯一未闭环项为真实 Cline GUI 冒烟（记录表待人工回填）。**
