# 功能差距清单：emberpy vs Claude Code（核心引擎侧）

> 生成日期：2026-09-07
> 目的：为 `runtime-py/emberpy`（纯 Python 编码 agent，经 JSON-RPC 与桌面 Ember 通信）对照 Claude Code 核心引擎，找出**值得补齐**的功能差距，供后续 roadmap 排期参考。

## 方法与范围

- **参考源（只读，不复制代码）**：`C:\Users\liu\Downloads\claude-code-analysis-main\src`，仅研究 IDE 无关的纯后端核心（`services/`、`query/`、`tools/`、`utils/`、`memdir/`、`assistant/` 中与会话/历史相关部分）；不研究 `commands/`、`screens/`、`components/`、终端/UI。
- **对比对象**：本仓库 `runtime-py/emberpy`。调研方式为直接读源码 + 4 个并行探索 agent 分区深挖（工具实现 / API-模型 / 编排上下文 / 记忆权限）。
- **基线（视为已具备，未逐项复核）**：ChatLLM（SSE + usage + cost）；agent 主循环 + plan + 同步 run_agent（general/explore）+ compact hook + 记忆注入；fs/shell/update_plan/AskUserQuestion(单选)/memory/Skill/vision/MCP 工具；JSONL v2 会话 resume/fork/compact/checkpoint/undo；memdir 手动记忆；skills 加载器；权限门禁 full/auto/ask/plan + workspace 范围；patches 撤销账本；bridge JSON-RPC worker（transcript/UiConfirm/compact 等）。
- **明确不计为差距**：
  - 已裁剪项：Task* TODO 工具、AskUserQuestion 多选、后台 Bash(n) 窗口、MCP 远程/审批/hot-reload、MinerU OCR、后台任务面板（TaskOutput/TaskStop）、hooks 系统、plugins、.claude 权限文件审批。
  - host 已覆盖：命令面板/login/成本 UI/状态栏/MCP 连接管理/各 screen/IDE 集成/plugins/autofix/CI/mobile。

## 分级标记

- 【真缺口】：建议排期补齐，价值明确。
- 【可裁】：价值有限 / host 或后续架构更合适，现阶段不做或降级做。

## ✅ 已落地（2026-09-08 · M11 P1–P6，见 MIGRATION-ROADMAP 十三节）

下列【真缺口】已排期落地，表格内以「✅ M11-Px」标注（提交见 roadmap 十三节）：

- ✅ **M11-P1**：#3 glob、#4 先读后改 + mtime 校验（**严格强制**）、#5 重复读去重、read_file 行分页、grep 输出模式。
- ✅ **M11-P2**：#1 web_search、#2 web_fetch（EXTERNAL 权限；直调 host 已配置的 provider HTTP API）。
- ✅ **M11-P3**：#13 API 重试 + 退避、#14 SSE 看门狗、#15 token 估算分权；另加超限自适应恢复（裁旧段重发）。
- ✅ **M11-P4**：skills fork 隔离执行（`context: fork`）+ 动态发现（store.refresh）。
- ✅ **M11-P5**：hooks 命令引擎（原「已裁剪项」按用户 claude 对齐要求补做：用户级恒读 + 项目级 env 门控；Agent/Worker 全触发点接线；仅 command 型）。
- ✅ **M11-P6**：#9 记忆两级结构 + 相关召回（关键词/字符重叠 MVP：task 驱动 top ≤3 全文；后续可换小模型选择）。

## ✅ 已落地（Wave A/B/C · 2026-09-08，增量账本 R-A…R-H，见 MIGRATION-ROADMAP 十四节）

承接下节「落地批次建议」三波全做，表格内以「✅ Wave-x」标注（提交 c5dad56 / aeb21ab / 544b20a）：

- ✅ **Wave A（R-A/R-G①/R-H）**：#8 CLAUDE.md/AGENTS.md 注入（`agent/prompts.py` build_instructions，workspace 层、大小/层数限）；SessionEnd 白名单 bug 修复；`.ipynb` 读取（read_file 对 notebook 走 cell+output markdown 视图）。
- ✅ **Wave B（R-B/R-C/R-D）**：#7 deny + 敏感集合在 full/auto 仍拦（`permission/policy.py`，env 恒读 + 项目 `.ember/permissions.json` persist 才并入，policy 跨模式/跨子 agent 保留）；#18 symlink 词法+逐跳中间+终值三查；#6 bash 目标解析加固（重定向/rm/rmdir/chmod/chown/sed -i 位置参数过系统敏感判定、echo 只读收窄、$IFS/反引号混淆正则）。parser 差分仍裁。
- ✅ **Wave C（R-G②③④/R-F/R-E）**：hooks 增量（StopFailure 触发位；PermissionDenied → denied 标志 + hook；PermissionRequest gate 询问前 hook 代答 allow/deny/ask）；R-F schemas 每步重算 + skills **paths 条件激活**（`activate_for_paths`，fs 触碰激活，未激活不进可用清单/工具/命令面）；#21 自定义 agent 定义文件（`.claude/agents`/`.ember/agents` + home，正文当子 agent system prompt，tools/disallowedTools/maxTurns/permissionMode/initialPrompt 覆盖子池）。

## ✅ 已落地（#10 记忆自动抽取 · 2026-09-08，见 MIGRATION-ROADMAP 十五节）

表格内 #10 行以「✅」标注（提交 `75e1596`）。**实现 = 单次调用式 MVP（方案 B），非 claude
的 fork 只读子代理版**：worker 每轮收尾（`response success` 前、run 线程内）同步一次
`llm.complete(tools=None)`，喂本轮紧凑档案 + 现有索引，模型回 JSON 记忆数组，引擎侧
严格 parse/同名覆盖 merge 后逐条 `MemoryStore.save`。默认 env 关闭（`EMBERPY_AUTO_MEMORY`），
纯库/未开场景零额外模型调用。语义去重（向量/别名归并）、fork 子代理版、UI 事件不做。

