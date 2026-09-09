# runtime-py / emberpy

用 **Python 从零实现**的 Ember agent 引擎（前端 UI 保持不变，用 Python 引擎当后端）。

- 单测跑完即绿：`75 passed, 1 skipped`（全部离线，用假模型，不烧 API）。
- 桌面桥接（阶段 2）已完成：Electron 原版前端 + Python 引擎，live 对话/工具轨迹正常。

## 域分包结构

按职责把引擎拆成一个个域（对齐 `claude-code-analysis/src` 的分层思路，只借鉴"职责分包"，不抄代码）：

| 域 | 职责 | 对应 claude-code-analysis/src 的概念 |
| --- | --- | --- |
| `agent/` | 编排内核：Agent 循环、RuntimeInput、系统提示词 | `assistant/`（agentic loop + session） |
| `llm/` | 模型层：领域类型(base) + OpenAI 兼容客户端(chat) | `services/`（模型调用） |
| `tools/` | 工具注册表 + 文件系统工具 + 命令工具 | `tools/` / `skills/` |
| `permission/` | 模式换算 / 命令分类规则 / 路径作用域 / 权限门 | 权限裁决 |
| `patches/` | 文件整写补丁 + checkpoint（/undo） | `ember-checkpoint` 语义 |
| `session/` | JSONL 会话落盘 / 断点续跑 | `assistant/sessionHistory`、`state/` |
| `bridge/` | 对桌面 UI 的桥：JSON-RPC worker / 界面转录 / 权限确认往返 | `bridge/` |
| `cli.py` | 无头终端（run 单轮 / chat 交互） | `cli/` |
| `errors.py` | 统一异常目录 | — |
| `testing.py` | 离线测试用假模型 / 便捷工厂（随包分发） | — |

```
runtime-py/
├── pyproject.toml / README.md
├── emberpy/
│   ├── __init__.py        # 公开 API + 版本
│   ├── rpc.py             # 桌面 worker 进程入口（薄壳 → bridge/worker）
│   ├── errors.py          # 统一异常
│   ├── cli.py             # 无头终端入口
│   ├── testing.py         # FakeLLM / tool_call / make_agent
│   ├── agent/             # 编排内核 core / inputs / prompts
│   ├── llm/               # 模型层 base / chat
│   ├── tools/             # 工具 registry / fs / shell
│   ├── permission/        # 权限 modes / rules / scope / gate
│   ├── patches/           # checkpoint store
│   ├── session/           # 会话 core / stats
│   └── bridge/            # 桌面桥 worker / transcript / ui
└── tests/
    ├── conftest.py        # workspace 夹具
    ├── unit/{agent,llm,tools,permission,session,patches}/  # 域内单测
    └── integration/bridge/                                 # RPC 桥端到端
```

**两个进程入口**：

| 入口 | 用途 | 被谁调用 |
| --- | --- | --- |
| `python -m emberpy run/chat` | 无头终端（调试/脚本） | 人 |
| `python -m emberpy.rpc` | JSON-RPC over stdio worker | Electron 主进程 `src/main/agent-host.ts` |

## 运行

```bash
cd runtime-py
.venv/Scripts/python -m pip install -e ".[dev]"   # Windows
# 或 mac/linux: .venv/bin/python -m pip install -e ".[dev]"

# 需要 DeepSeek key（Windows PowerShell）：
#   $env:DEEPSEEK_API_KEY = "sk-..."

# 单轮执行（auto 模式：工作区内自动、危险命令询问）
.venv/Scripts/python -m emberpy run "读取 README 并总结" --cwd .. --mode auto

# 交互对话（支持 /undo 回滚最近写文件、/stats、/exit）
.venv/Scripts/python -m emberpy chat --cwd ..
```

## 测试（不联网，用假模型）

```bash
cd runtime-py && .venv/Scripts/python -m pytest
# 桥接端到端（真 spawn Python worker）在仓库根跑 vitest：
#   cd .. && pnpm vitest run src/main/agent-host.python.test.ts
```

## 设计要点

- 权限检查在**工具内部**执行（读/写/命令各自过权限门），循环层不重复判断——
  挂漏一个工具也不会绕过权限（纵深防御）。
- 写文件一律过 `PatchStore` 留下 checkpoint，`/undo` 可整文件回滚，不依赖 git。
- 会话是 append-only JSONL；崩溃后 `resume_messages()` 会裁掉"半截工具轮次"，
  可继续对话但**不静默重放未完成动作**。
- 模型返回非法 JSON 参数时逐条标 `parse_error` 回喂模型，让其自纠，不崩循环。
- 命令有超时与输出截断；超时后尝试杀进程组，减少孤儿进程。
- 界面（renderer）是 spec：`bridge/` 只发 renderer 能 parse 的少数事件，
  助手文本必须带**累计全文**（mergeAssistant 是整文本覆盖）。
- 密钥只在进程环境里注入（`DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`），不落盘、不经渲染进程。

## 移植地图：以后从 claude-code-analysis/src 加东西往哪放

> 完整按里程碑走的迁移计划见仓库根 `docs/MIGRATION-ROADMAP.md`（当前进度：M1 thinking 渲染已完成）。

| 想加的能力 | 放进 | 说明 |
| --- | --- | --- |
| 更好的 prompt / thinking 引导 | `agent/prompts.py` | 系统提示词已独立成模块 |
| 思考过程（thinking）渲染 | `agent/core.py` + `bridge/transcript.py` | 需把 thinking 存进消息并流给界面 |
| 磁盘会话持久化 / 侧栏 resume（v2） | `session/` | JSONL 骨架已就位，缺跨进程存储 |
| slash 命令 | `cli.py` 或新建 `commands/` | 对齐 claude-code `commands/` |
| skills（自定义技能包） | 新建 `skills/` | 对齐 claude-code `skills/` |
| MCP 工具 | `tools/` | 在注册表里加 MCP 工具源 |
| 上下文自动压缩 compact | `agent/core.py` + `session/` | 目前 compact 是桩 |
| fork 分支会话 | `session/` | 目前 fork 是桩 |
| 图片输入 | `llm/` + `tools/` | 需模型层支持多模态 |

> ⚠️ 参考 `claude-code-analysis` 时只学"职责怎么分、接口怎么设计"，代码要自己写。
> 直接照搬其代码会撞版权，也不利于你简历上把它当作自己的作品。
