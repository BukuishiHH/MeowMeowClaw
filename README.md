# MeowMeowClaw

依据 OpenClaw 思路实现的自定义 Agent -- **不依赖 LangChain / LangGraph 等编排框架**, 用一个显式的"模型 ↔ 工具"循环驱动.

- **技术栈**: Python 3.10+; 运行时依赖 `openai`(AsyncOpenAI) / `python-dotenv` / `httpx` / `pyyaml`, 联网工具另需 `ddgs`、`html2text`
- **代码规模**: 17 个源码模块 / 约 2400 行; 测试 14 个文件 / **613 个用例**(601 passed + 6 skipped + 6 xfailed)
- **协议**: OpenAI Chat Completions + function calling, 任何兼容服务(DeepSeek / 通义 / vLLM / Ollama / One-API)改 `base_url` 即可接入

---

## 1. 快速开始

```bash
pip install -e ".[dev]"                  # 安装依赖 + 开发工具; 仅运行可去掉 [dev]

cp .env.example .env
export DEEPSEEK_API_KEY=sk-xxxx          # .env 里用 ${DEEPSEEK_API_KEY} 引用
python -m meowmeowclaw                   # 安装后也可以直接运行 meowmeowclaw
```

### 交互命令

| 命令 | 作用 |
| --- | --- |
| 直接输入 | 交给 Agent 处理 |
| `/exit`(`/quit` `/q`) | 退出 |
| `/clear` | 清空对话历史与工具调用记录 |
| `/tools` | 查看已注册工具 |

### 配置项(`.env`, 与 `meowmeowclaw/` 同级)

优先级: **系统环境变量 > .env 文件 > 代码默认值**. 相对路径一律按**项目根**解析, 不随当前工作目录漂移.

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `model` | `deepseek-chat` | 模型名 |
| `api_key` | 空(启动即提示并退出码 1) | 支持 `${ENV_VAR}` 引用系统环境变量 |
| `base_url` | `https://api.deepseek.com` | OpenAI 兼容服务地址 |
| `workspace` | `<项目根>/workspace` | 留空或写 `.` 均表示该预设; 可写绝对路径覆盖; 启动装配时自动创建 |
| `max_iterations` | `32` | 单轮"模型↔工具"往返上限; 非法值回退默认 |

> 人设文件固定为项目根 `identity.md`，随仓库提供，不再通过 `.env` 配置。

### 内置工具(默认注册 6 个 + `load_skill`; 内置技能始终存在)

| 工具 | 能力 | 关键限制 |
| --- | --- | --- |
| `read_file` | 读工作区内文本文件 | 越界拦截; 超过 16000 字符截断 |
| `write_file` | 写文件(自动建父目录) | 越界拦截 |
| `list_dir` | 列目录(目录加 `/`、文件带字节大小、按名排序) | 越界拦截 |
| `exec` | 在工作区执行 Shell 命令 | 60 秒超时; 危险命令黑名单; 输出 10000 字符截断 |
| `web_search` | DuckDuckGo 联网搜索 | 默认 5 条(上限 20); 20 秒超时; 输出 8000 字符截断 |
| `web_fetch` | 抓取 URL 并转纯文本 | 仅 http/https; **拒绝内网/回环地址**; 15 秒超时; 输出 12000 字符截断 |
| `load_skill` | 按名取回技能指南正文 | **仅发现技能时注册**; 未知名会回列可用技能; 16000 字符截断 |

