# MeowMeowClaw 项目结构与技能加载设计

> 状态：**设计稿（待评审，未实施）**
> 适用范围：本地 CLI 版本
> 关联决策：见 §1；现状诊断见 §2；实施顺序见 §6；路径映射见 §7

---

## 1. 已确认的设计约束

| 项 | 决策 | 对结构的直接影响 |
| --- | --- | --- |
| 产品形态 | **本地 CLI**，不做前后端分离、不预留 Web 层 | 包名不应叫 `backend`；入口是 `cli.py` + `bootstrap.py` |
| 技能策略 | **内置技能**随代码分发；v1 不读取用户目录、不做覆盖 | `SKILL.md` 必须进版本控制，并随包发布 |
| workspace | 默认 `<项目根>/workspace`；可在 `.env` 里写绝对路径覆盖 | workspace 是纯运行时目录（gitignore），不含任何"源码级"内容 |
| 技术约束 | **零编排框架**；不引入 LangChain/LangGraph 等 | 控制流继续写在 `AgentLoop.run()`；不引入 Service/DI 框架 |
| 人设 | **仅使用 `<项目根>/identity.md`** | 删除 workspace 覆盖与 `backend/` 兜底两条查找链 |
| 技能加载方式 | **保持"技能加载能力暴露为工具"**（渐进式披露） | `load_skill` 工具保留，技能正文按需进入上下文 |
| 依赖 | 运行时：`openai` / `python-dotenv` / `httpx` / `pyyaml` / `ddgs` / `html2text` | 无新增运行时框架；`pytest` 等只在 dev extras |

补充约定：包名建议由 `backend` 改为 `meowmeowclaw`（纯本地 CLI，`backend` 名不副实）。这是一次机械重命名，不改变行为，放在迁移第 1 阶段执行。

---

## 2. 现状问题摘要（只列与本次设计相关的）

| # | 现状 | 证据 | 本设计如何解决 |
| --- | --- | --- | --- |
| 1 | 自带技能在被 gitignore 的 `workspace/` 里，克隆后技能消失 | `.gitignore:220`；`git check-ignore workspace/skills/...` 命中 | 内置技能迁入包内 `skills/builtin/`，随代码入库与分发（§4.1） |
| 2 | 技能目录两套语义：`SkillsLoader()` 默认指向 `<根>/skills`，`main.py` 传 `<workspace>/skills` | `skills.py:35,54` 与 `main.py:84` | 统一为"内置资源包目录"，由 catalog 默认解析；不再让调用方拼路径（§4.1） |
| 3 | 项目根在三处各自计算；`agent` 层反依赖 `config` 常量 | `config.py:20`、`context.py:26`、`main.py:23`、`skills.py:30` | 新增唯一 `paths.py`；skills 不再 import config（§4.5） |
| 4 | import 期副作用：`settings = load_settings()` 读真实 `.env` 并建目录；`import backend.agent.tools` 经 `load_skill → skills → config` 触发该副作用 | `config.py:150`、`loop.py:23`、`tools/__init__.py` 预导入全部工具 | 删除全局单例；配置在组合根读一次并注入；`tools/__init__` 只导出框架（§4.5、§4.6） |
| 5 | `tools/` 子模块从包 `__init__` 反向取 `BaseTool`，包 `__init__` 又预导入全部工具 | `registry.py:2`、`web_search.py:11` 等 6 处 | 子模块一律从 `.base` 导入；`__init__` 只收口 `BaseTool` / `ToolRegistry`（§4.4） |
| 6 | 工具、工具基类、注册表、技能适配器同目录平铺 | `backend/agent/tools/` 8 个文件 | 明确"框架 / 内置工具 / 子系统适配器"三类归属与拆分阈值（§4.4） |
| 7 | 测试在源码包内、无打包配置、入口依赖 `sys.path` hack | `backend/test/`、根目录无 `pyproject.toml`、`main.py:20-24` | 迁到根级 `tests/` + `pyproject.toml` + `__main__.py`（§4.7） |
| 8 | 人设查找链有 workspace 覆盖 + `backend/` 兜底 + 默认值三级 | `context.py:26,86`、`config.py` 的 `identity_file` | 固定单一 `identity.md`；缺失时显式警告 + 默认人设（§4.3） |
| 9 | 安全债务：文件越界用 `startswith` | `filesystem.py:41,97,145` | 路径校验函数收拢为一处，改 `commonpath`（§4.4、第 7 阶段） |

---

## 3. 目标结构总览

### 3.1 目录树（目标形态）