**未动（仍为后续/有意裁剪）**：#11 会话记忆、#12 文件级快照、#17 输出预算、#20 auto LLM 分类器、#23 多工作目录、#24 的 PDF 侧，以及 R-块内已列边界（parser 差分、notifier/toast 基建、子 agent 挂 memory/skills/mcp、worktree 隔离、skills "激活即注入正文"、agent 的 model/effort/skills 覆盖）——需 host（renderer/main）消费面或成本较高的，等排期。注：#10 已落地（见上块，单次调用式 MVP，非 fork 子代理版）；#16 窄版（shell 大输出落盘）与 #19 精髓（settings 工具级 allow）已于收官批次落地（见下「✅ + 定案」块），自此 #16/#19 不再属未动清单。

## 差距清单（按模块）

| # | 功能块 | claude 侧文件（一句话职责） | 对 Ember 桌面 agent 的价值 | 难度 | 落点建议 | 级别 |
|---|--------|------------------------------|------------------------------|------|----------|------|
| 1 | Web 搜索工具 | `tools/WebSearchTool/WebSearchTool.ts`（经 LLM API 做服务端 `web_search` 嵌套 tool-use，最多 8 次，强制来源引用） | 桌面 agent 目前完全不能联网检索；host 端已有搜索配置（`src/shared/integrations.ts` brave/tavily/jina/exa）与 WebSearchDetail 卡片 | 中 | `tools/web.py` 新增 `web_search`，直接调 host 配置的 provider HTTP API（emberpy 走 OpenAI 兼容/DeepSeek，无服务端 web tool，不能照搬嵌套 tool-use）；输出按 host `parseWebSearchCard` 约定 | 【真缺口】 |
| 2 | Web 抓取 | `tools/WebFetchTool/WebFetchTool.ts`（HTML→markdown，域名白名单/黑名单预检，限制跨域重定向） | 与 #1 闭环："检索→读原文→引用"。host 已能渲染 fetch 类工具结果 | 易 | 同上 `web_fetch`（httpx + 简易 markdown；域名预检/缓存可精简） | 【真缺口】 |
| 3 | Glob 通配列文件 | `tools/GlobTool/GlobTool.ts`（glob 列路径，limit 100，结果相对化省 token） | fs 工具只有 list_dir/read_file/grep，模型无法用通配符一次定位文件，被迫 `ls -R` 全量 | 易 | `tools/fs.py` 加 `glob_tool`（上限 + 相对路径 + 尊重忽略规则） | 【真缺口】 |
| 4 | 先读后改 + mtime 校验 | `tools/FileEditTool/FileEditTool.ts`、`tools/FileWriteTool/FileWriteTool.ts`（未先读/读后 mtime 变了则拒绝写入） | 防止基于陈旧内容覆盖用户文件（正确性，不只是体验） | 易 | `fs.py` 的 `file_edit`/`write_file` 增加 read-state + mtime 校验 | 【真缺口】 |
| 5 | 重复读去重 | `tools/FileReadTool/FileReadTool.ts`（未变重读返回 `file_unchanged` stub，实测省 ~18% token） | 同文件反复读浪费上下文/钱 | 易 | `fs.py` `read_file` 命中同 mtime/size 返回 stub | 【真缺口】 |
| 6 | Bash 安全加固 | `tools/BashTool/bashPermissions.ts` + `bashSecurity.ts` + `utils/permissions/dangerousPatterns.ts`（LD_/PATH 劫持、危险 rm 目标、sed -i 门禁、解析差分 misparsing） | 现有 shell 用正则粗分类，危险命令易穿透；agent 越权风险 | 中 | `shell.py` 扩充危险模式 + 敏感目标黑名单（完整 parser 差分不必抄） | 【真缺口】(降级实现) |
| 7 | deny 规则/敏感文件在 full 模式仍硬阻断 | `utils/permissions/permissions.ts`、`filesystem.ts`（`DANGEROUS_FILES` .bashrc/.gitconfig/.mcp.json、`.git/.claude` 目录、safetyCheck 对 bypass 免疫） | full 模式若全放行，用户明确禁止的操作被绕过 | 易 | `permission/gate.py`：full 模式仍保留 hard-block 集合 | 【真缺口】 |
| 8 | 项目指令文件加载 | `context.ts`（getUserContext：合并 CLAUDE.md 树 + 当日日期）；`utils/attachments.ts`（编辑到子目录时加载嵌套 CLAUDE.md） | emberpy 与 host 目前都不读 CLAUDE.md/AGENTS.md，项目约定完全丢失，agent 行为"失忆" | 易 | `agent/core.py` 启动时注入 cwd 下 CLAUDE.md/AGENTS.md（限层数/限大小）；子目录嵌套为二期 | 【真缺口】 |
| 9 | 记忆两级结构 + 相关召回 | `memdir/memdir.ts`（MEMORY.md 索引常驻 + 主题文件）+ `findRelevantMemories.ts`（小模型从 manifest 选 ≤5 个文件注入，按年龄去重） | 现有 `memory_save` 全量注入 index 不缩放，文件一多就爆上下文；无法跨会话"按需"捞相关记忆 | 中 | `memory/store.py`：索引 + 主题文件；召回先用关键词/BM25 MVP，后续小模型选择 | 【真缺口】 |
| 10 | 记忆自动抽取 | `services/extractMemories/extractMemories.ts`（回合末 fork 只读子代理，仅写 memdir，带去重/更新指引） | 免手动沉淀长期记忆；当前全靠模型自觉 `memory_save` | 难 | ✅ `75e1596`：回合末**单次调用**抽取（worker 侧 complete(tools=None) → JSON → 同名覆盖 merge），默认 env 关 | 【真缺口】(远期) → ✅ 已落地 |
| 11 | 会话记忆（运行笔记） | `services/SessionMemory/sessionMemory.ts`（固定模板 summary.md，按 token/工具调用阈值抽取，供 compact 复用省一次 LLM 摘要） | 长任务 compact 现靠截断/现算摘要，丢"干到哪了、哪些文件、下一步" | 中 | `session/core.py`：先做"compact 前 LLM 生成结构化摘要并保留"即见效；再演进为阈值抽取 | 【真缺口】 |
| 12 | 文件级快照回滚 | `utils/fileHistory.ts`（edit 前快照 + 每回合打点，按 messageId 恢复文件系统并预览 diff） | patches 账本只能撤最近一次；无法回滚到任意回合的文件状态 | 难 | 在 patches ledger 上按 turn 打快照点，`undo` 增强为"回滚到某条消息" | 【真缺口】(增强) |
| 13 | API 重试 + 退避 | `services/api/withRetry.ts`（10 次指数退避，408/409/429/5xx，Retry-After，3×529 后降级/报错） | 现在非 200 直接停轮报"模型调用失败"，瞬时抖动即整轮失败；DeepSeek 高峰 429 常见 | 易 | `llm/chat.py` 加 httpx 重试（尊重 Retry-After，有限次） | 【真缺口】 |
| 14 | SSE 看门狗 + 流降级 | API 层（SSE idle 90s 判超时；流式失败降级非流） | 防半挂卡死整轮 | 易 | 并入 #13 一并实现 | 【真缺口】 |
| 15 | token 估算精确化 | `utils/tokens.ts`/`tokenEstimation.ts`（文本字符/4、JSON 字符/2、图片≈2k；先粗估再精确） | compact 现用字符数/3 一刀切，JSONL 里 JSON 占比高会系统性错估触发点 | 易 | `compact.py` 按事件内容类型分权估 token | 【真缺口】 |
| 16 | 大输出落盘 + 预览 | 工具层 toolResultStorage（>50KB 结果写盘，tool_result 指向文件路径 + 截断标记） | 现在 grep/shell 输出到 80KB 上限直接截断丢信息 | 中 | shell 窄版 ✅（2026-09-09 收官批次）：超 MAX_OUTPUT 时全文落 `.ember/out/shell-*.log` 并附工作区相对路径，模型用 read_file offset/limit 按行读回中段；MCP/web/read 其余 spill 保持裁（renderer crop 下收益打折） | 【可裁】→ 窄版已落地 |
| 17 | 输出预算 / USD 上限 | `query/tokenBudget.ts`、`QueryEngine` maxBudgetUsd（回合输出 token 预算、0.9 完成度阈值） | 桌面端控成本/控 max_tokens 溢出 | 中 | worker 设置模型时可选透传 max_tokens；依赖 host 传预算 | 【可裁】 |
| 18 | symlink 感知的路径包含 | `utils/permissions/filesystem.ts`（resolve symlink 后再判界，防 UNC/可疑 Windows 路径） | workspace 前缀校验可能被 symlink 绕过写越界 | 中 | `gate/rules.py` 校验前解析 symlink；可与 #7 合并实现 | 【真缺口】(并入#7) |
| 19 | 权限规则引擎 + 建议 | `utils/permissions/permissionRuleParser.ts`、`shellRuleMatching.ts`（`Bash(git *)` 内容规则、per-subcommand、ask 附 allow 建议） | 现只有模式分类；无会话级 allow 记忆，同一命令反复 ask | 中 | 免反复询问 ✅（2026-09-09 收官批次）：项目 `.claude/settings.json` 的 `permissions.allow/deny` 工具级规则进 gate（`Tool`/`Tool(pattern)`、Bash/Read/Write… 别名、无通配=前缀），命中 allow 短路 ASK/AUTO 普通询问——敏感写/deny/硬拦截不豁免；per-subcommand 规则、ask 附 allow 建议、会话级动态 allow 记忆保持裁 | 【可裁】→ 白名单部分落地 |
| 20 | auto 模式 LLM 分类器 | `utils/permissions/yoloClassifier.ts`（Opus 读 transcript 分类 block/allow，失败关闭） | regex auto 误放/误拦；但桌面端有 ask 兜底 | 难 | 保持裁剪；如做用"快路径白名单 + ask 兜底"即可 | 【可裁】 |
| 21 | 自定义 agent 定义文件 | `tools/AgentTool/loadAgentsDir.ts`（`.claude/agents/*.md` frontmatter：模型/记忆 scope/工具/隔离）+ `agentMemory.ts` | 项目级 persona 复用，不止内置 general/explore | 中 | 对接 host `.agents` 生态读取自定义 agent 定义 | 【真缺口】(依赖 host 约定) |
| 22 | Skill fork 执行模式 | `tools/SkillTool/SkillTool.ts`（inline 提示词扩展 vs fork 子代理隔离执行） | 复杂/有副作用技能隔离执行更安全 | 中 | ✅ M11-P4 已落地（`727ece3`）：frontmatter `context: fork` → 隔离子 agent 执行，正文不进主上下文、共享 PatchStore、无 spawner 回退 inline；`store.refresh()` 动态发现 | 【可裁】→ ✅ 已落地 |
| 23 | 多工作目录 | `utils/permissions/filesystem.ts`（cwd + additionalWorkingDirectories 作为一等权限范围） | 桌面单工程为主 | 易 | 不引入 | 【可裁】 |
| 24 | Notebook/PDF 读取 | `tools/NotebookEditTool/`、`tools/FileReadTool/`（pdf 分页读） | Python 用户可能碰 .ipynb；MinerU OCR 已裁，仅做文本抽取 | 中 | .ipynb 分格 ✅ Wave A 已落地（`c5dad56`）：read_file 对 notebook 走 cell+输出 markdown 视图；PDF 文本读取保持裁（pypdf 未引入） | 【可裁】→ ipynb 部分已落地 |