> - 联网工具在受限网络(如国内直连)下需先配代理, 见 [第 6 章](#6-联网能力与代理配置wsl2--clash);
> - 技能系统(SKILL.md 目录约定、渐进式披露、`load_skill` 链路)见 [第 4 章](#4-技能系统skills).

---

## 2. 构建框架: 四层 + 一个循环

```
┌────────────────────────────────────────────────────────────────────────────┐
│ 入口 / 交互层      main.py                                                 │
│   build_agent() 装配 6 工具(+技能时加 load_skill) · 技能摘要 · REPL        │
└──────────────────────────────────────┬─────────────────────────────────────┘
                                       │                                      
┌──────────────────────────────────────▼─────────────────────────────────────┐
│ 控制流层           agent/loop.py :: AgentLoop                              │
│   run(): for 最多 max_iterations 轮「模型 ↔ 工具」                         │
│     chat → 有 tool_calls ? 执行工具并回填 → continue : 返回最终回答        │
│   护栏: _check_tool_loop(重复 10 次警告 / 20 次熔断) · clear_history()     │
└───────────┬─────────────────────────┬──────────────────────────┬───────────┘
            │                         │                          │            
┌───────────────────────┐ ┌───────────────────────┐ ┌────────────────────────┐
│ 提示词层              │ │ 能力层  tools/ (6+1)  │ │ 模型接入层             │
│ context.py            │ │ 本地: 读/写/列目录    │ │ providers/             │
│ ContextBuilder        │ │ 执行: exec(黑名单)    │ │ LLMProvider (ABC)      │
│ build_system_prompt() │ │ 联网: search / fetch  │ │ OpenAICompatProvider   │
│ build_messages()      │ │ 技能: load_skill      │ │ → AsyncOpenAI          │
│ 人设+时间+工作区      │ │ → OpenAI function     │ │ 异常 → error 响应      │
│ + 记忆 / 技能摘要     │ │ 越界 / SSRF 防护      │ │ 可换任意兼容服务       │
└───────────────────────┘ └───────────────────────┘ └────────────────────────┘
                                                                 │            
┌────────────────────────────────────────────────────────────────▼───────────┐
│ 配置层             config.py :: Settings + load_config()                   │
│   paths.py 唯一定位项目根/workspace/人设; .env 解析无副作用               │
│   workspace 由 main.build_agent() 创建; 技能目录 = <包>/skills/builtin    │
└────────────────────────────────────────────────────────────────────────────┘
```

**一次 `run()` 的数据流**

1. `ContextBuilder.build_messages(history, user_message)` → `[system] + 历史 + [本次用户消息]`
2. `provider.chat(messages, tools=registry.get_definitions(), model)` → `LLMResponse`
3. 若 `has_tool_calls`: 把 assistant(`tool_calls`) 追加进 messages → 逐个执行 `registry.execute(name, arguments)` → 结果以 `role="tool"` 回填 → 回到第 2 步
4. 若无工具调用: 追加最终 assistant 消息 → 整轮写入 `_session_history` → 返回文本
5. 超过 `max_iterations` 返回超时提示; `finish_reason=="error"` 返回错误文本

**依赖方向单向**: `main → loop → context / tools / providers → config`, 无循环依赖.

---

## 3. 各组成部分

| 模块 | 行数 | 职责 | 关键 API |
| --- | ---: | --- | --- |
| `meowmeowclaw/config.py` | 141 | 读 `.env`、校验兜底, 纯解析不产生副作用 | `load_config()` / `Settings` |
| `meowmeowclaw/paths.py` | 40 | 项目根 / `.env` / workspace / 人设路径的唯一来源 | `PROJECT_ROOT` / `resolve_workspace()` / `IDENTITY_FILE` |
| `meowmeowclaw/providers/base.py` | 65 | LLM 接入抽象 + **统一数据契约** | `LLMProvider.chat()`、`LLMResponse`、`ToolCallRequest`、`FINISH_REASON_*` |
| `meowmeowclaw/providers/openai_compat.py` | 207 | OpenAI 兼容实现(异常包装为 `finish_reason="error"`) | `OpenAICompatProvider` |
| `meowmeowclaw/agent/tools/base.py` | 91 | 工具抽象, 产出 OpenAI function 定义 | `BaseTool` |
| `meowmeowclaw/agent/tools/registry.py` | 57 | 注册、查重、定义查询、按名路由执行 | `ToolRegistry` |
| `meowmeowclaw/agent/tools/filesystem.py` | 163 | 本地文件读写/列目录 | `ReadFileTool` / `WriteFileTool` / `ListDirTool` |
| `meowmeowclaw/agent/tools/shell.py` | 196 | 工作区内执行命令(黑名单 + 进程组清理) | `ExecTool` |
| `meowmeowclaw/agent/tools/web_search.py` | 139 | DuckDuckGo 搜索(同步库丢线程池) | `WebSearchTool` |
| `meowmeowclaw/agent/tools/web_fetch.py` | 248 | 网页抓取 → html2text → 清理(SSRF 防护) | `WebFetchTool` |
| `meowmeowclaw/skills/loader.py` | 237 | 技能扫描/索引/摘要(importlib.resources + 启动扫描一次) | `SkillCatalog` |
| `meowmeowclaw/skills/models.py` | 33 | 技能数据模型与配置错误 | `Skill` / `SkillConfigError` |
| `meowmeowclaw/skills/tool.py` | 85 | `load_skill` 工具(技能子系统对 Agent 的唯一出口) | `LoadSkillTool` |
| `meowmeowclaw/agent/context.py` | 150 | 组装 System Prompt 与 messages(人设路径显式注入) | `ContextBuilder.build_system_prompt()` / `build_messages()` |
| `meowmeowclaw/agent/loop.py` | 241 | **控制流**: 多轮往返、防爆护栏、会话历史 | `AgentLoop.run()` / `clear_history()` |
| `meowmeowclaw/main.py` | 215 | 入口: 装配(工具/技能/提示词) + 命令行交互 | `build_agent()` / `interactive_loop()` / `main()` |

### 3.1 契约先行, 实现可换

- **Provider 契约**: `LLMProvider.chat(messages, tools, model) -> LLMResponse`. 上层只认 `LLMResponse`, 不关心背后是 OpenAI、DeepSeek 还是本地 vLLM.
- **工具契约**: `BaseTool` 同时是"给模型看的 JSON Schema"(`to_function_definition()`)与"给人写的可执行体"(`execute()`).
- **消息契约**: 全链路 OpenAI 消息格式, `tool_calls.function.arguments` 是 JSON 字符串.

### 3.2 统一错误策略:"异常不炸主循环"

| 层 | 策略 |
| --- | --- |
| Provider | 捕获异常 → `LLMResponse(content="[LLM调用失败] ...", finish_reason="error")`(`CancelledError` 等 `BaseException` 继续上抛) |
| 工具 | 一律返回可读文本; `ToolRegistry.execute` 再兜一层 |
| Loop | `finish_reason=="error"` 立即返回; 空 `choices`、工具名不存在等都变成文本回流给模型 |
| 交互层 | 单次失败只打印 `[异常]`, 会话继续 |

好处是**模型能看到失败原因并自我纠正**(工具参数非法、工具名写错、命令被拦截等), 而不是让进程崩掉.

### 3.3 循环护栏

`_check_tool_loop(tool_name, args_json)` 以"工具名 + 入参 JSON"为签名, 在长度 30 的滑动窗口内统计重复次数:

- 重复 ≥ 10 次 → **警告**: 跳过本次执行, 回填 `[SYSTEM_ERROR] ...` 提示模型换思路
- 重复 ≥ 20 次 → **熔断**: 结束本轮并返回熔断说明
- 窗口内无论是否告警都记账, 否则计数会停在 10 导致熔断阈值永远不可达

### 3.4 会话状态

- `_session_history`: 跨轮次对话历史, **只保存跑完整的一轮**; 错误/熔断/超时这类半截过程不写入.
- `_tool_call_history`: 防爆签名滑窗, `/clear` 时一并清空.
- `reasoning_content`(推理模型思考过程)保留在 `LLMResponse` 上, **不回填 messages** -- DeepSeek 等要求多轮时不得回传.

---

## 4. 技能系统(Skills)

**技能 = 一段按需加载的"怎么做"指南**(`SKILL.md`)。平时只把「技能名 + 一句话描述」放进 System Prompt,
模型判断与当前任务相关时, 再用 `load_skill` 工具把指南正文取回来 —— 这就是**渐进式披露
(progressive disclosure)**: 既让模型知道"有哪些能力", 又不把长篇操作手册一次性塞进上下文.

### 4.1 目录约定

```
meowmeowclaw/skills/builtin/       # 内置技能随包发布(入 git / 随 wheel)
├── exec/SKILL.md
├── read_file/SKILL.md
└── ...                            # 每个子目录一个技能, 只扫描一层
```

> 技能不再放在 `workspace/` 下: `workspace/` 只保留 Agent 运行时产物, 内置技能随代码分发,
> 保证全新克隆/安装后技能一定存在。

`SKILL.md` 用 YAML frontmatter 描述元信息, 正文就是给模型看的指南:

```markdown
---
name: exec
description: 在工作区内执行 Shell 命令的用法与安全限制
---
# exec 使用指南
## 何时使用 ...
## 参数 ...
## 安全限制 ...
```

### 4.2 装配与执行链路

| 环节 | 实现 |
| --- | --- |
| 扫描 / 解析 | `skills/loader.py :: SkillCatalog`(importlib.resources、启动扫描一次、坏 YAML 跳过、重名报错) |
| 注入提示词 | `main.build_agent()` 把 `catalog.summary()` 传给 `ContextBuilder(skills_summary=...)`, `build_system_prompt()` 追加 `## 可用技能` 章节 |
| 按需取回 | `skills/tool.py :: LoadSkillTool`(**有技能时才注册**), 模型调用 `load_skill(name="exec")` |
| 技能不存在 | 返回 `[错误] 未找到技能: xxx. 可用技能: ...`, 把可用名字回给模型便于自纠 |
| 越界防护 | 先按名**精确查内存索引**, 未知名直接返回错误, 不拼接路径、不触碰文件系统 |

System Prompt 里最终长这样:

```
## 可用技能
你有以下技能可用. 当某项技能与当前任务相关时, 请调用 load_skill 工具并传入技能名, 获取该技能的详细指南.

可用技能:
- exec (exec/SKILL.md): 在工作区内执行 Shell 命令的用法与安全限制
- read_file (read_file/SKILL.md): 读取工作区内文本文件的用法与限制
```

### 4.3 自带技能

仓库在 `meowmeowclaw/skills/builtin/` 下自带 6 个"工具用法"技能(`read_file` / `write_file` /
`list_dir` / `exec` / `web_search` / `web_fetch`), 把每个工具的参数、限制(截断 / 超时 / 命令黑名单 /
SSRF 防护)与推荐用法写成指南 —— 技能随代码入库、随包分发, 克隆/安装后必然可用.

### 4.4 边界

- 技能目前**只是文本**, 不含可执行脚本与随附资源(不同于 Claude Skills 的 `scripts/` 目录);
- 只扫描 `skills_dir` 的**一层子目录**, 不支持分组嵌套;
- `load_skill` 返回的正文超过 16000 字符会截断。

## 5. 安全设计

面向"通用型助手"的定位, 把风险点都做了显式处理, **并如实标注了边界**.

### 5.1 路径防护(文件三件套)

所有文件操作先 `os.path.abspath` 归一化, 再校验是否落在工作区内, 越界返回 `[安全拦截] ...`.

### 5.2 命令执行防护(`exec`)

17 条正则黑名单(`re.IGNORECASE`)覆盖递归删除 / 格式化 / 关机重启 / 提权 / 覆盖设备文件 / 下载即执行 / 反弹 shell / Fork 炸弹等; 命中即返回 `安全拦截: 检测到危险命令模式 '...'`, **不创建任何进程**. 超时清理优先杀**整个进程组**(`start_new_session` + `killpg`), 并带**自杀保护**: 若子进程与父进程同组则退化为杀单进程, 避免误杀调用方.

### 5.3 网络访问防护(`web_fetch`)

- 仅允许 `http`/`https`;
- **默认拒绝本机/内网目标**: 字面量 IP、`localhost`/`*.localhost`、以及**域名解析后的地址**都要过 `not ip.is_global` 判据(覆盖回环、私网、链路本地、CGNAT/Tailscale、保留、组播等);
- 需要抓内网时显式传 `WebFetchTool(allow_private=True)`;
- 附 `<meta charset>` 嗅探、非 HTML 原样返回、2MB 转换上限.

### 5.4 其它

- `Settings.__repr__` 与启动信息**掩码 `api_key`**, 有专门用例断言密钥不出现在输出里;
- 所有工具输出都有长度上限(见第 1 章表格), 避免上下文被单次调用打爆;
- `.env` 已在 `.gitignore` 中, 模板用 `${ENV_VAR}` 而不是明文密钥.

### 5.5 已知缺口与局限(诚实清单)

| 位置 | 问题 | 影响 |
| --- | --- | --- |
| `filesystem.py` | 越界校验用 `str.startswith` | 同前缀兄弟目录(`/ws_evil`)可绕过, 应改 `os.path.commonpath` |
| `filesystem.py` | 路径拼接在 `try` 之外 | 传 `null` 之类的非字符串会抛 `TypeError`(registry 会兜住) |
| `shell.py` | 黑名单是**护栏不是沙箱** | `rm --recursive -f`、`find -delete`、`python -c`、`base64\|sh` 等可绕过; 也会误报(`git commit -m "fix rm -rf bug"`) |
| `web_fetch.py` | 只做**发起前**检查 | `follow_redirects=True` 时远端 302 跳内网仍会发出请求; 存在 DNS 重绑定窗口 |
| `providers/base.py` | `has_tool_calls` 直接 `len()` | `tool_calls=None` 时抛 `TypeError`; `usage` 类型标注缺 `Optional` |

以上前 5 条都以 `xfail` 形式记录在测试里, 修复后会转为 XPASS. **生产建议**: `exec` 默认不注册或加开关、放进容器/受限用户运行; 把工作区当不可信输入.

---

## 6. 联网能力与代理配置(WSL2 + Clash)

`web_search` / `web_fetch` 需要出网. 若 Linux 侧不能直连(DNS 污染、无路由), 可通过 Windows 上运行的 Clash 代理解决. 以 **WSL2 mirrored 网络模式 + Clash Verge** 为例:

```bash
# Windows: 在 %UserProfile%\.wslconfig 中启用 mirrored, 然后 wsl --shutdown
#   [wsl2]
#   networkingMode=mirrored
# 之后 WSL 里 127.0.0.1 即 Windows 本机, 可直接访问 Clash 的混合端口(默认 7897)

export HTTPS_PROXY="http://127.0.0.1:7897" HTTP_PROXY="http://127.0.0.1:7897"
export NO_PROXY="127.0.0.1,localhost,::1,*.local,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
```

- 无需改任何代码: 底层的 `primp`(ddgs) 与 `httpx` 都会自动读取 `HTTPS_PROXY`;
- **不要设 `ALL_PROXY=socks5://...`**: httpx 未安装 `socksio` 时会直接报错, 会连累 LLM 调用链路;
- NAT 模式下 Windows 主机是网关 IP(`ip route` 默认网关)且**重启会变**, mirrored 模式才能稳定用 `127.0.0.1`.

验证:

```bash
curl -x http://127.0.0.1:7897 -o /dev/null -w '%{http_code}\n' https://duckduckgo.com/
RUN_NETWORK_TESTS=1 pytest tests/ -m network -q     # 真实联网用例
```

---

## 7. 与"用 DAG / 图编排构建的 Agent"的区别

以 LangGraph 这类"节点 + 边"的图编排框架为对照:

| 维度 | 本项目(命令式 ReAct 循环) | DAG / 图编排(如 LangGraph) |
| --- | --- | --- |
| **控制流归属** | 写在代码里: `AgentLoop.run()` 的一个 `for` 循环 | 声明在图里: 节点 + 边, 由**引擎**推进 |
| **分支路由** | 由**模型输出**(`tool_calls`)在运行时动态决定 | 由开发者预先声明, `conditional_edge` 依据 State 选择分支 |
| **循环** | 普通 Python 循环(`for` / `continue`) | 回边(back edge)形成环, 图里显式画出来 |
| **状态组织** | 一个 `messages: list[dict]` 直接传递 | 全局 `State` + `reducer` 合并规则 |
| **护栏** | 直接写在循环里(`_check_tool_loop` + `max_iterations`), 工具自身还带黑名单/SSRF 防护 | 需要额外插入 guard 节点或中间件 |
| **依赖** | 仅 openai + dotenv + httpx(+ddgs/html2text) | 引入编排框架及其概念栈 |
| **可观测 / 可视化** | 无内建; 靠日志与断点 | 图结构可可视化, 常见内置 tracing 集成 |
| **持久化 / 断点续跑** | 无内建(`_session_history` 在内存里) | 内建 checkpointer, 可从任意节点恢复 |
| **人工审批(HITL)** | 需自行在循环里加确认步骤 | 一等公民: 中断/恢复 API |
| **多 Agent 编排** | 需自己写调度(子 Agent 即另一个 `AgentLoop`) | 天然支持 supervisor / swarm 等拓扑 |
| **测试方式** | 换掉 `LLMProvider` 一个替身即可跑全链路(本项目 536 用例) | 通常要驱动图运行时, 或按节点分别测 |
| **调试体验** | 断点就在 `run()` 里, 栈短、易读易改 | 需要在框架抽象层之间跳转 |
| **适合场景** | 单 Agent + 工具调用的主线业务; 想快速看懂/改控制流 | 复杂分支编排、长流程、需要暂停恢复与人工介入 |

**一句话概括**: DAG 是"把控制流**画出来**交给引擎跑", 本项目是"把控制流**写成代码**自己跑". 本质差别是 **控制流归属**(引擎 vs 代码)与 **状态组织**(State + reducer vs messages 列表). 本项目的取舍是**用可读、可测、零依赖换取了编排能力**.

**演进建议**: 若后续需要"并行工具调用 / 子 Agent 分工 / 人工审批 / 断点续跑", 把 `AgentLoop.run()` 内的循环替换成图编排是自然路径; 由于 `LLMProvider`、`BaseTool` 都是抽象基类且数据契约独立, **替换编排层不需要改动 Provider、工具与配置层**. 可先用"双轨 + 配置开关 + 双引擎等价测试"灰度过渡.

---

## 8. 扩展指南

**加一个工具**(3 步)

```python
class HttpGetTool(BaseTool):
    @property
    def name(self) -> str: return "http_get"
    @property
    def description(self) -> str: return "抓取网页文本, 需要联网查资料时调用"
    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"url": {"type": "string"}},
                "required": ["url"], "additionalProperties": False}
    async def execute(self, **kwargs) -> str: ...

registry.register(HttpGetTool())     # 注册后模型即可见
```

**换模型 / 换服务**: 只改 `.env` 的 `model` + `base_url`; 非 OpenAI 协议则实现一个 `LLMProvider` 子类.
**换编排**: `AgentLoop` 是独立一层, 可整体替换, `main.py` 只改装配代码.

---

## 9. 测试

```bash
pytest                                        # 全量: 601 passed, 6 skipped, 6 xfailed
pytest tests/test_loop.py -v
RUN_NETWORK_TESTS=1 pytest -m network -v      # 仅真实联网用例
```

策略: **Mock 为主、真实实现对照为辅**, 并用**变异测试**验证用例有效性(每轮改动都跑过 10~20 个变异体, 确认全被捕获).

| 测试文件 | 用例 | 重点 |
| --- | ---: | --- |
| `test_base_tool.py` | 3 | 工具基类抽象约束 |
| `test_tool_registry.py` | 19 | 注册/查重/路由/异常包装 |
| `test_filesystem.py` | 76 | 三个文件工具的读写、截断、路径穿越、异常分支 |
| `test_shell.py` | 91 | 黑名单 17 条、进程组清理与自杀保护、超时、输出拼装 |
| `test_web_search.py` | 44 | 结果格式化、条数归一化、超时、线程池执行、联网开关(4) |
| `test_web_fetch.py` | 71 | SSRF 12 类目标、`async with` 关连接、UTF-8/GBK、截断、联网开关(2) |
| `skills/test_catalog.py` | 36 | frontmatter 边界、索引/摘要、坏 YAML 跳过、重名报错、内置资源可发现 |
| `skills/test_tool.py` | 30 | `load_skill` 契约、自纠提示、截断、与引导语口径一致 |
| `test_context.py` | 47 | 人设查找链(工作区→项目根兜底)、时间实时取值、记忆/技能章节 |
| `test_provider_base.py` | 48 | 数据契约(默认值、可变默认、`has_tool_calls`) |
| `test_openai_compat.py` | 44 | 请求参数、tool_calls 转换、usage、异常兜底 |
| `test_loop.py` | 35 | 消息格式、防爆阈值、历史写入策略、配置驱动默认值 |
| `test_main.py` | 27 | 装配(工具/技能)、命令分支、Ctrl+C 优雅退出 |
| `test_config.py` | 43 | 取值优先级、路径解析、非法值兜底、密钥掩码 |

---

## 10. 目录结构

```
MeowMeowClaw/
├── .env / .env.example          # 配置与模板(密钥不入库)
├── pyproject.toml               # 依赖 / 控制台入口 / pytest 配置
├── identity.md                  # 人设文件(固定放项目根)
├── workspace/                   # 运行时工作区(自动创建, gitignore; 可被 .env 绝对路径覆盖)
├── tests/                       # 14 个测试文件 / 613 用例
└── meowmeowclaw/
    ├── config.py                # 配置加载(纯解析, 无副作用)
    ├── paths.py                 # 项目根 / workspace / 人设路径唯一来源
    ├── main.py                  # 入口(装配 + 交互)
    ├── __main__.py              # python -m meowmeowclaw
    ├── agent/
    │   ├── context.py           # System Prompt / messages
    │   ├── loop.py              # AgentLoop 控制流
    │   └── tools/               # BaseTool · registry · filesystem · shell ·
    │                            #   web_search · web_fetch
    ├── skills/
    │   ├── loader.py            # SkillCatalog 扫描/索引/摘要
    │   ├── models.py            # Skill / SkillConfigError
    │   ├── tool.py              # load_skill 工具(子系统唯一出口)
    │   └── builtin/             # 6 个内置技能(每子目录一个 SKILL.md, 随包发布)
    └── providers/               # LLMProvider - OpenAICompatProvider
```

---

## 11. 已知限制与 Roadmap

- 会话历史仅存内存, **无持久化 / 断点续跑**; 未实现流式输出与多 Agent 编排
- 长期记忆(`workspace/memory/MEMORY.md`)**已预留读取接口**, 尚无写入与检索
- 技能系统边界见 [4.4](#44-边界): 纯文本、单层目录、不含脚本与资源随附
- 安全侧的已知缺口见 [5.5](#55-已知缺口与局限诚实清单)(路径前缀绕过、命令黑名单绕过、抓取重定向/重绑定)
- 单实例 `AgentLoop` 不建议并发 `run()`(内部状态未加锁)
- 工程配置见 `pyproject.toml`(依赖 / 控制台入口 / pytest); 暂无 CI 配置

