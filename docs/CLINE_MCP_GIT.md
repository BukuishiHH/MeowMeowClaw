# Git MCP Server → Cline 接入指南（GM5）

> 适用：`meowmeowclaw_mcp_git` v0.1（GM1–GM5）。设计与里程碑见 `docs/GIT_MCP_DESIGN.md`。
> 本服务是**独立 stdio 进程**，不修改主 Agent 行为，也不读取项目 `.env`（配置走命令行参数或
> Cline 配置里的 `env`）。

---

## 1. 前置条件与安装

| 依赖 | 要求 | 检查命令 |
|---|---|---|
| Python | ≥ 3.10 | `python --version` |
| git | ≥ 2.30（建议 2.40+） | `git --version` |
| MCP SDK | 随可选依赖组安装 | `python -c "import mcp; print('mcp ok')"` |

在 MeowMeowClaw 源码目录安装（`mcp-git` 是可选依赖组，主项目运行时不引入）：

```bash
pip install -e ".[mcp-git]"
```

安装后自检（不依赖 Cline）：

```bash
python -m meowmeowclaw_mcp_git --version          # 打印版本到 stdout
python -m meowmeowclaw_mcp_git --help             # 查看全部启动参数
python -m meowmeowclaw_mcp_git --root /path/to/repo < /dev/null
# ↑ stdout 应为空(协议流), 启动/退出日志在 stderr; 配置错误会以退出码 2 + stderr 提示
```

`--root` 必须指向 **git 工作树顶层**（不是仓库子目录、不是 bare 仓库）；否则启动即失败并打印
`启动失败: ...`。

---

## 2. 五分钟接入（只读模式）

### 2.1 打开 Cline 的 MCP 配置

`cline_mcp_settings.json` 由 Cline 维护，不同版本/编辑器路径不同，**以 Cline UI 打开的路径为准**：
Cline 侧边栏 → **MCP Servers** → **Installed** → **Configure MCP Servers**（或 Edit MCP Settings）。

常见 VS Code 路径（仅供参考，以实际为准）：

| 系统 | 路径 |
|---|---|
| Linux | `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| macOS | `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| Windows | `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json` |

### 2.2 粘贴只读模板

模板文件：`docs/cline_mcp_settings.readonly.json`（可直接复制，替换两处占位符）：

1. `/path/to/python` → 已安装依赖的解释器**绝对路径**（`which python` / `where python`）；
2. `/path/to/repo` → 目标仓库的**绝对路径**。

```json
{
  "mcpServers": {
    "git": {
      "command": "/path/to/python",
      "args": ["-m", "meowmeowclaw_mcp_git", "--root", "/path/to/repo", "--read-only=true"],
      "disabled": false,
      "autoApprove": ["git_status", "git_diff", "git_log", "git_show", "git_blame", "git_file_at_revision",
                      "git_list_branches", "git_list_tags", "git_list_remotes", "git_rev_parse",
                      "git_config_get", "git_grep", "git_search_history"]
    }
  }
}
```

保存后回到 MCP Servers 面板点击 **Restart/Refresh**（或重载窗口）。Cline 会启动服务器并列出
13 个只读工具；上述工具都在 `autoApprove` 中，查询不会弹确认框。

### 2.3 验证

在 Cline 对话里让它：

- “用 git_status 看一下当前仓库状态”；
- “用 git_diff（stat_only=true）看看我改了什么”；
- “用 git_log 最近 5 条提交”。

预期：无需确认直接执行，返回结构化 JSON（`{ok:true,data:...,meta:...}`）。

---

## 3. 可写模式（需要人工确认）

模板文件：`docs/cline_mcp_settings.writable.json`。相对只读模式多了：

```json
"args": ["-m", "meowmeowclaw_mcp_git", "--root", "/path/to/repo",
         "--read-only=false", "--allow-remote=false",
         "--protected-branches", "main,master", "--timeout", "30"]
```

- `--read-only=false` 后额外注册 8 个写工具：`git_stage` / `git_unstage` / `git_restore` /
  `git_commit` / `git_branch_create` / `git_branch_switch` / `git_branch_delete` / `git_stash`；
- **`autoApprove` 里仍然只放 13 个只读工具**；写工具不在列表里，Cline 每次都会弹确认；
- 保护分支默认 `main,master`：直接 commit/删除会被拒绝（`protected_branch`），请在新分支上提交，
  或用 `--protected-branches` 调整；
- 危险开关必须显式传参且由 Cline 确认：`git_restore force=true`、`git_branch_switch force=true`
  （丢弃本地修改）、`git_branch_delete force=true`（强删未合并分支）、`git_commit amend/allow_empty`、
  `git_stash drop confirm=true`；
- dirty 工作区下 `git_branch_switch` / `git_restore` / `git_stash apply|pop` 默认拒绝
  （`dirty_worktree`），需要先提交/暂存或显式 `force`。

