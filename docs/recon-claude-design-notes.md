# 参考源功能域侦察笔记（职责/接口级，代码自写）

> 生成：2026-09-08，Explore agent 侦察 `claude-code-analysis-main/src`。
> 用途：M4 记忆 / M5 skills / M6 计划模式 / M7 compact / M8 子 agent / M9 MCP / M10 打磨 的落地蓝本。
> 只记职责与接口设计，**不照搬代码**（版权红线）。所有路径相对 `claude-code-analysis-main/src/`。

## memdir（+ SessionMemory / extractMemories）

- 核心文件：`memdir/paths.ts`、`memdir/memdir.ts`、`memdir/memoryTypes.ts`、`memdir/memoryScan.ts`、`memdir/findRelevantMemories.ts`、`memdir/memoryAge.ts`、`services/SessionMemory/*`、`services/extractMemories/*`。
- 布局：默认 `~/.claude/projects/<sanitized-git-root>/memory/`（git worktree 归一到同一 canonical root）。目录含 `MEMORY.md` 纯索引（无 frontmatter，每行 `- [Title](file.md) — hook`，≤200 行且 ≤25KB，超限截断并附 WARNING）；每条记忆是独立 topic `.md`，frontmatter：`name`、`description`（召回相关性判据）、`type` ∈ `user|feedback|project|reference`。
- 覆盖/开关：env `CLAUDE_CODE_DISABLE_AUTO_MEMORY`、`CLAUDE_CODE_SIMPLE`、settings `autoMemoryEnabled`；目录可被 env `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` 或 settings `autoMemoryDirectory` 整体替换。
- 团队记忆：`memory/team/` 子目录，敏感数据（API key）禁入。写入须过 symlink/遍历校验。
- 提示注入：`loadMemoryPrompt()` 造"行为指南" system-prompt section（"如何存"两步行=写文件+加索引；"何时取"；"不要存"清单=代码/架构/git/CLAUDE.md 可推导内容；recall 时 verify before recommending）。KAIROS 模式按日 append-only log。
- 召回：`scanMemoryFiles` 列目录 .md（排除 MEMORY.md，cap 200，读 frontmatter 前 30 行）；`findRelevantMemories` 用 side-query（json_schema `{selected_memories: string[]}`，max 5，排除 alreadySurfaced）挑文件。
- SessionMemory（会话内短期笔记，供压缩用）：独立会话文件（0600），固定标题块模板，总上限 ~12k token；自动提取用 fork 子代理，只允许 Edit 该记忆文件；阈值 init≥10k token、距上次增长≥5k、≥3 次工具调用或自然停顿。
- Python 最小复刻：目录/文件布局、frontmatter 解析、MEMORY.md 截断、两阶段写（文件+索引）、LLM 相关度筛选、行为提示模板、会话笔记增量总结与受限写、全部开关与路径优先级。

## skills + commands

- 核心：`skills/loadSkillsDir.ts`、`bundledSkills.ts`、`mcpSkillBuilders.ts`、`tools/SkillTool/*`、`commands.ts`。
- 发现目录：managed `<managedPath>/.claude/skills`、user `~/.claude/skills`、project 逐级向上 `.claude/skills`、`--add-dir` 附加、legacy `/commands`（兼容单 .md 或 `<dir>/skill.md`；新 `/skills` 只收 `<name>/SKILL.md` 目录式）。子目录命名空间 `:`。真实路径去重，优先级深层>浅层。
- 条件技能：frontmatter `paths`（gitignore glob），模型触碰匹配文件时激活，触达前不出现。
- SKILL.md frontmatter：`name`、`description`、`allowed-tools`、`argument-hint`、`when_to_use`、`version`、`model`、`disable-model-invocation`、`user-invocable`（默认 true）、`context:'fork'`、`agent`、`effort`、`paths`、`hooks`、`shell`。
- 暴露给模型：一个 `Skill` 工具（schema `{skill, args?}`），listing `- name: desc — when_to_use`，预算=上下文 1%，每条 ≤250 字符。
- 执行：inline（默认）注入式新 user/system 消息，`$ARGUMENTS` 插值、`!`...`` 内联 shell（mcp 技能禁 inline shell）、`allowed-tools` 注入 alwaysAllow；`context:'fork'` 起子代理，回 `{status:'forked', agentId, result}`。
- Python 最小复刻：目录发现去重、frontmatter 全集、预算化 listing、参数/内联 shell 安全替换、inline vs fork 两路径、conditional/paths 激活。

## 计划模式与确认类工具

- 核心：`tools/EnterPlanModeTool/*`、`tools/ExitPlanModeTool/*`、`tools/AskUserQuestionTool/*`、`utils/plans.ts`、`commands/plan/*`。
- EnterPlanMode：无参数、shouldDefer、isReadOnly、requiresUserInteraction。call() 把 permission mode 置 `'plan'`。tool_result 附指令："只读探索，不写文件（除 plan 文件）；可 AskUserQuestion；就绪后 ExitPlanMode"。
- Plan 文件契约：模型先自己把 plan 写到 `getPlanFilePath(agentId)`。ExitPlanMode 不接收 plan 文本参数。
- ExitPlanMode：仅 mode==='plan' 有效；checkPermissions 一律 ask；批准后 mode 恢复 `prePlanMode`。tool_result 回显 `## Approved Plan:` + 全文。
- AskUserQuestion：入参 `{questions[1..4]{question, header, options[2..4]{label,description,preview?}, multiSelect}}`；答案经 permission 系统回 input，tool_result 文本 `"q"="a"`。
- Python 最小复刻：mode 状态机与 setMode/restore prePlanMode、plan 文件读写约定、ExitPlanMode 权限问询+回显、AskUserQuestion 1-4 问 schema 与答案回填。

