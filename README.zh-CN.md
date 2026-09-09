<div align="center">

<img src="build/icon.png" width="96" alt="Ember logo" />

# Ember

**本地优先的 AI 编程工作台**

让 DeepSeek 与 OpenAI 兼容模型安全地阅读、修改和验证你的代码仓库。

[English](README.md) · [简体中文](README.zh-CN.md) · [下载最新版](https://github.com/Zhouyeming1/Ember/releases/latest)

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20arm64%20%7C%20Windows%20x64-lightgrey)](https://github.com/Zhouyeming1/Ember/releases/latest)

</div>

Ember 是一个面向真实代码仓库的 Electron 桌面 Agent。它把模型调用、文件工具、终端命令、权限审批、会话记录与 Diff 审查放进同一个本地工作台；你的界面与会话数据保存在本机，模型请求直接发送到你配置的服务商或本地网关，无云端中转。

## 为什么是 Ember

- **DeepSeek 优先**：支持自定义 Base URL、模型发现和推理强度设置，也可连接 OneAPI、Ollama、vLLM 等 OpenAI 兼容端点。
- **可见、可控**：实时展示工具调用、命令输出、文件改动和上下文用量。
- **权限隔离**：提供仅规划、编辑时询问、工作区权限与完全访问四种模式。
- **安全改动**：每次补丁保存文件检查点，可通过 `/undo` 恢复上一轮修改。
- **本地优先**：设置、凭据与会话存放在本机，无遥测、无云端模型代理。
- **桌面体验**：项目会话树、`@` 文件引用、生成中插话、图片输入、主题（纯白 / 纸质 / 暗色）、Diff 预览和中英文界面。

## 架构

Ember 完全自包含——没有第三方 agent 运行时依赖。跑 Agent 的一切都在本仓库：

```text
React Renderer
  对话、Diff、设置、项目与会话 UI
        │  contextBridge / Electron IPC
        ▼
Electron Main
  窗口、工作区、会话索引、Agent 进程托管
  （本地数据层：src/main/ember-core.ts）
        │  JSON-RPC over stdio  （python -m emberpy.rpc）
        ▼
emberpy —— Python agent 引擎（runtime-py/）
  agent 循环 · 工作区工具 · 权限模式
  文件补丁与 /undo checkpoint · hooks · skills · memory
```

- **Agent 跑在独立的 Python worker 里**（`runtime-py/emberpy`，以 `python -m emberpy.rpc` 启动）。权限模式、工作区工具、文件补丁、hooks、skills、memory 与 JSONL 会话落盘都在引擎内实现，渲染端只消费类型化事件。
- **`src/main/ember-core.ts` 是 Electron 侧的本地数据层**：数据 home `~/.ember`、文件型凭据（`auth.json`）、settings、MCP 配置与会话索引（`state.sqlite`）。
- 渲染进程不直接访问 Node.js；所有桌面能力都通过 `src/shared/types.ts` 定义的类型化 IPC 契约进入主进程。崩溃后可从已落盘会话继续对话，但引擎不会静默重放未完成命令。

## 模型与图片

桌面端默认面向 DeepSeek，并允许填写自定义 OpenAI 兼容 Base URL。API Key 本地存放在 `~/.ember/auth.json`，只会发送给你配置的服务商或本地网关。

粘贴图片时：

- 识图可用官方 DeepSeek Vision，或自行配置 GLM-4V 等兼容接口。
- MinerU 用于 OCR 解析，目前会将图片发送到 MinerU 服务，不应视为离线本地 OCR。

## 权限模式

| 模式 | 行为 |
| --- | --- |
| `plan` | 只读分析与规划；诊断命令可在只读沙箱中运行 |
| `ask` | 写入、网络或越界操作前请求确认 |
| `auto` | 自动执行工作区内的常规操作，越界时请求确认 |
| `full` | 关闭工作区沙箱，适用于用户明确授权的可信项目 |

沙箱是纵深防御，不替代代码审查。执行未知仓库中的命令前仍应检查 Agent 给出的操作。

## Agent Skills

Skills 由 Python 引擎加载。标准路径：

| 范围 | 路径 |
| --- | --- |
| 项目（需信任） | `.agents/skills/<name>/SKILL.md` |
| 用户全局 | `~/.ember/skills/<name>/SKILL.md`、`~/.agents/skills/<name>/SKILL.md` |

每个 skill 目录包含 `SKILL.md`，frontmatter 需有 `name` 与 `description`。输入 `/skill:名称` 调用；设置 → Agent Skills 可查看已加载列表；输入 `/` 时也会出现在补全里。

## 使用

从 [GitHub Releases](https://github.com/Zhouyeming1/Ember/releases/latest) 下载：

- macOS：Apple Silicon / arm64
- Windows：Windows 10/11 x64

首次启动后：

1. 打开项目目录。
2. 在设置中填写 DeepSeek API Key 或自定义兼容端点。
3. 输入任务，审查工具执行与文件 Diff；需要时使用 `/undo`。

### 生成中插话

生成过程中仍可输入并回车，内容会作为插话立刻交给当前轮次（显示在输入框上方），而不是排队等本轮结束后再发。`/` 命令不会作为插话发送。换对话、新对话或换项目会清空未完成的插话展示。

当前 macOS 包使用开发签名。若 Gatekeeper 拦截，请右键应用选择“打开”，或执行：

```bash
xattr -cr /Applications/Ember.app
```

## 本地开发

要求 Node.js `>=22.19`、pnpm，以及能 import 引擎的 Python `>=3.11`。

```bash
git clone https://github.com/Zhouyeming1/Ember.git
cd Ember
pnpm install
pip install -e ./runtime-py   # 让应用的 Python 能 import `emberpy`
pnpm dev
```

Agent worker 以 `python -m emberpy.rpc` 运行。如果你的默认 `python` 不是装有 `emberpy` 的解释器，请用 `EMBERPY_PYTHON` 环境变量指向正确的解释器。

常用检查：

```bash
pnpm typecheck
pnpm test
pnpm build
```

Python 引擎测试：

```bash
cd runtime-py && ./.venv/Scripts/python.exe -m pytest   # Windows
# 或先激活 runtime-py 的虚拟环境，再运行：pytest
```

## 隐私说明

Ember 不运行遥测或模型代理服务器。会话、设置与凭据保存在本机；但为了完成任务，提示词、相关代码上下文与图片会发送给你选择的模型、网关或 OCR 服务。使用第三方服务前请阅读其隐私政策，敏感项目可连接本地兼容端点。

## License

[MIT](LICENSE)。