远端能力单独开关：把 `--allow-remote=false` 改成 `--allow-remote=true` 后出现 `git_fetch`
（仍不在 autoApprove 中，逐次确认）。fetch 只更新 remote-tracking refs，不合并/rebase。

---

## 4. 多仓库模式

```json
"args": ["-m", "meowmeowclaw_mcp_git",
         "--roots", "/srv/app:/srv/lib",
         "--repos", "web=/srv/app/frontend,api=/srv/lib/backend",
         "--read-only=true"]
```

- `--roots`（或环境变量 `MCP_GIT_ROOTS`）按平台分隔符切分（POSIX `:` / Windows `;`）；
- `--repos alias=path,...` 显式指定 alias；未配置时用根目录 basename 作为 alias；
- 工具调用只接受 alias 与**仓库内相对路径**，绝对路径、`..`、符号链接逃逸都会被拒绝
  （`invalid_path`）；
- 多仓库时每个工具都要带 `repo`（单个仓库可省略）。

---

## 5. 启动参数与环境变量

| 参数 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `--root PATH` | `MCP_GIT_ROOT` | 当前目录 | 单仓库模式根目录 |
| `--roots PATH1:PATH2` | `MCP_GIT_ROOTS` | 空 | 多根模式（按平台分隔符） |
| `--repos alias=path,...` | `MCP_GIT_REPOS` | 空 | 显式 alias → 路径 |
| `--read-only {true,false}` | `MCP_GIT_READ_ONLY` | `true` | `false` 才注册写工具 |
| `--allow-remote {true,false}` | `MCP_GIT_ALLOW_REMOTE` | `false` | 是否注册 `git_fetch` |
| `--protected-branches main,master` | `MCP_GIT_PROTECTED_BRANCHES` | `main,master` | 禁止直接 commit/删除的分支；空串=不保护 |
| `--timeout SECONDS` | `MCP_GIT_TIMEOUT` | `30` | 单条 git 命令超时 |
| `--max-output-bytes BYTES` | `MCP_GIT_MAX_OUTPUT_BYTES` | `131072` | 单工具输出硬上限（超出截断/报错） |
| `--lock-dir PATH` | `MCP_GIT_LOCK_DIR` | 空 | 跨进程仓库写锁目录（多进程/与子代理共享时配置） |
| `--log-level LEVEL` | `MCP_GIT_LOG_LEVEL` | `INFO` | stderr 日志级别 |
| `--audit-log PATH` | `MCP_GIT_AUDIT_LOG` | 空 | 尚未接入（GM6），当前只记录提示 |

在 Cline 配置里也可以用 `env` 传环境变量：

```json
"env": { "MCP_GIT_LOG_LEVEL": "DEBUG", "MCP_GIT_LOCK_DIR": "/path/to/repo/.git/mcp-git-locks" }
```

### 启动包装脚本（可选）

POSIX 环境可把 `command` 指向 `scripts/run_git_mcp_server.sh`，用 `MCP_GIT_PYTHON` 指定解释器：

```json
{
  "command": "/path/to/MeowMeowClaw/scripts/run_git_mcp_server.sh",
  "args": ["--root", "/path/to/repo", "--read-only=true"]
}
```

脚本内容即 `exec "$MCP_GIT_PYTHON" -m meowmeowclaw_mcp_git "$@"`（默认 `python3`）。Windows 请直接在
`command` 里写 `python.exe` 的绝对路径。

---

## 6. autoApprove 清单与原则

**只放只读工具**（本服务共有 13 个只读、8 个写、1 个远端）：

`git_status`、`git_diff`、`git_log`、`git_show`、`git_blame`、`git_file_at_revision`、
`git_list_branches`、`git_list_tags`、`git_list_remotes`、`git_rev_parse`、`git_config_get`、
`git_grep`、`git_search_history`。

- `git_stage`/`git_unstage`/`git_restore`/`git_commit`/`git_branch_*`/`git_stash`/`git_fetch`
  不要放进 `autoApprove`；代码层也不依赖 Cline 的 autoApprove（即使误配，服务端仍受
  `--read-only`、保护分支、dirty 检查约束）；
- 只读模式下写工具**根本不会注册**，列表里看不到。

---

## 7. 上下文预算使用建议

| 场景 | 建议 |
|---|---|
| 大工作区状态 | `git_status short=true`（只回计数）；需要文件列表再用 `short=false` |
| 看差异 | 先 `git_diff stat_only=true` 看 numstat，再按 `paths` 局部取 patch |
| 看历史 | `git_log max_count`；`has_more=true` 时用过滤条件收敛 |
| 看某个提交 | `git_show stat_only=true`，需要 patch 时放大 `max_bytes`（≤ `--max-output-bytes`） |
| 定位行 | `git_blame start_line/end_line`（默认前 200 行） |
| 大文件 | `git_file_at_revision` 默认 64KB，用 `max_bytes` 控制；二进制只回元信息 |
| 搜索 | `git_grep max_lines/max_bytes`；历史搜索 `git_search_history` |
| 截断信号 | 返回里 `truncated`/`has_more`/`output_truncated` 都表示结果不完整，需缩小范围 |