```
MeowMeowClaw/
├── pyproject.toml                     # 唯一工程配置：依赖 / 入口 / pytest / package-data
├── README.md
├── .env.example
├── .env                               # gitignore
├── identity.md                        # 唯一人设文件（项目根，入库）
├── docs/
│   └── ARCHITECTURE.md                # 本文档
├── workspace/                         # 纯运行时数据（gitignore，可被 .env 绝对路径覆盖）
├── meowmeowclaw/                      # Python 包（本地 CLI，平铺布局；将来分发再考虑 src/）
│   ├── __init__.py                    # 仅版本号等元信息
│   ├── __main__.py                    # python -m meowmeowclaw -> cli.main()
│   ├── bootstrap.py                   # 组合根：唯一知道具体实现的装配处
│   ├── cli.py                         # CLI 交付层：参数 / REPL / 命令 / 打印
│   ├── config.py                      # 配置解析：.env + 环境变量 + 默认值 -> Settings
│   ├── paths.py                       # 路径唯一事实来源：项目根 / workspace / identity
│   ├── llm/
│   │   ├── __init__.py                # 对外导出契约
│   │   ├── base.py                    # LLMProvider / LLMResponse / ToolCallRequest（零内部依赖）
│   │   └── openai_compat.py           # OpenAI 兼容实现
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── context.py                 # System Prompt / messages 构建
│   │   └── loop.py                    # AgentLoop：模型 <-> 工具 控制流 + 护栏
│   ├── skills/
│   │   ├── __init__.py                # 对外导出 SkillCatalog / Skill / LoadSkillTool
│   │   ├── models.py                  # Skill 数据模型 + 错误类型
│   │   ├── loader.py                  # SkillCatalog：扫描 / 索引 / 摘要 / 取正文
│   │   ├── tool.py                    # LoadSkillTool：技能子系统对 Agent 的唯一出口
│   │   └── builtin/                   # 内置技能资源（随包发布）
│   │       ├── exec/SKILL.md
│   │       ├── list_dir/SKILL.md
│   │       ├── read_file/SKILL.md
│   │       ├── write_file/SKILL.md
│   │       ├── web_fetch/SKILL.md
│   │       └── web_search/SKILL.md
│   └── tools/
│       ├── __init__.py                # 只导出 BaseTool / ToolRegistry（不导入任何具体工具）
│       ├── base.py                    # 工具契约（零项目内依赖）
│       ├── registry.py                # 注册 / 查重 / 定义查询 / 路由执行（只依赖 base）
│       ├── filesystem.py              # ReadFileTool / WriteFileTool / ListDirTool + 唯一路径校验
│       ├── shell.py                   # ExecTool
│       ├── web_search.py              # WebSearchTool
│       └── web_fetch.py               # WebFetchTool
└── tests/
    ├── conftest.py                    # 公共 fixture：tmp workspace / fake provider / skill 工厂
    ├── test_config.py
    ├── test_paths.py
    ├── test_identity.py
    ├── agent/
    │   ├── test_context.py
    │   └── test_loop.py
    ├── skills/
    │   ├── test_catalog.py
    │   └── test_tool.py
    ├── tools/
    │   ├── test_base.py
    │   ├── test_registry.py
    │   ├── test_filesystem.py
    │   ├── test_shell.py
    │   ├── test_web_search.py
    │   └── test_web_fetch.py
    └── test_bootstrap.py
```

> 说明：v1 的 `workspace/` 下**没有** `skills/` 子目录。它是纯运行时数据目录，只放 Agent 产物与将来的 `memory/MEMORY.md`。若未来开放用户自定义技能，再以"叠加根（overlay）"方式加回，见 §4.2 扩展点。

### 3.2 各目录职责与归属原则

| 路径 | 职责 | 允许依赖 | 禁止 |
| --- | --- | --- | --- |
| `cli.py` | 用户交互：banner、REPL、`/exit` `/clear` `/tools` `/skills`、异常兜底 | `bootstrap`、`agent` 类型 | 直接 import 具体 Provider/Tool |
| `bootstrap.py` | 组合根：读配置 → 造 Provider → 注册工具 → 加载技能 → 组装 Context/Loop | 所有具体实现模块 | 被其他业务模块 import |
| `config.py` | `.env`/环境变量解析、校验、兜底，产出 `Settings` | `paths` | 读磁盘以外的副作用（不建目录、不打印） |
| `paths.py` | 项目根、`workspace`、`identity.md` 的唯一解析 | 无 | import 项目内其他模块 |
| `llm/base.py` | 模型接入契约与数据契约 | 无 | import 上层 |
| `llm/openai_compat.py` | OpenAI 兼容实现，异常包装成 error 响应 | `llm.base` | import agent/tools |
| `agent/loop.py` | 控制流、护栏、会话历史 | `agent.context`、`tools.registry`、`llm.base` | import 具体 Provider/Tool、读写全局配置 |
| `agent/context.py` | System Prompt / messages 组装（人设正文作为参数传入或按路径读取） | 标准库 | import skills / tools 具体类 |
| `skills/*` | 技能扫描、摘要、按名取正文；对 Agent 只暴露 `LoadSkillTool` | `tools.base` | import agent.loop / cli / 具体工具 |
| `tools/base.py`、`registry.py` | 工具框架：契约与路由，零具体工具依赖 | 标准库 | import 具体工具、skills、config |
| `tools/<具体工具>.py` | 能力实现，构造参数显式传入所需资源 | `tools.base` | import config / bootstrap / skills |
| `tests/` | 测试，按被测包镜像分层 | 包内公开 API | 依赖运行目录、真实 `.env`、真实网络（除 `network` 标记） |

### 3.3 依赖方向（硬约束）

```
                    ┌─────────────┐
                    │   cli.py    │  交付适配器（可替换：将来加 web/api 也只换这一层）
                    └──────┬──────┘
                           ▼
                    ┌─────────────┐
                    │ bootstrap.py│  组合根：唯一 import 具体实现的地方
                    └──┬───┬───┬──┘
          ┌────────────┘   │   └──────────────┐
          ▼                ▼                  ▼
   ┌────────────┐   ┌────────────┐    ┌────────────┐
   │   agent/   │   │  skills/   │    │  tools/    │
   │ loop+ctx   │   │ catalog+   │    │ base+reg+  │
   │            │   │ tool       │    │ 内置工具    │
   └─────┬──────┘   └─────┬──────┘    └─────┬──────┘
         │                │                 │
         ▼                ▼                 ▼
   ┌──────────────────────────────────────────────┐
   │  llm/base.py · tools/base.py  （契约层，零依赖） │
   └──────────────────────────────────────────────┘
                           ▲
                    ┌──────┴──────┐
                    │ paths.py    │  （被 config / bootstrap 使用）
                    │ config.py   │
                    └─────────────┘
```