## ✅ + 定案（2026-09-09 · 差距账本收官批次，见 MIGRATION-ROADMAP 十六节）

承接"把剩下的开发任务做完"，做两处**窄版**落地，其余条目落**定案不做**（写账而非硬做）：

- ✅ **settings 工具级权限（#19 免反复询问精髓）**：项目 `<ws>/.claude/settings.json` 的
  `permissions.allow/deny`（`Tool` / `Tool(pattern)`；Bash/Read/Write/Edit/WebSearch/WebFetch
  别名表；无通配=前缀规则）进 `PermissionPolicy`/`PermissionGate`（env 门控
  `EMBERPY_PROJECT_SETTINGS=1`，沿用项目文件默认不信任）。deny 无条件（压过 full/confirm）；
  allow 只短路普通询问——**不豁免敏感写（.env/.git…）、deny 与硬拦截**（安全不变量，单测锁定）。
  不做：defaultMode、用户级 settings 合并、`pattern:*` 后缀语法。
- ✅ **shell 大输出落盘（#16 窄版）**：shell 输出超 80KB 时把**全文**落 `<ws>/.ember/out/shell-*.log`，
  返回附工作区相对路径，模型用 `read_file offset/limit` 按行读回被截中段；写盘失败退回纯截断；
  worker 在 `_new_session`/`close` 清理 24h+ 残留。只做 shell 一处。
