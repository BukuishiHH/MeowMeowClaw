# MeowMeowClaw

依据 OpenClaw 思路实现的自定义 Agent —— **不依赖 LangChain / LangGraph 等编排框架**，用一个显式的「模型 ↔ 工具」循环驱动。

- 语言/依赖：Python 3.10+，运行时仅需 `openai`（AsyncOpenAI）+ `python-dotenv`
- 代码规模：9 个模块 / 约 1300 行源码；测试 9 个文件 / 330 个用例（324 passed + 6 xfailed 记录已知缺口）
- 协议：OpenAI Chat Completions + function calling，任何兼容服务（DeepSeek / 通义 / vLLM / Ollama / One-API）改 `base_url` 即可接入

---

## 1. 快速开始

```bash
# 1) 安装依赖
pip install openai python-dotenv pytest pytest-asyncio

# 2) 配置：复制模板并填入密钥
cp .env.example .env
export DEEPSEEK_API_KEY=sk-xxxx        # .env 里用 ${DEEPSEEK_API_KEY} 引用

# 3) 启动
python -m backend.main                 # 或 python backend/main.py
```

交互命令：`/exit` 退出 · `/clear` 清空对话历史 · `/tools` 查看已注册工具

配置项（`.env`，与 `backend/` 同级；优先级 **系统环境变量 > .env > 代码默认值**）：

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `model` | `deepseek-chat` | 模型名 |
| `api_key` | 空（启动即提示并退出） | 支持 `${ENV_VAR}` 引用系统环境变量 |
| `base_url` | `https://api.deepseek.com` | OpenAI 兼容服务地址 |
| `workspace` | `<项目根>/workspace` | 留空或写 `.` 都表示该预设；相对路径按**项目根**解析，不随 cwd 漂移 |
| `max_iterations` | `32` | 单轮「模型↔工具」往返上限；非法值回退默认 |
| `identity_file` | `identity.md` | 人设文件，优先 `workspace/` 再兜底 `backend/` |

---

## 2. 构建框架：四层 + 一个循环

```
┌────────────────────────────────────────────────────────────────────────────┐
│ 入口 / 交互层      main.py                                                 │
│   build_agent() 装配 · interactive_loop() REPL · main() 启动               │
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
│ 提示词层              │ │ 能力层                │ │ 模型接入层             │
│ context.py            │ │ tools/                │ │ providers/             │
│ ContextBuilder        │ │ BaseTool (ABC)        │ │ LLMProvider (ABC)      │
│ build_system_prompt() │ │ ToolRegistry          │ │ OpenAICompatProvider   │
│ build_messages()      │ │ Read/Write/ListDir    │ │ → AsyncOpenAI          │
│ 人设+时间+工作区      │ │ → OpenAI function     │ │ 异常 → error 响应      │
└───────────────────────┘ └───────────────────────┘ └────────────────────────┘
                                                                 │            
┌────────────────────────────────────────────────────────────────▼───────────┐
│ 配置层             config.py :: Settings + load_config()                   │
│   .env → 环境变量 > 文件 > 默认值 → 校验/兜底 → workspace 自动创建         │
│   统一数据契约: LLMResponse / ToolCallRequest / FINISH_REASON_*            │
└────────────────────────────────────────────────────────────────────────────┘
```

**一次 `run()` 的数据流**

1. `ContextBuilder.build_messages(history, user_message)` → `[system] + 历史 + [本次用户消息]`
2. `provider.chat(messages, tools=registry.get_definitions(), model)` → `LLMResponse`
3. 若 `has_tool_calls`：把 assistant(`tool_calls`) 追加进 messages → 逐个执行 `registry.execute(name, arguments)` → 结果以 `role="tool"` 回填 → 回到第 2 步
4. 若无工具调用：追加最终 assistant 消息 → 整轮写入 `_session_history` → 返回文本
5. 超过 `max_iterations` 返回超时提示；`finish_reason=="error"` 返回错误文本

**依赖方向单向**：`main → loop → context / tools / providers → config`，无循环依赖；`config` 只依赖标准库与 dotenv。

---

## 3. 各组成部分

