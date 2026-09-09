# Ember

面向仓库的 AI 编程桌面工作台。本地数据层在 `src/main/ember-core.ts`，Agent 引擎是 Python 包 `runtime-py/emberpy`；本仓库是 Electron 壳 + 中文工作流界面 + 引擎实现。

## 地图

- 产品说明与架构：[README.md](README.md)
- 主进程（窗口 / IPC / 权限 / 工作区）：[src/main/index.ts](src/main/index.ts)
- RPC 子进程宿主：[src/main/agent-host.ts](src/main/agent-host.ts)
- 界面状态：[src/renderer/App.tsx](src/renderer/App.tsx)
- 组件：[src/renderer/ui.tsx](src/renderer/ui.tsx)
- 会话归并：[src/renderer/conversation.ts](src/renderer/conversation.ts)
- IPC 契约：[src/shared/types.ts](src/shared/types.ts)
- 长任务协议：`.agents/skills/init-long-run`、`.agents/skills/continue-long-run`、`.agents/skills/plan-then-act`
- 界面气质（改 CSS / 组件 / 克隆同款工作台）：`.agents/skills/ember-ui`（Cursor 侧同步 `.cursor/skills/ember-ui`）

## Agent Skills

Skills 由 Python 引擎加载（Ember 不另写 loader）。标准路径：

| 范围 | 路径 |
| --- | --- |
| 项目（需信任） | `.agents/skills/<name>/SKILL.md` |
| 用户全局 | `~/.ember/skills/<name>/SKILL.md`、`~/.agents/skills/<name>/SKILL.md` |

每个 skill 目录一个 `SKILL.md`，frontmatter 需含 `name` 与 `description`（引擎校验，缺项不会加载）。

- 调用：输入 `/skill:名称`；输入 `/` 时也会列出当前会话已加载的 skill
- 查看：设置 → Agent Skills（展示路径与已加载列表）
- 项目 skill 需先信任项目；`@` 文件引用仅扫描项目内 `.agents/skills`

## 约定

- 新对话必须先选项目；绑定 cwd，发第一条消息才 `startAgent`。
- 生成中回车进入排队（最多 5 条，不入会话文件）；停止保留排队，换对话/项目清空。`/` 命令不进队。
- 权限：`plan` / `ask` / `auto` / `full`。有项目时 `sandbox: workspace-write`。
- 跨会话进度写在 `.agents/features.json` 和 `.agents/progress.md`，不要只写在聊天里。
- `features.json` 只改 `passes`，不要删条目或改 `description`。
- 面向用户的文案用简体中文；代码、路径、命令保持原文。

## 检查

macOS / Linux:

```bash
pnpm test
pnpm typecheck
```

Windows PowerShell:

```powershell
pnpm test
pnpm typecheck
```