- **定案不做**：#11 会话记忆（热路径不接 LLM 摘要；compact 结构化摘要已覆盖推进上下文）、
  #12 文件级回滚（正确落点在 host /undo）、#17 输出预算（依赖 host 传预算）、#20 auto LLM
  分类器（PermissionDecider hook + settings allow 已覆盖可配置豁免面）、#23 多工作目录、
  #24 的 PDF 侧、#16/#19 各自裁掉的子面（MCP/web/read spill、per-subcommand 规则、ask 附
  allow 建议、会话级动态 allow 记忆）。未排期"值得补"项（会话标题/分桶/跨会话搜索、
  settings defaultMode / 用户级合并）归 host UI 槽位或二期，不实现。

## Top 10 优先补齐建议

1. 联网工具组（#1 WebSearch + #2 WebFetch）——host 侧配置与 UI 已就绪，引擎是唯一缺口。
2. API 重试/退避 + SSE 看门狗（#13/#14）——直接决定日常可用性。
3. 项目指令加载 CLAUDE.md/AGENTS.md（#8）——成本最低的"行为对齐"。
4. 先读后改 + mtime 校验 + 重复读去重（#4/#5）——正确性与 token 双收。
5. Glob 工具（#3）。
6. deny 硬阻断 + symlink 路径包含（#7/#18）——安全底线。
7. Bash 危险模式加固（#6 降级实现）。
8. 记忆两级 + 相关召回（#9，关键词 MVP）。
9. 会话记忆/compact 结构化摘要（#11）。
10. token 估算精确化（#15）。

（注：此 Top-10 为 2026-09-07 排期初稿。其后 M11 P1–P6 / Wave A–C / #10 / 2026-09-09 收官批次已落地其中绝大多数（#1–#9、#13–#15、#19/#22/#24-ipynb…，见上各"已落地"块与「✅ + 定案」块）；清单内仅 #11/#12/#17/#20/#23 与 #24-PDF 仍为未来候选，且多为"定案不做"。）

## 检查过的 claude 目录清单

```
src/Tool.ts                                   工具框架（validateInput→checkPermissions→call→mapResult）
src/tools/                                    内置工具实现
  BashTool/  FileReadTool/  FileWriteTool/  FileEditTool/
  GlobTool/  GrepTool/  WebFetchTool/  WebSearchTool/
  NotebookEditTool/  SkillTool/  AskUserQuestionTool/  AgentTool/  SendMessageTool/
src/services/api/withRetry.ts                 重试/退避/降级
src/services/compact/                         整段压缩 + autoCompact + microCompact + sessionMemoryCompact
src/services/SessionMemory/                   会话运行笔记（模板/阈值抽取/注入）
src/services/extractMemories/                 回合末自动记忆抽取（fork 只读）
src/services/AgentSummary/ + awaySummary.ts   离开摘要/子代理进度（host 相关）
src/services/toolUseSummary/                  工具批一行摘要（SDK/mobile 相关）
src/memdir/                                   长期记忆：MEMORY.md 索引 + 主题文件 + findRelevantMemories + memoryAge
src/utils/fileHistory.ts                      文件快照/按消息回滚
src/utils/tokens.ts / tokenEstimation.ts      token 估算
src/utils/sessionStorage.ts                   JSONL 会话持久化/父链/分支（emberpy JSONL v2 已有等价物）
src/utils/attachments.ts                      附件注入（嵌套 CLAUDE.md / relevant_memories / skill 清单）
src/utils/permissions/                        规则引擎/文件系统权限/分类器/危险模式（源码要点）
src/query/tokenBudget.ts, QueryEngine.ts, query.ts  输出预算与主循环
src/context.ts                                项目上下文（CLAUDE.md 合并 + 当日日期）
src/assistant/sessionHistory.ts, src/history.ts      远程 CCR / 输入历史（非本地会话，host 相关）
```

> 注：本清单只反映"引擎能力差距"。凡标注"host 相关/已裁"者未列为差距，最终裁剪判断以 `docs/MIGRATION-ROADMAP.md` 中 M 里程碑口径为准。