| 模块 | 职责 | 关键 API |
| --- | --- | --- |
| `backend/config.py` | 读 `.env`，校验兜底，解析/创建工作目录 | `load_config()` / `load_settings()` / `settings` / `Settings` |
| `backend/providers/base.py` | LLM 接入抽象 + **统一数据契约** | `LLMProvider.chat()`、`LLMResponse`、`ToolCallRequest`、`FINISH_REASON_*` |
| `backend/providers/openai_compat.py` | OpenAI 兼容实现（异常包装成 `finish_reason="error"`） | `OpenAICompatProvider` |
| `backend/agent/tools/base.py` | 工具抽象，产出 OpenAI function 定义 | `BaseTool`（`name`/`description`/`parameters`/`execute`） |
| `backend/agent/tools/filesystem.py` | 内置工具：读/写/列目录，带**工作区越界防护** | `ReadFileTool` / `WriteFileTool` / `ListDirTool` |
| `backend/agent/tools/registry.py` | 注册、去重、定义查询、按名路由执行 | `ToolRegistry` |
| `backend/agent/context.py` | 组装 System Prompt 与 messages | `ContextBuilder.build_system_prompt()` / `build_messages()` |
| `backend/agent/loop.py` | **控制流**：多轮往返、防爆护栏、会话历史 | `AgentLoop.run()` / `clear_history()` |
| `backend/main.py` | 入口：装配 + 命令行交互 | `build_agent()` / `interactive_loop()` / `main()` |
| `backend/identity.md` | 人设文本（可被 `workspace/identity.md` 覆盖） | — |
| `backend/test/` | 9 个文件 / 330 用例，Mock 为主 + 真实实现对照 | — |

### 3.1 契约先行，实现可换

整个项目的"可替换性"建立在三份契约上：

- **Provider 契约**：`LLMProvider.chat(messages, tools, model) -> LLMResponse`。上层只认 `LLMResponse`，不关心背后是 OpenAI、DeepSeek 还是本地 vLLM。
- **工具契约**：`BaseTool` 同时是"给模型看的 JSON Schema"（`to_function_definition()`）与"给人写的可执行体"（`execute()`）。
- **消息契约**：全链路使用 OpenAI 消息格式（`system/user/assistant/tool`），`tool_calls` 里的 `arguments` 是 JSON 字符串。

### 3.2 统一错误策略："异常不炸主循环"

| 层 | 策略 |
| --- | --- |
| Provider | 捕获异常 → `LLMResponse(content="[LLM调用失败] ...", finish_reason="error")`（`CancelledError` 等 `BaseException` 继续上抛） |
| 工具 | 不抛异常，返回可读文本；`ToolRegistry.execute` 再兜一层 |
| Loop | `finish_reason=="error"` 立即返回；空 `choices`、工具名不存在等都变成文本回流给模型 |
| 交互层 | 单次失败只打印 `[异常]`，会话继续 |

好处是**模型能看到失败原因并自我纠正**（例如工具参数非法、工具名写错），而不是让整个进程崩掉。

### 3.3 循环护栏（DAG 里通常要额外加节点才能做的事）

`_check_tool_loop(tool_name, args_json)` 用「工具名 + 入参 JSON」当签名，在长度 30 的滑动窗口里统计重复次数：

- 重复 ≥ 10 次 → **警告**：跳过本次执行，回填 `[SYSTEM_ERROR] ...` 提示模型换思路
- 重复 ≥ 20 次 → **熔断**：直接结束本轮并返回熔断说明
- 窗口内无论是否告警都记账，否则计数会停在 10 导致熔断阈值永远不可达

### 3.4 会话状态

- `_session_history`：跨轮次对话历史，**只保存跑完整的一轮**（用户消息 + 中间往返 + 最终回答）；错误/熔断/超时这类半截过程不写入，避免污染后续对话。
- `_tool_call_history`：仅用于防爆的签名滑窗，`/clear` 时一并清空。
- `reasoning_content`（推理模型的思考过程）保留在 `LLMResponse` 上，**不回填进 messages** —— DeepSeek 等要求多轮时不得回传。

---

## 4. 与「用 DAG / 图编排构建的 Agent」的区别

以 LangGraph 这类"节点 + 边"的图编排框架为对照（LlamaIndex Workflows、自研 DAG 引擎同理）：

| 维度 | 本项目（命令式 ReAct 循环） | DAG / 图编排（如 LangGraph） |
| --- | --- | --- |
| **控制流归属** | 写在代码里：`AgentLoop.run()` 的一个 `for` 循环 | 声明在图里：节点 + 边，由**引擎**推进 |
| **分支路由** | 由**模型输出**（`tool_calls`）在运行时动态决定 | 由开发者预先声明，`conditional_edge` 依据 State 选择分支 |
| **循环** | 普通 Python 循环（`for` / `continue`） | 回边（back edge）形成环，图里显式画出来 |
| **状态组织** | 一个 `messages: list[dict]` 直接传递，无中间态 | 全局 `State` 对象 + `reducer` 合并规则，节点返回增量 |
| **护栏（防死循环）** | 直接写在循环里（`_check_tool_loop` + `max_iterations`） | 需要额外插入 guard 节点或中间件 |
| **依赖** | 仅 OpenAI SDK + dotenv，零编排框架 | 引入编排框架及其概念栈 |
| **可观测 / 可视化** | 无内建；靠日志与断点 | 图结构可可视化，常见内置 tracing 集成 |
| **持久化 / 断点续跑** | 无内建（`_session_history` 在内存里） | 内建 checkpointer，可从任意节点恢复 |
| **人工审批（HITL）** | 需自行在循环里加确认步骤 | 一等公民：中断/恢复 API |
| **多 Agent 编排** | 需自己写调度（子 Agent 即另一个 `AgentLoop`） | 天然支持 supervisor / swarm 等拓扑 |
| **测试方式** | 换掉 `LLMProvider` 一个替身即可跑全链路（本项目 330 用例） | 通常要驱动图运行时，或按节点分别测 |
| **调试体验** | 断点就在 `run()` 里，栈短、易读易改 | 需要在框架抽象层之间跳转 |
| **适合场景** | 单 Agent + 工具调用的主线业务；想快速看懂/改控制流 | 复杂分支编排、长流程、需要暂停恢复与人工介入 |