规则：依赖只能从上往下，**不得回指**。判断某个 import 是否合法，看箭头即可。

---

## 4. 专项设计

### 4.1 技能文件放在哪里

#### 候选方案对比

| 方案 | 位置 | 随包分发 | git 管理 | 编辑体验 | 主要问题 |
| --- | --- | --- | --- | --- | --- |
| A（你提出的位置） | `backend/agent/skill/<名>/SKILL.md` | 可 | 可 | 一般 | `agent` 是控制流层，不应承载内容资源；单数 `skill` 与子系统边界不清；与 `tools` 平级关系被破坏 |
| B（推荐） | `<包>/skills/builtin/<名>/SKILL.md` | ✅ `package-data` | ✅ | 好（技能子系统自带资源） | 需要通过 `importlib.resources` 读取，不能假设当前工作目录 |
| C | 仓库根 `skills/<名>/SKILL.md` | ❌（需额外打包配置） | ✅ | 最好 | 安装为 wheel 后路径失效；仍依赖"从源码根启动"的隐含假设 |
| D | 根 `resources/skills/<名>/` | ❌（同上） | ✅ | 好 | 多一个"资源袋"目录；与 C 相同的分发问题 |
| E | `workspace/skills/<名>/` | ❌（被 gitignore） | ❌ | — | **当前 bug**：克隆后技能消失；运行时目录被当成源码目录 |

#### 结论

**采用 B：`meowmeowclaw/skills/builtin/<技能名>/SKILL.md`。**

理由（按重要性排序）：

1. **内置技能是产品资源，必须与代码同生命周期**：入 git、随 wheel/sdist 发布、随版本升级。放在包内并通过 `package-data` 声明，是唯一能同时满足"源码运行 + 将来可安装分发"的位置。
2. **技能是独立子系统，不属于 `agent`**：`agent/` 的职责是"怎么驱动模型"；技能系统的职责是"有哪些指南、怎么按需取回"。两者只通过 `LoadSkillTool` 适配器连接（§4.4）。
3. **读取方式改为 `importlib.resources`，从根上消灭路径歧义**：不再有"默认 `<根>/skills` vs `<workspace>/skills`"两套语义，也不再依赖 `PROJECT_ROOT` 或当前工作目录。
4. **运行时目录保持纯净**：`workspace/` 只存用户数据与 Agent 产物，不与仓库内容混住。

#### 读取方式与打包

```python
# meowmeowclaw/skills/loader.py 关键实现示意
from importlib import resources
from importlib.resources.abc import Traversable

BUILTIN_PACKAGE = "meowmeowclaw.skills"

def default_builtin_root() -> Traversable:
    return resources.files(BUILTIN_PACKAGE) / "builtin"
```

- `SkillCatalog` 接收 `root: Traversable | None = None`；`None` 时用 `default_builtin_root()`。
- 只使用 `Traversable` 的 `iterdir()` / `is_dir()` / `read_text()`，**不调用 `os.listdir` / `open(path)`**，保证 zipimport / wheel 安装也能读。
- `pyproject.toml` 中声明包数据：

```toml
[tool.setuptools.package-data]
"meowmeowclaw.skills" = ["builtin/*/SKILL.md"]
```

#### 技能文件规范（v1 冻结）

```markdown
---
name: exec
description: 在工作区内执行 Shell 命令的用法与安全限制
---
# exec 使用指南
...
```

- 只扫描 `builtin/` 的**一层**子目录；子目录名即技能 ID，`name` 缺省时取子目录名；
- `name` 必须与子目录名一致（便于提示词与排障），不一致时启动警告并以 frontmatter 为准；
- 同名技能（不应当出现）→ `SkillCatalog` 抛 `SkillConfigError`，启动失败并给出明确信息（内置资源重复属于仓库错误，应尽早暴露）；
- 技能是**纯文本指南**，不包含可执行脚本与随附二进制资源；需要"做事"的逻辑一律写成工具（§4.2 边界）。

---

### 4.2 技能加载机制：继续做成工具，还是换别的？

#### 候选机制对比

| 机制 | 模型如何拿到技能 | 上下文成本 | 可控性 / 可测性 | 额外依赖 | 适用规模 |
| --- | --- | --- | --- | --- | --- |
| ① 全部内联进 System Prompt | 启动即注入所有正文 | 高（随技能数线性增长，挤占对话与工具结果） | 高，但会稀释指令、降低模型对关键约束的注意力 | 无 | 1~2 个极短技能 |
| ② **工具化 + 渐进式披露（当前）** | System Prompt 只放"名字 + 描述"，模型按需 `load_skill(name)` 取正文 | 低（只有用到的正文进入历史） | 高：执行路径明确、可单测、可观测（日志/工具调用记录） | 无 | 任意数量 |
| ③ 关键词 / 向量检索自动注入 | 用户消息命中后自动把正文塞进上下文 | 中 | 低：路由错误是"静默"的，模型不知道自己"被喂了什么"，难调试 | 关键词方案零依赖；向量方案需 embedding + 索引 | 技能很多（50+） |
| ④ Python 插件动态 import | 技能目录里的 `.py` 注册能力 | 无（能力是代码） | 低：执行任意代码引入安全与可复现性问题；版本升级易崩 | 无 | 需要"可执行技能"时 |
| ⑤ 约定模型输出特殊标记，由中间件接管 | 模型输出 `<skill>name</skill>` 后注入 | 低 | 低：比函数调用更脆弱，且要改控制流、绕过现有 tool 协议 | 无 | 不推荐 |