---

# 全量地毯式复查（2026-09-08，任务 #46）：tools / skills / hooks / commands / 配置体系

> 用户要求"一个个和 claude code 对比，skills/hooks/tools 都不能缺"。方法：3 个并行 general agent 分区深读（tools 全集 / skills+hooks / 其余核心），全部结论以 Ember 侧实际文件/行号佐证；上文 #45 的结论沿用。范围扩大到 claude `src/` 全模块（含之前裁掉的 hooks/commands/screens/settings），对每项重判「桌面 DeepSeek 架构下能否补」。

## 0. 桌面架构边界（判断可否补的总纲）
- 引擎 = JSON-RPC over stdio worker（`python -m emberpy.rpc`）；`src/main/agent-host.ts` 是透明转发器；renderer **只消费固定事件集**，工具渲染为通用轨迹卡片。前端 spec 不改；`src/main` 有先例小改但最小化。
- 模型 DeepSeek/OpenAI 兼容 → **无 Anthropic 服务端能力**（服务端 web_search、OAuth、云 CCR、云侧 task 全不可用）。
- 引擎可自由读 `EMBER_HOME`(~/.ember) 下宿主写的 json（memory/skills/web-search 先例）→ **多数联网/配置功能引擎自读即够，不需要改 main**。

## 1. 工具全集对照（claude src/tools/ 45+ 目录）
### 值得补 Top 5（全引擎内、不动 renderer）
| 补项 | claude 文件 | 为什么 | 落点 | 难度 |
|---|---|---|---|---|
| Glob | `tools/GlobTool` | 引擎无通配列文件，模型被迫 ls -R | `tools/fs.py`，pathlib.rglob+忽略集，READ 类 | 易 |
| web_search | `WebSearchTool`(服务端版不可用) | 联网最高价值；**renderer 已按 `web_search` 名出卡**（conversation.ts:1307）+ **宿主已存 key 于 ~/.ember/web-search.json**（index.ts:552，形状 {workflow, braveApiKey?, tavilyApiKey?, jinaApiKey?, exaApiKey?}）→ 引擎直读该 json 适配 brave/tavily/jina/exa，**零 main 改动** | 新 `tools/web.py` | 中 |
| web_fetch / fetch_content | `WebFetchTool` | 纯 httpx 抓 URL 转文本，**renderer 认 `fetch_content` 名出卡** | 同 web.py | 低-中 |
| read_file 分页 | `FileReadTool` | 引擎现头尾截断无法跳读文件中部/末尾 | fs.py 加 offset/limit + 行号 | 低 |
| grep 输出模式 | `GrepTool` | 命中多时只回文件清单/计数，省大量 token | fs.py grep 加 output_mode/content|files|count + head_limit/offset | 低 |

### 工具框架层可借鉴（非照抄）
- **写前必读 + mtime 陈旧拒写**（Write/Edit 未读过或读后被外部改 → 拒）：fs 正确性护栏。落点 fs + 会话内已读状态表。中价值低难度。
- **重复读去重 file_unchanged**（Read 未变返回 stub，实测省 ~18% token）。同上。
- 语义 validator 与执行分离（参数错/执行错/权限拒绝三类区分）：Tool 加可选 validator，`_execute` 先跑。低优先。
- 连续拒绝降级提示模型换策略：denied 回填已有，仅差计数/提示。低优先。
- 大结果落盘+路径预览（超 60k 落临时文件）：低优先。
- CostTracker 非工具（会话成本模块），引擎 UsageTracker 已等价。

### 明确不该补（一句话理由）
PowerShell/REPL/Sleep（cmd 可顶/无 VM/无自延续）；TaskCreate/Get/List/Update + TodoWrite（renderer 只认 update_plan，spec 不可改）；TaskOutput/TaskStop（无后台任务）；SendMessage/Team*（无常驻多 agent swarm）；EnterWorktree/ExitWorktree（桌面单工作区、未必 git）；StructuredOutput/Cron*/RemoteTrigger（SDK/后台/CCR 云产物）；ToolSearch（~18 工具量小无 defer）；ConfigTool（设置归宿主）；LSP（无基建成本高，grep+read 顶）；Brief/SendUserMessage（宿主已渲染最终答复）；McpAuth/Resources（引擎 MCP 仅 tools 主路径）；NotebookEdit（.ipynb 编辑低频，read_file 还拒 ipynb）；AskUserQuestion 多选/多问（renderer 是单选卡片）。

## 2. Skills 差异
引擎已 inline 注入等价（frontmatter/`/skill:`/disable-model-invocation/$ARGUMENTS/SKILL_DIR）。缺三块（全引擎内）：
| 缺口 | claude 行为 | 价值 | 落点 | 难度 |
|---|---|---|---|---|
| context: fork | SkillTool 经 run_agent 把技能跑隔离子代理，防正文污染主上下文 | 高 | skill_tool 复用 `_spawn_subagent`（引擎已有，子 agent 事件不外发父只见一条轨迹 → renderer 零改） | 中 |
| paths 条件技能 | 按触碰文件路径 gitignore 匹配激活 | 中高 | loader 存 filter + `_execute` 工具后路径匹配 | 中 |
| 动态发现 | 随 Read/Edit 路径上溯发现子级 skills 目录 | 中 | loader 每次 Skill 调用重扫 + 路径上溯（限深） | 中 |
维持不做：inline shell 执行（有意安全取舍）；allowed-tools 可作"注入后临时放行"二期；mcp 技能/遥测/enable CLI（桌面无场景）。