## services/compact 与 tokenBudget

- 核心：`services/compact/{compact,autoCompact,prompt,grouping,microCompact,sessionMemoryCompact}.ts`、`query/tokenBudget.ts`。
- 触发：`shouldAutoCompact` = tokenCount ≥ effectiveContextWindow − 13_000；effectiveContextWindow = min(model window, env CLAUDE_CODE_AUTO_COMPACT_WINDOW) − min(maxOutputTokens, 20_000)。禁用在 DISABLE_COMPACT/AUTO_COMPACT。连续失败 3 次熔断。Session memory compact 优先，失败才走 legacy 全量。
- 摘要 prompt：no-tools 强前缀 + 9 段式（Primary Request / Key Technical Concepts / Files and Code / Errors and fixes / Problem Solving / All user messages / Pending Tasks / Current Work / Optional Next Step；partial 版 Current Work 换 Work Completed + Context for Continuing Work）。先 `<analysis>` 草稿再 `<summary>`，`formatCompactSummary` 剥 analysis、换 `Summary:` 标题。
- 替换：`isCompactSummary` user 消息 + compact boundary 标记；partial 按 pivot 双向切分、按 assistant message.id 分 API round。
- Python 最小复刻：token 估算、阈值/缓冲计算、单轮 no-tools 摘要调用、boundary 消息格式与替换、partial pivot 双向切分、session-memory 优先与降级。

## Task / 子代理 / coordinator

- Task 状态模型（后台任务）：`TaskType = local_bash|local_agent|remote_agent|in_process_teammate|local_workflow|monitor_mcp|dream`；`status = pending|running|completed|failed|killed`；每个任务有磁盘 `outputFile`+`outputOffset`（增量读输出）。
- 子代理模型（Agent tool）：schema `{description(3-5 词), prompt, subagent_type?, model?, name?, team_name?, isolation?:'worktree', cwd?}`。同步（isAsync:false）时把子代理消息流式 yield 给父循环；异步返回 `{agentId, description, prompt, outputFile}`。
- Agent 定义：built-in general-purpose（tools `['*']`）、Explore（read-only）、Plan、verification。自定义：`.claude/agents/<name>.md` frontmatter `{name, description, tools, model, permissionMode, context?}`。
- coordinator 模式：系统提示把主代理变协调者，worker 结果以 **user 角色 `<task-notification>` XML** 到达（`<task-id>/<status>/<summary>/<result>/<usage>`）。
- Task 列表工具（同会话 TODO，区别于后台任务）：TaskCreate/TaskUpdate/TaskList/Get/Output/Stop。
- Python 最小复刻：任务状态机与磁盘输出游标、Agent 同步流/异步+输出文件、agent 定义 frontmatter、权限继承规则、`<task-notification>` 结果协议、worker 工具裁剪、Task 列表 CRUD。

## services/mcp + MCPTool

- 配置 scope：project（逐级向上 `.mcp.json`，越近 cwd 越高）、user（claude.json）、local、enterprise；policy 允许/拒绝名单。Server schema `{type?: stdio(默认)|sse|http|ws|sdk…}`，stdio→command/args/env{}，值 `${VAR}` 展开。
- 命名：`mcp__<server>__<tool>`，server 名收敛 `^[a-zA-Z0-9_-]{1,64}$`；权限规则按全限定名。
- 发现：initialize → 检查 capabilities.tools → `tools/list` → 把每个远端 tool 包成本地 Tool：`name=mcp__…`、inputJSONSchema 透传、annotations→isConcurrencySafe/isReadOnly/isDestructive。
- Python 最小复刻：多 scope 配置合并与 env 展开、stdio/HTTP(SSE) 传输、initialize+tools/list、`mcp__server__tool` 命名、schema 透传与权限 passthrough、tools/call 映射与错误归一化。

## cost-tracker（附加）

- 单价表 per-model config；`calculateUSDCost(resolvedModel, usage)`；按 model 聚合 `ModelUsage{inputTokens, outputTokens, cacheRead, cacheCreation, costUSD, contextWindow, maxOutputTokens}`。
- 持久化：`saveCurrentSessionCosts` 写 config `lastCost/lastModelUsage/lastSessionId`；resume 仅当 lastSessionId 匹配才 restore。
- Python 最小复刻：按模型单价算 USD、按 model 聚合、会话末落盘/续会话恢复。

## 关键差异提醒（Python 复刻易漏）

- 技能与 slash 命令同对象族但 Skill 工具禁止调内置命令。
- plan 文件是"模型先写盘、ExitPlanMode 只读盘"。
- compact 摘要要求 no-tools 单轮 + `<analysis>/<summary>` 结构并剥 analysis。
- MCP 工具名是唯一标识并贯穿权限规则。
- 会话记忆/记忆抽取都用"受限 fork 子代理 + 只允许写特定文件"的白名单保证安全。