#### 结论

**坚持 ②：保留 `load_skill` 工具 + 渐进式披露。** 理由：

1. **与现有控制流零冲突**：`AgentLoop.run()` 的"模型 ↔ 工具"循环天然支持"取回文本"这一动作，不需要新增机制、不引入框架，符合零框架约束。
2. **上下文经济学**：技能正文只在相关任务中进入历史；对 6 个（将来更多）技能都可扩展，而内联方案会立刻膨胀 System Prompt。
3. **模型自解释、人类可观测**：工具调用会出现在消息历史与日志里，"为什么模型知道这条指南"有明确因果链；检索方案则是隐式的。
4. **可测试性最好**：给 `LoadSkillTool` 一个假 catalog 就能覆盖全部协议分支；不需要驱动额外运行时。
5. **与主流实践一致但无框架依赖**：Claude Skills / OpenClaw 的渐进式披露本质也是"元数据常驻 + 正文按需取回"，你现在的方向是对的，问题只在**资源放错位置**和**目录语义分裂**，这两点已在 §4.1 解决。

#### 需要补充的机制（不是换方案，而是把方案做扎实）

1. **工具契约（冻结）**

   | 项 | 设计 |
   | --- | --- |
   | 名称 | `load_skill`（保持稳定，不做改名，避免旧行为回归） |
   | 参数 | `{"name": string}`，`additionalProperties: false` |
   | 成功返回 | 去掉 frontmatter 的正文；超过 16000 字符截断并附提示 |
   | 未知技能 | `[错误] 未找到技能: X. 可用技能: a, b, c`（把可用名回给模型，便于自纠） |
   | 注册条件 | catalog 非空才注册；为空时启动打印明确警告（内置技能缺失说明打包/资源有问题） |

2. **Catalog 生命周期**：启动时扫描一次，构建 `{name: Skill}` 索引；`Skill` 持有 `name / description / body / source`。当前实现 `list_skills()` 每次调用都重新扫盘、`__repr__` 也扫盘，属于隐性 I/O，重构时一并去掉；需要热更新时显式调用 `catalog.refresh()`（测试用）。

3. **正文进历史的去重**：同一技能被反复加载时，正文会重复进入上下文。v1 依赖 `AgentLoop._check_tool_loop` 的滑动窗口护栏即可；若观察到浪费，再在 `LoadSkillTool` 里加"本会话已加载则返回一行提示 + 仍可选择强制加载"的开关，作为 v2 优化，不提前实现。

4. **提示词渲染**：System Prompt 只渲染"名字 (相对路径): 描述"；引导语明确"任务相关时调用 `load_skill`"。由 `SkillCatalog.summary()` 产出，`ContextBuilder` 只接收字符串，保持对 skills 包零依赖。

#### 明确的边界（v1 不做什么）

- **不**支持从技能目录动态 import Python 代码：技能是"给模型的说明"，工具才是"能执行的能力"；两者混用会同时破坏安全边界与可复现性。
- **不**做向量检索 / RAG / 自动路由：技能数量还远未到需要它的规模，且会引入隐式行为。
- **不**做用户技能覆盖（按你的决策，v1 全内置）。扩展点保留：`SkillCatalog` 将来可接收"多根（builtin → workspace overlay）"，同名时后者覆盖前者，届时只需改 catalog 的扫描入口，`LoadSkillTool` 与 Prompt 渲染不变。
- **不**把 `load_skill` 从工具改成"中间件自动注入"：那会把显式控制流变成隐式魔法，违背本项目"控制流写在代码里、一眼看懂"的定位。

---

### 4.3 人设文件：固定为项目根 `identity.md`

#### 变更内容

| 现状 | 目标 |
| --- | --- |
| `Settings.identity_file` 可配置，支持别名 `persona_file` | **删除该配置项**；`.env` 里出现时启动警告"该项已废弃" |
| 查找链：`workspace/<identity_file>` → `backend/<identity_file>` → `DEFAULT_IDENTITY` | **唯一路径**：`<项目根>/identity.md`；缺失时警告 + `DEFAULT_IDENTITY` |
| `backend/identity.md` 在包内 | `identity.md` 移到项目根，入库 |
| `context.py` 里两处路径计算 + `FALLBACK_IDENTITY_DIR` 常量 | `paths.IDENTITY_FILE` 是唯一定义；`ContextBuilder` 接收 `identity_path: Path` |

#### 缺失时的策略

`identity.md` 是随仓库入库的资源，缺失通常意味着打包/部署不完整。推荐：

- `ContextBuilder` 读取失败时 `logging.warning` 并使用内置 `DEFAULT_IDENTITY`（不阻断启动，符合"CLI 永远能起来"的定位）；
- `bootstrap` 启动时若走了兜底，打印一行可见提示：`[启动警告] 未找到 identity.md，已使用内置默认人设`；
- 将来若需要严格模式，再加 `--strict` 参数，v1 不做。

#### ContextBuilder 新签名（示意）

```python
ContextBuilder(
    workspace: Path,
    identity_path: Path,
    skills_summary: str = "",
)
```

`build_system_prompt()` 仍为：人设 + 当前时间 + 工作区 + （非空时）长期记忆 + （非空时）可用技能。

---

### 4.4 `tools/` 目录治理

当前 `backend/agent/tools/` 把四类东西平铺在一起：**契约**（`base.py`）、**运行时**（`registry.py`）、**具体工具**（5 个文件）、**跨子系统适配器**（`load_skill.py`）。治理原则如下。

#### 1）四类内容的归属

