# Git MCP Server v0.1 验收记录（GM6）

> 日期：2026-10-13　范围：GM1–GM6（含 GM6.1 补丁）　关联：`docs/GIT_MCP_DESIGN.md` §10.7/§14、`docs/CLINE_MCP_GIT.md`
>
> **2026-10-13 补丁**：修复 git ≥2.46 下 `git_fetch` 误报远端 HEAD 符号引用（CI 两条 Python 腿失败根因），
> 详见设计文档 §14.1 GM6 补丁记录；本记录中的测试数字为补丁后重跑结果。
> 结论：**GM1–GM6 交付完成，自动验收全绿**；唯一未闭环项是“真实 Cline GUI 冒烟”（本环境无 Cline，
> 记录表见 §5，人工执行后回填）。

---

## 1. 验收环境

| 项 | 值 |
|---|---|
| 系统 | Linux 6.18.40.1-microsoft-standard-WSL2 x86_64 |
| Python | 3.11.15（conda env `AI_chat`） |
| git | 2.43.0 |
| MCP SDK | `mcp` 1.29.1 |
| 安装方式 | `pip install -e ".[dev,mcp-git]"`（与 CI 一致） |

## 2. 自动验收结果

| 检查 | 命令 | 结果 |
|---|---|---|
| 全量测试 | `pytest -q` | **2136 passed / 6 skipped / 2 xfailed**（含 2026-10-13 补丁后重跑） |
| 静态检查 | `ruff check meowmeowclaw meowmeowclaw_mcp_git tests` | 通过（E9+F 硬错误） |
| 端到端验收 | `python scripts/verify_git_mcp_server.py` | **24/24 PASS**（见 §3） |
| Git MCP 专项 | `pytest tests/mcp_git -q` | **447 passed**（GM1–GM6.1） |
| 协议能力 | `pytest tests/mcp_git/test_mcp_git_protocol.py -q` | 通过：真 SDK stdio 握手、tools/prompts/resources、stdout 纯净性 |
| Cline 模板 | `pytest tests/mcp_git/test_mcp_git_cline_config.py -q` | 通过：模板参数真实启动服务器，工具列表与 `--read-only` 一致 |
| 审计 | `pytest tests/mcp_git/test_mcp_git_audit.py -q` | 通过：JSONL/0600/参数摘要/失败 fail-soft |

## 3. 端到端脚本结果（`scripts/verify_git_mcp_server.py`）

临时仓库 + 本地 bare remote，真实 stdio 会话，24/24 通过：

| 阶段 | 验证点 |
|---|---|
| 只读模式 | 13 个工具；写/远端工具不可见；`git_status` 分支与未暂存文件；`git_diff` numstat；`git_log`；`write_commit_message` prompt；`head`/`file` resource |
| 可写模式 | 21 个工具；`git_stage` → `git_commit` 成功且提交真实落库；审计 JSONL 写入、含 tool/status/args_digest/exit_code、不落盘 message、文件权限 0600 |
| 远端模式 | 22 个工具；`git_fetch` 更新 `origin/main`（`refs_changed` 含 updated）且**不合并**本地分支 |
| stdout 纯净性 | 空 stdin 启动退出码 0、stdout 0 字节、stderr 有启动/退出日志 |
| 残留进程 | 退出后无 `meowmeowclaw_mcp_git` 残留进程 |

## 4. 目标（G1–G7）与里程碑验收

### 4.1 设计目标

| # | 目标 | 验收证据 |
|---|---|---|
| G1 | 完整结构化 Git 工具集 | 13 只读 + 8 写 + fetch；numstat/log/refs/grep/blame/porcelain v2 解析（`tests/mcp_git/`） |
| G2 | 默认安全 | 默认 13 只读、写/远端不注册；保护分支/dirty/force 显式；destructive 不实现 |
| G3 | 输出可被模型消费 | 统一 `{ok,data,meta}`；`truncated`/`has_more` 与预算裁剪；app 层 `--max-output-bytes` 兜底 |
| G4 | Cline 集成顺畅 | 两套模板 + 接入指南 + 模板启动一致性自动校验（GM5） |
| G5 | 并发可控 | 每仓库 `asyncio.Lock` + `--lock-dir` 跨进程 flock；并发 stage 后 index 完整 |
| G6 | 可测试可回归 | Git MCP 447 用例、全量 2136 passed、ruff 通过、脚本不依赖网络 |
| G7 | 不污染主项目 | 独立顶层包 + 可选依赖组 `mcp-git`；`mcp` 只在 `server.py` 导入；主项目用例回归通过 |

### 4.2 里程碑

| 里程碑 | 验收项 | 结果 |
|---|---|---|
| GM1 | 真 SDK 握手/工具列表/`git_status`；stdout 无污染；默认只读 | ✅ 真 stdio 会话 + `test_mcp_git_protocol.py` |
| GM2 | 13 只读工具齐全、结构化、输出预算 | ✅ `test_mcp_git_read_tools.py` 等 |
| GM3 | 写工具默认关闭、dirty/保护分支/锁生效、before/after | ✅ `test_mcp_git_write_tools.py` / `test_mcp_git_write_concurrency.py` |
| GM4 | fetch 默认关闭、prompts/resources 可用 | ✅ `test_mcp_git_fetch_tool.py` / `test_mcp_git_prompts.py` / `test_mcp_git_resources.py` |
| GM5 | 配置模板/接入指南/启动包装；“只读模式写工具不可见” | ✅ `test_mcp_git_cline_config.py`（GUI 冒烟见 §5） |
| GM6 | 全量 pytest+ruff、端到端脚本、审计、已知限制 | ✅ 本记录 §2/§3/§6 |