## 3. Hooks（重要结论：可做，不动 renderer）
- **claude 事件全集**（coreTypes.ts:25）：Pre/PostToolUse(+Failure)、UserPromptSubmit、SessionStart/End、Stop(+Failure)、SubagentStart/Stop、Pre/PostCompact、Notification、PermissionRequest/Denied、Setup 等 26 项；带 matcher 的按 tool_name/notification_type。
- **执行机制**：命令 hook 用 shell spawn，输入 JSON 走 stdin，stdout 首行 JSON 协议（`{"continue":false}`/hookSpecificOutput），**退出码 0=成功回灌、2=阻塞（stderr 给模型并阻止继续，如 UserPromptSubmit 抹 prompt / PreToolUse deny）、其它仅可见**；超时默认可配；注入 session_id/transcript_path/cwd/permission_mode 等 env。
- **host 查证**：main 只写单层 `~/.ember/settings.json`（locale/provider/model，index.ts:847-895），有通用 read/writeHomeJson（index.ts:829-841）；**无 hooks 配置位**。renderer 只认固定事件集（App.tsx:1073-1118），未知 type 忽略。
- **可行性结论**：整套 hooks 可在**不动 renderer、不动 main** 前提下由引擎独立实现——读 `<home>/.ember/hooks.json`（用户级默认读）+ 工作区 `.ember/hooks.json`（**项目级默认不读**，需显式信任/授权，防第三方仓库带入即执行任意命令），在 agent/core.py + worker.py 天然事件点 subprocess 执行、解析退出码/JSON、输出以上下文/阻塞文本回灌。
- **有触发点可做**：UserPromptSubmit（`_execute_prompt` 在 agent.run 前，exit2 不建 agent 直接回文本）、SessionStart/End（Worker init/new_session/close）、Pre/PostToolUse(+Failure)（`Agent._execute` 工具 fn 前后，天然 chokepoint，deny→"权限拒绝"文本）、Stop(+Failure)（agent.run 返回后）、SubagentStart/Stop（`_spawn_subagent` child.run 前后）、Pre/PostCompact（`_compact_history` 摘要前后）、Notification（复用 `extension_ui_request` notify → toast 现成通道）、PermissionRequest/Denied（gate._ask/UiConfirm 包一层）。
- **无触发位不做**：FileChanged/CwdChanged/Worktree/ConfigChange/Elicitation/Task*/TeammateIdle（引擎无文件监视/worktree/elicitation/多 agent 队列）。
- hooks 的 `type: prompt/agent/http` 先不做（需小模型评判/嵌套代理/低优先），只做 `command` 型。

## 4. 其余核心复查（commands/settings/服务）
### 值得补（复查新发现）
| 补项 | claude 侧 | 价值 | 落点 | 难度 |
|---|---|---|---|---|
| 上下文超限自适应恢复 | `services/api/errors.ts` 解析超限 token 差距→自动 compact/裁剪重发一次 | 高（DeepSeek 兼容端点也会回 context_length_exceeded，现在整轮死） | llm/chat.py 捕错→算 gap→compact 后单次重试 | 易-中 |
| 上下文占用分桶分析 | `utils/analyzeContext.ts`(系统/工具/记忆/消息分桶)+`contextSuggestions.ts`(近 80%/超大工具结果/重复读建议) | 中高（renderer 只画圆环，用户看不懂被谁占满） | llm/usage.py 分桶 + worker 扩展字段 + 一条建议 | 中 |
| 会话标题生成 | `utils/sessionTitle.ts` 最近 1000 字符喂小模型 | 中（现标题=首条用户消息全文，含图片 handoff 噪音） | worker 首轮后异步补，或 host 做 | 易 |
| 跨会话相关性搜索 | `utils/agenticSessionSearch.ts` | 中上（找上次干到哪） | 二期 keyword 先 | 中 |

### 命令面结论
claude 101 个命令目录大多是 **CLI/TUI 层 + Anthropic 云账号 + 企业/团队/CCR**。Ember renderer 命令面板只消费技能 slash + 少量特判（/new /open /undo /compact /login /permissions）；状态类能力（/status /cost /context）应走 UI 槽位+worker 特判，不新建大命令面。裁掉的分类：纯 TUI（screens/statusline/components/vim/theme/onboarding/help/exit）；Anthropic 云/账号（login/oauth/stats/usage/upgrade/share/summary/insights/teleport/desktop(CCR)/remote-*/voice 等 ~50）；宿主已有等价（compact/clear/undo/resume/model/permissions/mcp/config/doctor/update-check）；企业/团队（coordinator/bridge*/buddy/plugins/autofix/bughunter/security-review）。`/import` 本快照无此命令。

### settings.json 配置体系结论
- claude：5 层来源后覆盖（user/project/local(私密)/flag/policy）+ zod 校验 35+ 字段 + `/config` 编辑 UI。Ember：**只有单层 `~/.ember/settings.json`={locale,provider,model}** + mcp.json + web-search.json；引擎 permission 无落盘 allow/deny 规则文件，renderer 权限下拉内存态、重开即重置。
- 值得轻量落地（M 里程碑增强，非硬缺口）：项目级配置 + 会话级 allow/deny 持久化（补 #19 裁掉的半），字段仅 {model, 默认权限模式, env, cleanupPeriodDays}。整体 5 层 schema 不搬。

## 5. 补齐建议（分批方案见下方实施计划）
引擎内可独立落地且价值明确者聚合为 6 个阶段：P1 本地文件/搜索工具升级 → P2 联网（web_search/web_fetch）→ P3 API 重试+超限恢复+token 估算 → P4 skills fork/条件/动态发现 → P5 hooks → P6 记忆两级相关召回。全部不需要改 renderer；web_search 靠引擎直读 ~/.ember/web-search.json，亦不改 main。每个阶段带单测/桥测后 commit。

---