| 类别 | 文件 | 归属 | 依赖规则 |
| --- | --- | --- | --- |
| 工具契约 | `base.py` | `tools/` 顶层 | 零项目内依赖 |
| 工具运行时 | `registry.py` | `tools/` 顶层 | 只依赖 `tools.base` |
| 具体工具 | `filesystem.py` / `shell.py` / `web_search.py` / `web_fetch.py` | `tools/` 顶层（当前规模） | 只依赖 `tools.base` + 标准库/三方库；资源由构造函数注入 |
| 子系统适配器 | `LoadSkillTool` | **迁到 `skills/tool.py`** | 依赖 `skills.loader` + `tools.base` |

**为什么 `load_skill` 放 `skills/` 而不是 `tools/`**：它是技能子系统对外暴露的唯一出口；放在 `skills/` 可以让 `tools/` 保持"不知道 skills 存在"的洁净依赖，避免将来 `tools → skills → tools` 的回环。组合根注册时从 `skills` 包导入它，仅此一处跨包。

#### 2）导入规则（必须遵守）

```python
# ✅ 具体工具 / registry 一律从直接依赖导入
from .base import BaseTool
from .registry import ToolRegistry

# ❌ 禁止从包 __init__ 反向取（会形成"包未初始化完"的隐性循环）
from meowmeowclaw.tools import BaseTool
```

`tools/__init__.py` **只做两件事**：导出 `BaseTool` / `ToolRegistry`；不 import 任何具体工具。这样 `import meowmeowclaw.tools` 不再产生级联副作用（现在它会经 `load_skill → skills → config` 触发读真实 `.env`、建目录）。

#### 3）何时把平铺改成子包（拆分阈值）

不要为"看起来整齐"提前建目录。触发条件满足任一条再拆：

- 同一领域出现 **≥ 3 个模块**；或
- 同一领域出现 **≥ 1 个被多处复用的辅助模块**；或
- 单文件超过 ~400 行且包含多个不相关实现。

届时 `web_search.py` + `web_fetch.py`（+ 将来的 `web_download.py`）可迁为：

```
tools/web/
├── __init__.py
├── search.py
└── fetch.py
```

`filesystem.py` 目前 3 个类共用一个越界校验辅助函数、单文件 163 行，**维持单文件**即可，不必拆成 `filesystem/` 子包。

#### 4）路径校验收拢

`filesystem.py` 中三处重复的 `abspath + startswith` 校验提取为一个模块级函数（示意）：

```python
def resolve_in_workspace(workspace: Path, user_path: str) -> Path:
    """相对 workspace 解析并校验，越界抛 PathOutsideWorkspace。"""
    candidate = (workspace / user_path).resolve()
    if os.path.commonpath([str(candidate), str(workspace)]) != str(workspace):
        raise PathOutsideWorkspace(...)
    return candidate
```

顺带修掉 README §5.5 记录的 `startswith` 同前缀绕过缺口，让对应 xfail 转 XPASS。

#### 5）注册表的职责边界（保持不变，明确写入文档）

- `register(tool)`：查重、入表，重复抛 `ValueError`；
- `get_definitions()`：产出 OpenAI function calling 定义；
- `execute(name, arguments)`：按名路由、异步执行、把异常统一包装为可读文本回给模型；
- **不**负责：构造具体工具、读取配置、决定注册哪些工具。这些全部属于 `bootstrap.py`。

#### 6）组合根里的注册清单（示意）

```python
def _build_registry(config: Settings) -> ToolRegistry:
    from meowmeowclaw.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
    from meowmeowclaw.tools.shell import ExecTool
    from meowmeowclaw.tools.web_fetch import WebFetchTool
    from meowmeowclaw.tools.web_search import WebSearchTool

    registry = ToolRegistry()
    registry.register(ReadFileTool(config.workspace))
    registry.register(WriteFileTool(config.workspace))
    registry.register(ListDirTool(config.workspace))
    registry.register(ExecTool(config.workspace))
    registry.register(WebSearchTool())
    registry.register(WebFetchTool())
    return registry
```

具体工具的 import 只出现在这一处，将来增删工具、按配置开关 `exec` 都只改这里。

---

### 4.5 配置与路径：唯一来源 + 显式注入

#### 1）`paths.py`（新增，零项目内依赖）

```python
PROJECT_ROOT: Path          # 包目录的上一级（当前平铺布局）
ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_WORKSPACE = PROJECT_ROOT / "workspace"
IDENTITY_FILE = PROJECT_ROOT / "identity.md"

def resolve_workspace(value: str | None) -> Path:
    """未设置/空/./ -> DEFAULT_WORKSPACE；绝对路径原样；相对路径基于 PROJECT_ROOT。"""
```

- 全仓库只有这里计算项目根；`config.py`、`context.py`、`skills/`、`bootstrap.py` 都不再出现 `parents[1]`。
- `PROJECT_ROOT` 基于包位置推导。注意：这隐含"源码/可编辑安装"假设；若将来做全机器分发，默认 workspace 应改为用户目录（`~/.meowmeowclaw/workspace`）。当前按你的决策保持"仓库根/workspace"，在 README 中注明"安装版请在 `.env` 写绝对 workspace"。

#### 2）workspace 解析规则（写入 README 与 `.env.example`）

| `.env` 中 `workspace` | 结果 |
| --- | --- |
| 未设置 / 空 / `.` / `./` | `<项目根>/workspace` |
| 绝对路径（如 `/data/mc-workspace`） | 原样使用（`~` 展开） |
| 相对路径（如 `sandbox`） | `<项目根>/sandbox`，不随进程 CWD 漂移 |