### 4.3 模式矩阵（`read_only` × `allow_remote`）

| 模式 | 工具数 | 内容 |
|---|---|---|
| 默认 | 13 | 只读工具；写/远端不注册 |
| `--allow-remote=true` | 14 | + `git_fetch` |
| `--read-only=false` | 21 | + 8 写工具 |
| 两者都开 | 22 | 全量 |

## 5. Cline 人工冒烟记录（待执行）

本环境无法启动 Cline GUI。配置模板、指南与“模板参数真实启动服务器”的一致性已自动校验；
请在真实 Cline 中按 `docs/CLINE_MCP_GIT.md` §10 执行以下清单并回填：

| # | 检查项 | 结果 | 备注/日志 |
|---|---|---|---|
| 1 | MCP Servers 面板出现 `git` 且已连接 | 待执行 | |
| 2 | 只读模式工具列表=13（无写工具与 `git_fetch`） | 待执行 | |
| 3 | `git_status`/`git_diff`/`git_log` autoApprove 不弹确认且返回结构化 JSON | 待执行 | |
| 4 | 可写模式列表=21，`git_commit` 等弹出确认，确认后提交成功 | 待执行 | |
| 5 | 保护分支上 commit 被拒绝（`protected_branch`） | 待执行 | |
| 6 | dirty 下 `git_branch_switch` 被拒绝；`force=true` 按预期生效 | 待执行 | |
| 7 | `--allow-remote=true` 后 `git_fetch` 可用且 refs 更新 | 待执行 | |
| 8 | Prompt/Resource 在 Cline 中可选用且内容正确 | 待执行 | |
| 9 | 配置错误时 stderr 提示明确、退出码可解释 | 待执行 | |

> 记录格式建议：`Cline 版本 | 编辑器/系统 | cline_mcp_settings.json 路径 | 每项结果 | 截图或日志片段`。

## 6. 发布说明与已知限制（v0.1.0）

**发布内容**

- 独立包 `meowmeowclaw_mcp_git`（入口 `python -m meowmeowclaw_mcp_git`），可选依赖组
  `mcp-git = ["mcp>=1.0,<2"]`；
- 22 个工具（13 只读 / 8 写 / 1 fetch）、4 个 Prompts、2 类静态 Resources + 2 个 Resource Templates；
- 默认只读；`--read-only=false` 开写、`--allow-remote=true` 开 fetch；
- 最小审计：`--audit-log` 输出 JSONL（工具调用、参数摘要、状态、耗时、截断标记，文件 0600）；
- Cline 模板/指南/启动包装见 `docs/CLINE_MCP_GIT.md`。

**已知限制**

| # | 限制 | 说明 |
|---|---|---|
| 1 | 无 push/pull/rebase/merge/cherry-pick | v2 能力；v1 只有 `git_fetch`（不合并） |
| 2 | 无 worktree/submodule 工具 | §6.5 可选能力，未排期 |
| 3 | 审计为最小实现 | 只记工具调用（不含 prompts/resources）、只存参数摘要、无轮转/大小上限、无查询接口 |
| 4 | 跨进程锁未与主项目共用同一文件 | 按 repo 哈希命名；同一 `--lock-dir` 的多个 MCP Server 可互斥，与 `WorkspaceMutationGuard` 完全打通留待后续 |
| 5 | 非 UTF-8 文件名/路径 | 以替换字符展示；`git_file_at_revision` 依次尝试 UTF-8/GB18030，其他视为二进制 |
| 6 | 输出预算默认值 | diff/show patch 默认 32KB、blame 200 行、grep 100 条、文件 64KB；大结果需分段或调参 |
| 7 | 仅 stdio 单客户端 | 无 HTTP/TLS/鉴权、无多客户端并发会话 |
| 8 | Windows 未实机验证 | 代码含 Windows 分支（taskkill/msvcrt），CI 只覆盖 Linux |
| 9 | Cline GUI 冒烟未执行 | 见 §5 记录表 |

**残余风险**（与设计 §6.6 一致）

- git hooks、credential helper、全局 `.gitconfig` 属于 Host 信任边界，本服务不做沙箱；
- fetch 可能引入大对象/慢网络，依赖超时与用户网络策略；
- 模型可用多次合法小操作组合出风险行为，依赖 Cline 逐次确认与本审计记录；
- 保护分支只拦截“直接 commit/删除”，不阻止新建分支后提交。

## 7. 回滚与排障

- 关闭集成：从 `cline_mcp_settings.json` 删除 `mcpServers.git` 或置 `"disabled": true`；
- 只读回退：`--read-only=true`（默认）；关闭远端：`--allow-remote=false`（默认）；
- 常见错误码与排查见 `docs/CLINE_MCP_GIT.md` §8；设计文档见 `docs/GIT_MCP_DESIGN.md`。