# M11 后二轮对照（2026-09-08，任务 #53）

> 用户"现在再去对比呢"。方法：4 个并行 Explore agent 分区精读 claude 源码（① CLAUDE.md 注入链路 / ② 权限 deny+symlink 语义 / ③ bash 加固 / ④ tools-skills-hooks 增量 + agent 定义文件），逐点对照引擎现状并给出最小落点。P1–P6 已落地项（glob/grep 模式/分页/写前必读/web/重试/自恢复/skills fork+refresh/hooks 命令引擎/记忆两级）**不再重复报缺**。下表只列"仍缺且引擎内可落地、不动 renderer"的增量。

## 已核对的关键事实（claude 侧，决定落点形状）

- **CLAUDE.md 不是塞 system prompt**：`context.ts` 把 User+Project 指令合成 `{claudeMd,currentDate}`，经 `prependUserContext` 渲染成一条 `role:"user"+isMeta` 的 system-reminder，prepend 到**每轮**消息最前；一次会话内是快照（memoize，只在 /clear、compact、worktree 换/进、settings sync 时重读）。优先级 Managed(/etc) > User(~/.claude) > Project（FS 根→cwd 上溯，**近者优先**）> Local(CLAUDE.local.md)。
- **deny 无条件**：claude 判定序 = 整工具 deny → ask → 工具 checkPermissions → 内容 deny → 人机要求 → safetyCheck → **才轮到 bypassPermissions/allow**。所以 deny（.claude/settings 的 `permissions.deny`）压过 allow/ask/**full 全放行**，confirm 也救不回；内置 DANGEROUS 文件（.bashrc/.gitconfig/.gitmodules/.mcp.json/.claude.json…）与目录（.git/.vscode/.idea/.claude）在 full/auto 下仍是 `ask`（有交互=弹窗，无交互=拒）。
- **symlink 全链查**：权限判定对每条路径收集"词法原样 + 逐跳中间目标 + realpath 终值"，三者任一命中危险即拦；工作区边界用所有 resolved 形式判（cwd + additionalWorkingDirectories）。
- 引擎已具备、本轮不用重做的先例：命令硬拦截在 full 也不豁免（`gate.py:42-43`）、symlink 逃逸工作区硬包含（`scope.resolve_within`）、plan 只读、`set_mode` 热切换（policy 可跨模式保留）。
- **hooks 口径勘误**：引擎 `hooks/config.py` `_KNOWN_EVENTS`（12 事件）**漏了 `SessionEnd`**——worker `_fire_hooks("SessionEnd",…)`（`bridge/worker.py:750`）确实在发，但配置把 SessionEnd 当未知事件丢弃并告警，用户写了 SessionEnd 规则永不触发。属 bug 级小修。

## 增量账本（引擎内、renderer 中性）

| 块 | 缺口 | claude 行为要点 | 引擎现状 | 价值 | 最小落点 | 难度 |
|---|---|---|---|---|---|---|
| R-A | #8 项目指令文件注入（CLAUDE.md） | FS 根→cwd 每层读 `CLAUDE.md`/`.claude/CLAUDE.md`/`.claude/rules/*.md`/`CLAUDE.local.md`，@include 与 5 层上限，40k 软限不硬截 | `_system_prompt` 只用固定 SYSTEM_TEMPLATE+plan+memory，全库对 CLAUDE.md 零读取（唯一提及是 memory 里"别存 CLAUDE.md 已写的"）→ 项目约定/偏好/禁区对 DeepSeek 全不可见 | 高（成本最低的行为对齐） | `agent/prompts.py` 加 `build_instructions(workspace)`：读 workspace 层 `CLAUDE.md`+`.claude/CLAUDE.md`+顺带 `AGENTS.md`；`(mtime_ns,size)` 缓存；上限截断带提示；`_system_prompt` 在 SYSTEM_TEMPLATE 后、memory 前注入。上溯/rules/用户级/嵌套=裁 | 易-中 |
| R-B | #7 deny + 敏感文件/目录在 full/auto 仍拦 | DANGEROUS 文件/目录集合 + deny 规则无条件压过 bypass/confirm（safetyCheck=ask） | `authorize_write`：FULL 直接 `return`（gate.py:27-28）、AUTO 且 within → return（31-32），不判路径 → `.git/config`、`.bashrc`（工作区内）full/auto 静默可写 | 高（安全底线） | 新 `permission/policy.py`：内置敏感集合（.git/.vscode/.idea/.claude/.ember 目录大小写不敏感 + 危险文件名单）+ 极简 deny 表（env `EMBERPY_DENY` 恒读 + 项目 `.ember/permissions.json` persist 才并入，镜像 hooks 信任分层）；`authorize_write` 在 FULL/AUTO return 前插 deny（无条件）+ 敏感检查（`_ask` 人工批准、confirm=None fail-closed）。policy 随 Agent 传给子 agent | 中 |
| R-C | #18 symlink 词法+终值双查 | 每条路径查"词法+链+终值"，写逃逸多层都拦 | `scope.resolve_within` 只展开终值强包含（逃逸工作区已堵）→ 软链 `ws/link→ws/real/.git/config`（目标也在工作区内）终值无 `.git`，full 放行 | 中 | `fs._resolve` 顺带返回词法归一路径，gate 双查（canonical 或 lexical 任一命中敏感/deny 即拦）。叠在 R-B 上，成本≈0 | 易 |
| R-D | #6 Bash 目标解析加固 | 三件套：写路径安全(DANGEROUS)+只读放行判定+重定向目标越界检查；`$IFS`/CR/反引号混淆单独层 | 引擎只读白名单过松：`echo hi > /etc/cron.d/x`（echo 在白名单，整条 READ_ONLY 放行）、`rm /etc/passwd`（无 -r/-f → SAFE）、`sed -i /etc/passwd`、`curl -o /etc/cron.d/x`、`$IFS`/命令替换变量绕掉**全部**基于 `\b`+空格的规则 | 高（命令层关键洞） | `permission/rules.py` 加 shlex 目标解析：重定向目标（除 /dev/null）、rm/rmdir/chmod/chown/sed -i 的位置参数过 `_is_system_or_sensitive` 或 `is_within`（系统前缀 /etc /usr /bin /proc /dev…+ 敏感名 .git/.ssh/.aws/.bashrc…）；BLOCKED/DANGEROUS 判定；echo 只读收窄（含 `>`/`$(`/`` ` ``/`|` 不强判 READ_ONLY）；补 `\$IFS`/反引号廉价正则。parser 差分/规则持久化=裁 | 中 |
| R-E | #21 自定义 agent 定义文件 | `.claude/agents/*.md` frontmatter：name/description/tools/disallowedTools/model/maxTurns/permissionMode/initialPrompt/skills/…，正文=system prompt；子 agent 工具集按定义过滤 | `agent_tool.py` agent_type 是封闭 enum `general|explore`；无任何 agents 目录扫描；`build_subagent_registry` 只按 category 剔除 | 高（claude 造专用子 agent 的核心扩展点） | 新 `agents/loader.py`（复用 skills frontmatter 解析，扫 workspace 与 home 的 `.claude/agents`/`.ember/agents`，project>user 去重）；`run_agent` 动态枚举；`_spawn_subagent_inner` 按定义传 maxTurns/initialPrompt、按 tools/disallowedTools 过滤注册表。memory/skills/isolation/effort/hooks 子集=裁 | 中 |
| R-F | skills paths 条件激活 + 模型侧清单动态化 | 带 `paths` 的技能进 conditional 集（模型不可见），FileRead/Write/Edit **成功后**按触碰路径 gitignore 匹配激活，激活才进可用清单；cwd 以下深层技能目录随触碰动态发现 | `Skill.paths` 只解析不激活（注释明说）；`store.all()/listing()` 全量列出；`run()` 里 `schemas = registry.schemas()` **每轮只算一次**、循环内复用 → Skill 工具 description 在 Agent 生命周期冻结（store.refresh 只对命令面生效） | 中高（技能库变大后防噪；也解锁 R-E 清单热更） | SkillStore 加 conditional/activated + `activate_for_paths`；fs 的 read_file/write_file/file_edit 成功后回调 `env.skills.activate_for_paths([target])`；`run()` 把 schemas 移进每步重算（一处改动同时兑现 paths 激活 + agents 清单 + 已 claim 的 refresh 动态）。激活即注入正文/深层目录发现=后置 | 中 |
| R-G | hooks 增量 | 全集 26+ 事件；StopFailure（模型出错停止）、PermissionDenied（权限被拒）、PermissionRequest（弹权限询问前，hook 可代答 allow/deny/ask） | 已落地 12 事件缺 SessionEnd 白名单（bug）；StopFailure 无触发位；PermissionDenied 被 fs/shell/web/mcp 各 catch 吞成文本、denied 标志恒 False；PermissionRequest 无（gate._ask 是唯一询问漏斗但无工具上下文） | 中 | ① `_KNOWN_EVENTS` 补 SessionEnd（bug 小修）；② run() 出错 break 前 `_fire_hook("StopFailure",…)`；③ PermissionDenied：工具不再吞 Denied，改 `_execute` 统一 `except Denied` → 触发 hook + 返回 denied=True（顺带让 UI denied 标志变真，改 fs/shell/web/mcp ~4-6 处 catch）；④ PermissionRequest：`_execute` 把当前工具名+入参写入 env，gate._ask 前触发，hook 决策 allow→免问/deny→直接拒/ask→照旧。Notification 无 notifier/UI toast 通道 → 可后置或裁 | 中 |
| R-H | 小件 | FileRead 对 .ipynb 走 readNotebook 返回 cell+output markdown 化视图；TodoWrite 纯记账 | `read_file` 把 .ipynb 当 UTF-8 返回原始 JSON，模型难以使用；无 todo 状态 | 低-中 | read_file 内对 .ipynb 走 json→markdown 渲染（纯函数）；TodoWrite 可选。NotebookEdit cell 编辑/MCP resources/Task*/Cron/Worktree=明确裁 | 易 |