目录创建时机：**只在 `bootstrap` 中执行一次**（`mkdir(parents=True, exist_ok=True)`）；`config.py` 保持纯函数、无副作用。

#### 3）删除全局单例与 import 副作用

| 现状 | 目标 |
| --- | --- |
| `settings = load_settings()` 在模块 import 时执行 | 删除；只保留 `load_config(env_file=None)` |
| `AgentLoop.__init__` 里 `from backend.config import settings` 取默认 `max_iterations` | `AgentLoop` 不再 import config；`max_iterations` 由 `bootstrap` 显式传入；构造参数默认值使用模块常量（便于单测直接构造） |
| `main.py` 调 `load_config()` 后自己拼技能目录/人设路径 | 全部由 `bootstrap` 完成，路径来自 `paths` 与 `SkillCatalog` 默认值 |
| `Settings.workspace: str` | 改为 `Path`；`__repr__` 仍掩码 `api_key` |

#### 4）配置项的最终形态

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `model` | `deepseek-chat` | 模型名 |
| `api_key` | 空（启动报错退出） | 支持 `${ENV_VAR}` 引用环境变量 |
| `base_url` | `https://api.deepseek.com` | OpenAI 兼容地址 |
| `workspace` | `<项目根>/workspace` | 绝对路径可覆盖 |
| `max_iterations` | `32` | 非法值警告并回退 |

删除 `identity_file`（含 `persona_file` 别名）。优先级保持：系统环境变量 > `.env` > 默认值。

---

### 4.6 装配与入口：`bootstrap.py` + `cli.py`

#### 1）职责切分

| 文件 | 职责 | 是否 print | 是否 import 具体实现 |
| --- | --- | --- | --- |
| `bootstrap.py` | 读配置 → 建 Provider → 建 Registry → 扫技能 → 建 Context → 建 Agent；返回 `Application` | 否（用 logging） | ✅ 唯一允许 |
| `cli.py` | 参数解析、banner、启动信息、REPL、命令、Ctrl+C/EOF 兜底、用户可见异常 | ✅ | ❌（只 import bootstrap 与契约类型） |
| `__main__.py` | `raise SystemExit(cli.main())` | 否 | ❌ |

#### 2）组合根返回结构（示意）

```python
@dataclass
class Application:
    config: Settings
    provider: LLMProvider
    registry: ToolRegistry
    catalog: SkillCatalog
    context: ContextBuilder
    agent: AgentLoop


def build_application(env_file: str | Path | None = None) -> Application:
    ...
```

- 缺少 `api_key` 的处理从 `main.py` 移到 `bootstrap`：抛 `ConfigError`；`cli` 捕获后打印引导并返回退出码 1 —— 让"装配失败"与"交互层展示"分离，便于将来别的入口复用 bootstrap。
- 技能为空（内置资源缺失）时不注册 `load_skill`，并 `logger.warning` + CLI 可见提示。

#### 3）CLI 命令

保持 `/exit`(`/quit` `/q`)、`/clear`、`/tools` 不变；新增（可选、建议）：

- `/skills`：列出已发现技能（名 + 描述 + 来源），纯展示，不改状态。

#### 4）入口命令

- 开发：`python -m meowmeowclaw`
- 安装：`pip install -e .` 后 `meowmeowclaw`
- 删除 `main.py` 的 `sys.path` hack 与 `python backend/main.py` 用法；README 同步更新。

---

### 4.7 打包、测试与依赖