**一句话概括**：DAG 是"把控制流**画出来**交给引擎跑"，本项目是"把控制流**写成代码**自己跑"。两者都能表达 ReAct 循环，本质差别是 **控制流归属**（引擎 vs 代码）与 **状态组织**（State + reducer vs messages 列表）。本项目的取舍是**用可读、可测、零依赖换取了编排能力**。

**演进建议**：若后续需要"并行工具调用 / 子 Agent 分工 / 人工审批 / 断点续跑"，把 `AgentLoop.run()` 内的循环替换成图编排是自然路径；由于 `LLMProvider`、`BaseTool` 都是抽象基类且数据契约独立，**替换编排层不需要改动 Provider、工具与配置层**。

---

## 5. 扩展指南

**加一个工具**（3 步）

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

**换模型 / 换服务**：只改 `.env` 的 `model` + `base_url`；换非 OpenAI 协议（如 Anthropic）则实现一个 `LLMProvider` 子类。

**换编排**：`AgentLoop` 是独立的一层，可用图编排或自研调度替换，`main.py` 只需换装配代码。

---

## 6. 测试

```bash
pytest                     # 全量: 324 passed, 6 xfailed
pytest backend/test/test_loop.py -v
```

策略：**Mock 为主、真实实现对照为辅**，并用变异测试验证用例有效性。

| 测试文件 | 用例 | 重点 |
| --- | --- | --- |
| `test_base_tool.py` | 3 | 工具基类抽象约束 |
| `test_tool_registry.py` | 19 | 注册/查重/路由/异常包装 |
| `test_filesystem.py` | 76 | 三个文件工具的读写、截断、路径穿越、异常分支 |
| `test_provider_base.py` | 48 | 数据契约（默认值、可变默认、`has_tool_calls`） |
| `test_openai_compat.py` | 44 | 请求参数（仅在有工具时传 `tools`/`tool_choice`）、tool_calls 转换、usage、异常兜底 |
| `test_context.py` | 40 | 人设查找链（工作区→backend 兜底）、时间实时取值、记忆注入 |
| `test_loop.py` | 35 | 消息格式、防爆阈值、历史写入策略、配置驱动默认值 |
| `test_main.py` | 22 | 装配、命令分支、Ctrl+C 优雅退出 |
| `test_config.py` | 43 | 取值优先级、路径解析、非法值兜底、密钥掩码 |

> 6 个 `xfail` 是有意保留的**已知缺口**（修复后自动转 XPASS）：工作区越界校验用 `str.startswith` 存在同前缀兄弟目录绕过；工具入参为 `None` 时抛 `TypeError`；`tool_calls=None` 时 `has_tool_calls` 抛 `TypeError`；`usage` 类型标注缺 `Optional`。

---

## 7. 目录结构

```
MeowMeowClaw/
├── .env / .env.example          # 配置与模板(密钥不入库)
├── pytest.ini                   # 测试配置(pythonpath=.)
├── workspace/                   # 运行时工作区: 与 backend/ 同级, 自动创建
└── backend/
    ├── config.py                # 配置加载
    ├── identity.md              # 人设
    ├── main.py                  # 入口(装配 + 交互)
    ├── agent/
    │   ├── context.py           # System Prompt / messages
    │   ├── loop.py              # AgentLoop 控制流
    │   └── tools/               # BaseTool · filesystem · ToolRegistry
    ├── providers/               # LLMProvider · OpenAICompatProvider
    └── test/                    # 9 个测试文件 / 330 用例
```

---

## 8. 已知限制与 Roadmap

- 工作区越界校验用 `str.startswith`，需改为 `os.path.commonpath`（同前缀兄弟目录可绕过）
- 会话历史仅存内存，无持久化 / 断点续跑
- 未实现流式输出（`stream=True`）、多模态、结构化输出
- 长期记忆（`workspace/memory/MEMORY.md`）已预留接口，未做写入与检索
- 单实例 `AgentLoop` 不建议并发 `run()`（内部状态未加锁）
- 无 `requirements.txt`（依赖见"快速开始"）