**明确继续不做的边界**（各块内已列）：Windows 恶意路径枪、settings 多源 deny、read deny、full 越界放宽/additionalWorkingDirectories、shell parser 差分、notifier/UI toast 基建、子 agent 挂 memory/skills/mcp 的复杂语义。

## ✅ 落地批次建议（已完成三波）

依赖关系：R-C 叠在 R-B 上；R-F 的"schemas 每步重算"同时是 R-E 清单热更前置。分 3 个小 wave 顺序落地，每 wave pytest + 相关桥测 + commit，不 push：

1. ✅ **Wave A（信息类，改动面最小）**：R-A CLAUDE.md 注入；R-G① SessionEnd 白名单 bug；R-H .ipynb 读取。→ `c5dad56`
2. ✅ **Wave B（安全硬拦）**：R-B deny+敏感集合（含 R-C 词法/终值双查）；R-D bash 目标解析加固。→ `aeb21ab`
3. ✅ **Wave C（扩展点）**：R-G②③④ hooks 增量 → R-F schemas 每步动态化 + skills paths → R-E agent 定义文件。→ `544b20a`

三波均完成并通过全量 pytest + host vitest 契约（`agent-host.python.test.ts` 5 passed）；
落地细节与验证见 MIGRATION-ROADMAP 十四节，未做边界见各 R-块与顶部「未动」清单。