#### 1）`pyproject.toml`（唯一工程配置）

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "meowmeowclaw"
version = "0.1.0"
description = "一个会用工具的小猫: 零框架的本地 CLI Agent"
requires-python = ">=3.10"
dependencies = [
    "openai>=1.0",
    "python-dotenv>=1.0",
    "httpx>=0.27",
    "pyyaml>=6.0",
    "ddgs",
    "html2text",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[project.scripts]
meowmeowclaw = "meowmeowclaw.cli:main"

[tool.setuptools.packages.find]
include = ["meowmeowclaw*"]

[tool.setuptools.package-data]
"meowmeowclaw.skills" = ["builtin/*/SKILL.md"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "network: 真实联网用例(默认跳过, 需 RUN_NETWORK_TESTS=1 才运行)",
]
```

- 删除根目录 `pytest.ini`（配置并入 `pyproject.toml`）；
- 零框架约束不受影响：`setuptools` 只是构建后端，不是运行时编排框架。

#### 2）测试布局

- `backend/test/` → 根级 `tests/`，按 `agent/` `skills/` `tools/` 镜像分层；
- `conftest.py` 提供：`tmp_workspace`、`fake_provider`、`skill_factory`（在 tmp 目录造 SKILL.md）、`env_file_factory`；
- 新增两类结构测试：
  1. **打包资源测试**：通过 `importlib.resources` 断言 `builtin/` 下 6 个技能可发现、frontmatter 可解析；
  2. **导入无副作用测试**：`import meowmeowclaw.tools` / `import meowmeowclaw.config` 不读 `.env`、不创建目录。
- 真实联网用例保持 `network` 标记与 `RUN_NETWORK_TESTS=1` 开关。

#### 3）CI / 本地检查（可选但建议）

- 最小方案：GitHub Actions 上 `pip install -e .[dev] && pytest -q`；
- 可加 `ruff`（lint + format check），同样放 dev extras；不引入运行时框架。

---

## 5. 依赖与命名硬规则（评审时逐条检查）

1. 只有 `bootstrap.py` 可以同时 import 具体 Provider、具体工具、`SkillCatalog`、`ContextBuilder`、`AgentLoop`。
2. `paths.py` 是项目根/workspace/identity 的唯一解析处；任何模块不得再出现 `Path(__file__).resolve().parents[...]`。
3. 契约模块（`llm/base.py`、`tools/base.py`、`skills/models.py`）不 import 项目内其他模块。
4. 任何模块 import 时不得读环境变量/`.env`、不得创建目录、不得发网络请求、不得 print。
5. 具体工具从 `.base` 导入基类，禁止从包 `__init__` 反向导入；`tools/__init__` 不导入具体工具。
6. `agent` 不 import `config`、`paths`（路径与参数由构造器注入）。
7. `skills` 不 import `agent`、`cli`、`config`；它对外的唯一能力出口是 `LoadSkillTool`。
8. 运行时数据只进 `workspace/`；产品资源只进包内 `skills/builtin/` 与根 `identity.md`；测试产物只进 tmp。
9. 目录命名：集合用复数（`tools`/`skills`），单职责用单文件；不新增 `utils/`、`common/`、`helpers/` 万能目录。
10. 面向用户的提示文本集中在 `cli.py`；库代码用 `logging`，不在核心层 `print`。

---

## 6. 迁移计划（每阶段独立提交、测试全绿）

> 前置要求：先把当前未提交的 `load_skill` / `test_skills_tool.py` 等工作提交并打 tag（如 `pre-refactor`），再开始任何结构性改动；否则结构 diff 与功能 diff 混在一起，回滚困难。

### 阶段 0：基线冻结与卫生（0 行为变化）

- 提交当前技能功能；`git tag pre-refactor`；
- 清理根目录 `list.txt`；检查 `workspace/` 下的 `bing.html` / `cn.html` / `test.md` / `XwX.txt`，确认无用后删除（workspace 本身保持 gitignore）；
- 记录全量测试基线（通过数 / xfail / skipped），写入 README 或提交信息。

**验收**：`git status` 干净；测试基线与 README 一致。

### 阶段 1：包重命名 + 打包骨架（机械变更，无逻辑改动）

- `git mv backend meowmeowclaw`；全局替换 `backend.` 前缀 import；
- `backend/test/` → 根级 `tests/`（按 §3.1 分层可留到阶段 6，本阶段先整体移动）；
- 新增 `pyproject.toml`、`meowmeowclaw/__main__.py`；
- 删除 `main.py` 的 `sys.path` hack 与"直接运行脚本"支持；
- 删除 `pytest.ini`（配置并入 pyproject）。

**验收**：`pip install -e .[dev]`；`python -m meowmeowclaw` 可启动；`pytest -q` 全绿；`grep -r "backend\." meowmeowclaw tests` 为空。

### 阶段 2：资源归位（解决 P0-1）

- `git mv meowmeowclaw/identity.md identity.md`（根目录）；
- 将 `workspace/skills/*` 迁到 `meowmeowclaw/skills/builtin/*/SKILL.md`，并 `git add`（它们此前未入库）；
- 新增 `meowmeowclaw/skills/__init__.py`、`loader.py`（可暂用 `Path(__file__).parent` 推导目录，阶段 4 再改 `importlib.resources`）；
- `bootstrap`/`main` 改为使用内置目录；`workspace/skills` 不再被读取；
- 更新 `.env.example`（删 `identity_file`）、README 技能章节与目录树。

**验收**：模拟全新克隆（`git clean -xdf` 后可再生成、或在临时目录 `git clone`）启动，能看到 6 个技能且 `load_skill` 已注册；`workspace/skills` 是否存在都不影响。

### 阶段 3：路径与配置收敛（解决 P0-3、P1 全局副作用）

- 新增 `paths.py`；
- `config.py` 改用 `paths` 常量；删除 `identity_file` 配置与全局 `settings` 单例；workspace 创建移到 bootstrap；
- `AgentLoop` 删除 `from ...config import settings`，`max_iterations` 由 bootstrap 注入；
- `ContextBuilder` 改为接收 `workspace: Path` + `identity_path: Path`，删除两级兜底查找；
- 更新受影响的 `test_config` / `test_context` / `test_loop` / `test_main`。

**验收**：`python -c "import meowmeowclaw.config"` 不产生文件；缺失人设时启动警告并使用默认人设；`workspace` 绝对路径覆盖用例通过。

### 阶段 4：技能子系统重构（解决 P0-2、P1 扫描语义）

- 新增 `skills/models.py`（`Skill`、`SkillConfigError`）；
- `skills/loader.py` 重写为 `SkillCatalog`：`importlib.resources` + 启动扫描一次 + `{name: Skill}` 索引 + 重复名报错；删除 `os.path.commonpath` 越界逻辑（改按索引精确查找，天然无穿越）；
- 新增 `skills/tool.py`（`LoadSkillTool`），删除旧 `tools/load_skill.py` 与 `agent/skills.py`；
- `tools/__init__.py` 删除 `LoadSkillTool` 导出；
- 测试迁到 `tests/skills/test_catalog.py` / `test_tool.py`，新增"打包资源可发现"测试。

**验收**：`load_skill` 全部协议用例通过；未知技能返回可用列表；轮子安装场景下资源仍可读（可在 CI 加 `python -m build` + 安装测试）。

### 阶段 5：tools 目录治理与安全修复

- `tools/__init__.py` 收口为只导出 `BaseTool` / `ToolRegistry`；全部具体工具改为 `.base` 导入；
- `filesystem.py` 提取唯一 `resolve_in_workspace()` 并改 `commonpath`；
- `bootstrap` 集中注册清单；
- 按拆分阈值决定是否建 `tools/web/`（当前可不建）。

**验收**：`import meowmeowclaw.tools` 无副作用；路径穿越相关 xfail 转 XPASS；注册/路由测试全绿。

### 阶段 6：入口拆分与测试分层

- 新增 `bootstrap.py` / `cli.py`；删除 `main.py`；
- `tests/` 拆为 `agent/` `skills/` `tools/` 子目录 + `test_bootstrap.py`；
- `LoadSkillTool` 注册、`/skills` 命令等归入对应测试。

**验收**：CLI 命令逐条行为不变；`python -m meowmeowclaw` 与控制台脚本行为一致；核心层无 `print`。

### 阶段 7：文档、CI 与收尾

- README 全面对齐：四层图、目录树、命令、`.env` 表、技能章节、测试统计；
- 新增 CI（pytest；可选 ruff）；
- 检查 `git status` 与 `docs/ARCHITECTURE.md` 中"目标结构"一致。

**验收**：全新环境按 README 从零跑通；CI 绿。

---

## 7. 旧 → 新 路径映射

| 现状 | 目标 | 说明 |
| --- | --- | --- |
| `backend/` | `meowmeowclaw/` | 本地 CLI，包名去后端化 |
| `backend/main.py` | `meowmeowclaw/cli.py` + `bootstrap.py` | 交互与装配分离 |
| `backend/config.py` | `meowmeowclaw/config.py` | 去全局单例；workspace 创建移出 |
| — | `meowmeowclaw/paths.py` | 新增，路径唯一来源 |
| `backend/identity.md` | `identity.md` | 唯一人设，项目根 |
| `backend/providers/` | `meowmeowclaw/llm/` | 与 tools 对称命名 |
| `backend/agent/loop.py` | `meowmeowclaw/agent/loop.py` | 去掉对 config 全局依赖 |
| `backend/agent/context.py` | `meowmeowclaw/agent/context.py` | 人设改为显式路径 |
| `backend/agent/skills.py` | `meowmeowclaw/skills/loader.py` + `models.py` | 独立子系统 + 数据模型 |
| `backend/agent/tools/load_skill.py` | `meowmeowclaw/skills/tool.py` | 适配器归子系统 |
| `backend/agent/tools/base.py` | `meowmeowclaw/tools/base.py` | 契约 |
| `backend/agent/tools/registry.py` | `meowmeowclaw/tools/registry.py` | 运行时 |
| `backend/agent/tools/{filesystem,shell,web_search,web_fetch}.py` | `meowmeowclaw/tools/同名.py` | 平铺，达到阈值再分组 |
| `workspace/skills/<名>/SKILL.md` | `meowmeowclaw/skills/builtin/<名>/SKILL.md` | **关键修复**：内置资源入库随包 |
| `backend/test/` | `tests/` | 测试移出源码包 |
| `pytest.ini` | `pyproject.toml [tool.pytest.ini_options]` | 配置统一 |
| 根 `list.txt`、workspace 调试产物 | 删除 / 保留在 gitignore 的 workspace | 清理源码树 |

---

## 8. 风险与开放问题

| 风险/问题 | 说明 | 缓解 |
| --- | --- | --- |
| `PROJECT_ROOT` 假设源码/可编辑安装 | 全机器安装时默认 workspace 会指向 site-packages 旁 | v1 本地 CLI 可接受；README 注明安装版请配绝对 `workspace`；将来默认改 `~/.meowmeowclaw` |
| 包重命名 diff 大 | 触及全部 import 与测试 | 单独阶段、纯机械替换、测试全绿后提交；必要时保留一版 `backend` 转发 shim |
| 旧 `.env` 里的 `identity_file` | 用户本地配置可能仍有该键 | 检测到就启动警告并忽略，不静默 |
| `load_skill` 正文重复进历史 | 模型反复加载同一技能 | v1 靠循环护栏；预留"已加载提示"扩展点，不提前实现 |
| 技能数量增长 | 全量摘要进 System Prompt 的成本 | 当前 6 个无压力；超过 ~20 个再引入检索或分组，保持 catalog 接口可替换 |
| 未来想加用户自定义技能 | v1 决策为纯内置 | catalog 预留多根 overlay（workspace 覆盖 builtin），届时只改扫描入口 |
| `filesystem` 安全修复改变行为 | 原本 xfail 的绕过用例会 XPASS | 属预期；在阶段 5 单独提交并更新 README 缺口表 |

---

## 9. 最终验收清单（全部满足才算设计落地）

- [ ] 全新克隆 + `pip install -e .` + `python -m meowmeowclaw`：启动即发现 6 个内置技能，`load_skill` 已注册；
- [ ] `import meowmeowclaw.config` / `import meowmeowclaw.tools` 无任何 I/O 副作用（不读 `.env`、不建目录）；
- [ ] 人设只来自 `<项目根>/identity.md`；缺失时行为明确（警告 + 默认人设）；
- [ ] `workspace` 默认 `<根>/workspace`，`.env` 绝对路径可覆盖；工作区以外不产生运行时数据；
- [ ] `tools/__init__` 不导入具体工具；具体工具不反向依赖包 `__init__`；
- [ ] `skills` 子系统对 Agent 只暴露 `LoadSkillTool`；技能正文按需进入上下文；
- [ ] 内置 `SKILL.md` 位于 `meowmeowclaw/skills/builtin/<名>/` 且随包发布；
- [ ] `tests/` 全绿，联网用例仅由 `RUN_NETWORK_TESTS=1` 触发；
- [ ] README 的目录树、命令、配置表与实际代码完全一致；
- [ ] 路径穿越 `startswith` 缺口修复并有测试覆盖（xfail → XPASS）。
```