---

## 8. 排障

| 现象 | 排查 |
|---|---|
| Cline 看不到 `git` server | 配置 JSON 语法；`command` 是否为**绝对路径**；该解释器是否 `pip install -e ".[mcp-git]"`；保存后是否 Restart/重载 |
| Server 启动即退出 | 看 Cline MCP 面板的 stderr：`启动失败: ...`（退出码 2）。常见原因：`--root` 不存在/不是 git 工作树顶层/裸仓库 |
| 工具报 `repo_not_found` | alias 拼写；多仓库模式必须显式传 `repo`；检查 `--repos` 配置 |
| 工具报 `invalid_path` | 只接受仓库内相对路径；不要传绝对路径、`..` 或指向仓库外的符号链接 |
| 命令超时 `command_timeout` | 大仓/网络慢：调大 `--timeout`，或缩小 `max_count`/`paths`；fetch 检查网络与凭证助手 |
| 输出被截断 | `output_truncated`（无法保证完整性）或 `truncated:true`（按预算裁剪）：缩小查询范围，或调大 `--max-output-bytes` |
| 写操作被拒绝 | `policy_denied`：只读模式/未确认；`protected_branch`：保护分支；`dirty_worktree`：先提交/暂存或显式 force；`lock_timeout`：另一写操作持锁，稍后重试 |
| 提交没有任何反应 | `git_commit` 要求暂存区非空（或 `allow_empty=true`）；`git_stage` 的路径必须在仓库内 |
| fetch 报错含 URL | 返回已脱敏（userinfo → `***`）；凭证仍由本机 credential helper/.gitconfig 决定 |
| 中文乱码 | 服务端输出为 UTF-8 JSON；确认终端/Python 环境编码；文件名非法 UTF-8 时以替换字符展示 |
| `ModuleNotFoundError: mcp` | 未装可选依赖：`pip install -e ".[mcp-git]"`；`__main__` 会给出该提示并返回退出码 3 |

调试时把 `MCP_GIT_LOG_LEVEL=DEBUG`，日志只写 **stderr**（stdout 是 MCP 协议流，无任何日志）。

---

## 9. 安全边界与残余风险

- 默认只读：只读工具开放；写/远端工具分别需要 `--read-only=false` / `--allow-remote=true`；
- destructive 工具（reset --hard、clean、push、rebase）v1 不实现；
- 每仓库一把进程内写锁；配置 `--lock-dir` 可让多个 MCP Server 进程互斥（按仓库哈希命名锁文件）；
- URL/userinfo/token 查询参数在 remote/config/fetch 输出中统一脱敏；
- git hooks、credential helper、全局 `.gitconfig` 属于 Host 信任边界，服务不做沙箱；模型可组合多个
  合法写操作造成影响，依赖 Cline 逐次确认与审计；
- `--protected-branches` 只保护“直接 commit/删除”，不阻止新建分支后提交；
- 真实 Cline GUI 冒烟（工具列表/autoApprove/写确认）请按第 10 节执行并记录。

---

## 10. 人工冒烟验收清单（GM5/GM6）

在真实 Cline 中依次验证并记录（版本/系统/配置文件路径/截图或日志）：

- [ ] MCP Servers 面板出现 `git`，状态为已连接；
- [ ] 工具列表包含 13 个只读工具（只读模式：不含任何写工具与 `git_fetch`）；
- [ ] `git_status` / `git_diff` / `git_log` 在 autoApprove 下**不弹确认**且返回结构化 JSON；
- [ ] 可写模式下列表出现 8 个写工具，调用 `git_commit` 等**弹出确认**，确认后提交成功；
- [ ] 保护分支（main）上 commit 被拒绝并提示 `protected_branch`；
- [ ] dirty 工作区下 `git_branch_switch` 被拒绝（`dirty_worktree`），`force=true` 时按预期生效；
- [ ] `--allow-remote=true` 后出现 `git_fetch`，fetch 后 `git_log`/`git_list_branches` 能看到远端 ref 变化；
- [ ] 让 Cline 使用 Prompt（`write_commit_message`/`review_staged_changes`）生成可用的提示词/tool 调用；
- [ ] 读取 Resource（`git://<repo>/status`、`git://<repo>/file/HEAD/<path>`）内容正确；
- [ ] 配置错误（错 root/缺依赖）时，Cline 面板 stderr 有明确提示且服务器退出码可解释。

验收记录回填到 `docs/GIT_MCP_DESIGN.md` §14.1。
