# emberpy 迁移路线图

把 `claude-code-analysis/src` 的优秀 coding-agent 功能逐步迁进 Python 引擎
（`runtime-py/emberpy`），**前端（Ember Electron/React UI）原样保留**。

> 参考源只用于搞懂"功能该长什么样、接口怎么设计"，**代码全部从零自己写**
> （版权红线，也是简历上把它当自己作品的前提）。

## 一、总原则（每块都遵守）

1. 参考源只看职责/接口设计，不照搬代码。
2. UI 契约是 **spec**：改动不得破坏 renderer 能 parse 的事件/命令面
   （以 `src/renderer/conversation.ts` 现有解析为准，必要时才扩）。
3. 每块三连验证：**pytest（离线假模型）→ vitest 桥探针（真 spawn python）→ Electron 真机**。
4. 红线：删文件 / 改密钥 / 装全局依赖等先问；commit 只在要求时；密钥只在 env。
5. 一块一里程碑，绿了再进下一块；向后兼容（旧 session JSONL / 旧命令响应不破坏）。

## 二、迁移路线图

| # | 功能块 | 复刻参考源 | emberpy 落点 | 前端/验收锚点 | 依赖 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| M0 | 域分包 + Live 桥 | — | 全部域 | — | — | ✅ 完成 |
| **M1** | 思考过程（thinking）渲染（最小非流式） | thinking 内容块建模 | `llm/` `agent/` `bridge/` `session/` | 折叠"思考"面板；reasoner 真机可见 | 无 | ✅ 完成 |
| M1b | thinking 流式增量（SSE） | thinking_delta / text_delta 分离 | `llm/chat.py` `bridge/worker.py` | 思考面板实时跳动 | M1 | ✅ 完成 |
| M2 | 语义文件编辑 FileEdit | `tools/FileEditTool` | `tools/fs.py` `patches/` | 工具轨迹显示 edit；/undo 撤销语义编辑 | 无 | ✅ 已落地（真机能力冒烟任务 A/B 已用 file_edit/write_file 跑通，此前"🔨 真机待验"为过时账目） |
| M3 | 会话持久化 v2 + resume/fork | `assistant/sessionHistory` + 侧栏 threads 契约 | `session/` `bridge/` | 重启后历史在、可恢复/分支旧会话 | M1 | ✅ 完成 |
| M4 | 记忆系统（memdir） | `memdir/` `services/SessionMemory` | 新建 `memory/` | 会话 A 记住 → 会话 B 自动生效 | M3 | ✅ 完成 |
| M5 | 技能包 skills/ + 命令面 | `skills/` `commands/` | 新建 `skills/` | 手写示例技能可调用 | 无 | ✅ 完成 |
| M6 | 计划模式闭环 + 确认对话框 | `EnterPlanModeTool` `AskUserQuestionTool` | `permission/` `bridge/ui.py` | plan 先出计划经 UI 确认再执行 | M2 | ✅ 完成 |
| M7 | 自动上下文压缩 compact | `services/compact` `query/tokenBudget` | `agent/core.py` `session/` `llm/` | 长对话自动压缩不丢语义 | M3 | ✅ 完成 |
| M8 | 子 agent / 任务系统 | `Task*Tool` `tasks/LocalAgentTask` | `tools/` + `agent/tasks.py` | 引擎可派生子 agent 干活 | M2 | ✅ 完成 |
| M9 | MCP 客户端 | `services/mcp/` `tools/MCPTool` | `tools/` 加 mcp 源 | 接本地 MCP 服务器可调用 | M2 | ✅ 完成 |
| M10 | 打磨项：cost 统计 / 图片多模态（Bash(n) 后台**跳过**：renderer/main 零 Bash 编号窗口消费面） | `cost-tracker` + 遗留 `src/extensions/vision.ts`（已不挂载） | `session/stats` `llm/usage.py` `tools/vision.py` | — | 视块 | ✅ 完成 |

执行：M1 起按序做，每块独立可验证；M4/M7 依赖 M3；M8/M9 后置；M1b/M10 按需挑。

## 三、M1 落地细节（thinking 渲染 · 最小非流式）

**目标**：把 DeepSeek reasoner 的推理文本（`reasoning_content`）渲染成 UI 里可折叠
"思考"面板；让"思考等级"旋钮真正生效（**等级=模型开关**）。

### 前端契约（不变更 `src/renderer/`）
- content part 形状 **`{type:"thinking", thinking:string}`**（`getThinking` 只认它）。
- 事件整消息携带 `message.content`；合并语义 = **text 整文本覆盖**、**thinking 追加 +
  按空行分段的包含去重** → 后端发累计全文或增量都安全。
- **`message_end` 必须带最终 text part**（否则 streaming 停不下来）。
- 等级与显示无关：UI 经 `set_thinking_level({level})` 下发、`get_state().thinkingLevel` 回读。

### 已落地改动（2026-09-08，runtime-py）
| 文件 | 改动 |
| --- | --- |
| `llm/base.py` | `AssistantReply.thinking`；`without_reasoning()`（回发前剥离推理） |
| `llm/chat.py` | 读 `reasoning_content`；reasoner 请求不带 temperature（`_is_reasoner()`） |
| `bridge/transcript.py` | `thinking_part()`；`add_assistant(..., thinking=)`，thinking 在前 |
| `bridge/worker.py` | thinking part 事件；`_resolve_model()` 等级=模型开关；`set_model`/`set_thinking_level` 双向同步 |
| `agent/core.py` | in-run 塞回上下文前剥离 reasoning |
| `session/core.py` | `resume_messages()` 回发前剥离（落盘保留） |
| `testing.py` | FakeLLM 脚本项支持 `"reasoning"` |

验证：pytest **94 passed 1 skipped**、vitest 桥 **2 passed**（进程契约未断）。

### 真机已验证（2026-09-08）
1. reasoner 含**工具轮**的任务正常：write_file → read_file 落盘、轨迹正确（无需回退）。
2. 思考面板渲染出的是**连续推理散文**（非动作日志、非占位），off 时无思考面板、on 时出现。
3. UI 默认 effort=medium → 会话自动 reasoner（思考开启、成本/延迟上升），off 才回聊天模型。

### 后置（不在 M1）
- M1b 真流式已单独立项完成（见下）；等级细分 effort：DeepSeek reasoner 无此能力，维持 off/on 语义即可。

## 四、M3 落地细节（会话持久化 v2 + resume/fork）

**目标**：python 引擎把会话写成 **Ember 索引器能识别的 JSONL**（现由 `src/main/ember-core.ts` 的
`listEmberThreads` 读取），放进它扫描的目录 → 侧栏列表/删除/置顶/重命名//undo 全走主进程
现有 IPC，`src/main/` 与 `src/renderer/` **零改动**（agent-host 早已支持 `--session`）。

### 落地改动（全在 runtime-py）
| 文件 | 改动 |
| --- | --- |
| `session/core.py` | Session 落盘格式升级 v2：新文件首行写 `{type:"session",version,cwd:<绝对路径>}`；assistant 消息落盘为 parts（thinking/text/toolCall，arguments 存原始 JSON 串）；工具写盘存 checkpoint **全文 before/after**（供跨重启 /undo）；改为"逐条 open(append) 写完即关"（Windows 上不挡索引器分区 rename/硬链）；首行非 session 头自动退回 v1 原样读写。新增 `resolve_sessions_dir()` / `allocate_session_path()`。**内存事件模型一行没改**——现有单测即回归锁 |
| `bridge/worker.py` | 会话生命周期：`--session` 启动即打开历史并 **replay 进转录**（snapshot/get_messages/get_entries 发消息前就返回完整历史，含 thinking 面板、可 /undo）；`_ensure_agent` 复用同一 Session 续写；桌面场景（有 `EMBER_SESSIONS_DIR`/`EMBER_HOME` env）**首个 prompt 惰性分配**会话文件，不产生空文件；`new_session` 换新 Session；fork/get_fork_messages 补最小原语（复制成独立文件，不切换现场） |
| `session/__init__.py` | 导出新增路径助手 |

### 验证（全自动）
- pytest **122 passed 1 skipped**（既有 ~109 + 新增 v2 单元 + resume 桥 13）。
- vitest **桥 5 passed**：跨 host 同 session 文件 resume 历史在并能续跑；无 `--session` + env 桌面场景首个 prompt 自动落盘；**真索引器 e2e** —— `listEmberThreads()` 列出 python 写的平铺 JSONL（node:sqlite 在本机可用）。
- tsc --noEmit 绿。

### 注记
- **fork 目前无 UI 消费者**（白名单有命令、renderer 无分支入口）→ 只做引擎层最小原语 + 测试，分支按钮留待后续。
- 会话文件 id/parentId/timestamp 等索引器严格解析字段未引入——读取 python 会话的一直是本 python worker，只需索引器兼容即可。
- resume 后新 agent 的 PatchStore 从 1 重新编号：worker 水位在 replay 后归零，`_sync_checkpoints` 能把新写盘补丁全部同步成 checkpoint（与历史 id 重复无碍，/undo 按"最后一个真实 user 之后的位置"定位、不依赖 id）。

## 五、M1b 落地细节（SSE 真流式 + message_update）

**目标**：模型答复**边生成边渲染**（含 reasoner 的 thinking 实时跳动），不再等整包返回。

### 实现（全在 runtime-py，renderer 零改动）
| 文件 | 改动 |
| --- | --- |
| `llm/chat.py` | 新增 `ChatLLM.stream_complete(messages, tools, on_delta) -> AssistantReply`：POST `stream:True`，httpx `client.stream` 逐行解析 SSE `data:`（到 `[DONE]` 停）；`reasoning_content`/`content` 各自分片累加成**整段**，**每次增量**回调 `on_delta(累计text, 累计thinking)`；tool_calls 按 index 拼接 arguments 分片成完整 JSON；末尾 choices 为空的 usage-only 块照收（DeepSeek 会发）；结尾返回与 `complete` 同形状的 `AssistantReply`（tool_calls 走同一 `parse_tool_calls`）。reasoner 仍不带 temperature |
| `agent/core.py` | `_call_model`：LLM 若实现 `stream_complete`（duck-type 探测）就走流式并把增量转成 `assistant_delta` 事件（携带累计 text/thinking）；`FakeLLM` 等替身没有该方法，自动落回整包 `complete`——**现有 ~131 个测试零改动** |
| `bridge/worker.py` | `_DynamicLLM` 补 `stream_complete` 代理；新增流式现场 `_streaming`；`_on_agent_event` 处理 `assistant_delta` → `_handle_assistant_delta`：突发首个可见增量新开文本段发 **message_start**，后续覆盖段尾发 **message_update**（每发都带**累计全文** text part + optional thinking part，renderer 整文本覆盖/joinThinking 去重幂等）；最终 `assistant` 事件（`_handle_assistant`）把段替换成权威最终文本再补一条累计 message_update 收口 → **窗口/转录最终形状与非流式完全一致**（thinking 只进转录一次）。纯工具轮/空增量不发可见气泡 |

### 为什么"增量必带累计全文"
renderer `conversation.ts`：assistant 事件若为最后一条就走 `mergeAssistant`（`text: incoming.text || old.text` 整文本覆盖、`thinking: joinThinking` 前缀去重追加）→ **发累计全文天然幂等**，不用为去重做增量心智负担。

### 验证（全自动）
- pytest **131 passed 1 skipped**（+9）：`unit/llm/test_chat_streaming.py` 用本机 stdlib HTTP 服务发真 OpenAI 风格 SSE（reasoning/content 分片累计、tool_calls arguments 分片拼接、并行 tool 各 index 独立、usage-only 尾块、`[DONE]`）；`integration/bridge/test_worker_streaming.py` 用 `StreamingLLM`（带 stream_complete 的假模型）驱动 worker，断言 message_start→message_update→message_end 时序、文本单调增长、message_end 带权威最终文本、thinking 收敛、多轮+工具轮窗口 join 语义、以及**非流式 FakeLLM 对照：整包 message_start、绝无 message_update**。
- vitest 桥 **5 passed**、tsc 绿：进程契约未断；真实 ChatLLM 缺 key 的失败路径现在走 stream_complete 抛错 → 错误助手消息 → agent_settled，行为不变。

### 注记
- 测试文件取名避免与目录内其它文件 basename 冲突（pytest 按 basename 去重）：unit 用 `test_chat_streaming.py`、bridge 用 `test_worker_streaming.py`。
- 真机待验一句话：reasoner 长回复应见思考面板逐字跳动、正文逐步出现，非"整块闪现"。

## 六、M4 落地细节（跨会话记忆 memory/memdir）

**目标**：模型可在会话 A 用 `memory_save` 存一条记忆，**新会话 B（另一进程/另一工作区只要同记忆目录）system prompt 自动带上索引**——跨会话偏好/项目背景不丢。

### 参考 claude-code-analysis 源码（只读职责/行为设计，代码自写）
精读了 `src/memdir/{paths,memoryScan,memdir,memoryTypes}.ts`（claude 记忆目录实现）。提取的行为设计：
- 目录布局 `<home>/projects/<sanitized-cwd>/memory/`，每条记忆一个 `.md`，`MEMORY.md` 是**纯索引**（无 frontmatter），每行一条 `- [Title](name.md) — 一句提示`；
- 索引 **200 行 / 25KB** 上限，超限截断 + WARNING 提示（本项目：list() 天然封顶 200 条 → 索引行数到不了上限，触达的是字节上限）；
- `type` 闭集 `user | feedback | project | reference`；frontmatter 存 `name / description / type`；
- 记忆提示（system 区块）含：何时保存 / 保存方式（两步行：写文件 + 更新索引）/ 四类型释义 / 不要存什么（代码可从项目推导的、git 历史、修 bug 解法、CLAUDE.md 已写、临时任务细节）/ 何时读 / **记忆可能过时须核实**；
- 目录懒建：读（list/read）在目录不存在时返回空、绝不 mkdir；首次 save 才建目录（不产生空目录污染）。

### 实现（全在 runtime-py，renderer 零改动）
| 文件 | 改动 |
| --- | --- |
| `memory/store.py`（新） | 文件模型：`resolve_memory_dir(workspace)`（`EMBERPY_MEMORY_DIR` 覆盖 → `EMBER_HOME/projects/<sanitize>/memory` → `~/.ember/...`）；`MemoryStore.list()`（最新在前、跳过 MEMORY.md、cap 200）、`read(name)`、`save(name, description, type, content)`（校验名字防穿越/防覆盖 MEMORY.md、校验 type、mkdir + `_atomic_write` 写 .md + 重建索引，返回 `SaveResult(created)`）、`index_text()`（截断 + WARNING）；`build_memory_prompt(store)` 中文记忆行为指南 + 当前索引（空索引给提示） |
| `memory/__init__.py`（新） | 导出 `MemoryStore / SaveResult / MemoryMeta / MEMORY_TYPES / build_memory_prompt / resolve_memory_dir` |
| `tools/memory_tools.py`（新） | `memory_save / memory_read / memory_list / memory_dir` 四个工具，**不经文件权限门**（写的是引擎记忆目录不是用户代码），名字由 store 校验；save 自动完成"写文件 + 更新索引"两步行 |
| `tools/registry.py` | `ToolEnv` 加 `memory: Optional[MemoryStore]` 字段（默认 None → 既有工具集断言不变） |
| `tools/__init__.py` | `default_registry`：`env.memory is not None` 才追加 memory_*（置于 shell 前） |
| `agent/core.py` | `Agent(memory=...)`；`env.memory is None and memory` 时回填；`_system_prompt` 把 `build_memory_prompt` 整段拼在 system 后（每次 run 现读索引 → 跨会话自动带上前几轮记忆） |
| `bridge/worker.py` | `persist`（桌面有 EMBER_HOME/EMBER_SESSIONS_DIR env）时才建 `self.memory = MemoryStore(resolve_memory_dir(workspace))`；`_ensure_agent` 的 env 挂 `memory=self.memory` |
| `cli.py` | 仅显式设 `EMBERPY_MEMORY_DIR` 才建 memory 并传 Agent（CLI 默认不碰 ~/.ember） |

### 验证（全自动）
- pytest **150 passed 1 skipped**（+19）：`unit/memory/test_memory_store.py`（save 建目录+frontmatter+索引、read 往返、覆盖不产生重复索引行、list 最新在前/跳过 MEMORY.md、目录不存在返回空且**不建目录**、非法名字/穿越/非闭集 type 拒绝、索引字节超限截断+WARNING、resolve_memory_dir env 优先级与 home 布局、空索引/有索引两种 prompt）；`integration/bridge/test_worker_memory.py`（① 会话 A 经 memory_save 落盘 → 会话 B 新 Worker 不同工作区同目录能 list 到、B 装配的 agent system prompt 记忆区块带索引条目 = "A 记住 B 生效"；② 无 env 时 registry 无 memory_*、system 无记忆区块=默认工具集不变；③ 启用时四工具就位、空索引提示、save 后区块带索引行）。
- vitest 桥 **5 passed**、tsc 绿：进程契约未断。

### 注记
- 索引"行数上限"实测不可达：`save` 用 `list()`（cap 200）重建，MEMORY.md 恒 ≤200 行；`index_text` 的行截断逻辑属防御分支，能实际触达的是 25KB 字节上限（200 条长 hook 时）。
- 记忆目录与"启用"是解耦的：测试/纯库用法只要不设 EMBER_HOME 就不建 store；集成测试用 `EMBERPY_MEMORY_DIR` 指向临时目录，绝不碰真 `~/.ember`。

## 七、M5 落地细节（技能包 skills/ + 命令面）

**目标**：让模型可加载一段"现成技能工作流"照做，用户也能用 /skill: 命令面点技能。复刻 claude
skills 的目录格式与触发语义，前端契约对齐 Ember 桌面端 get_commands -> parseSkillCommands。

### 参考 claude-code-analysis 源码（只读职责/行为设计，代码自写）
精读 `src/skills/loadSkillsDir.ts` 提取的行为设计：
- 目录式技能 = `<name>/SKILL.md`，嵌套子目录命名空间 `a/b` -> 名 `a:b`；
- 技能正文头部 frontmatter 描述行为（本引擎只取子集：name/description/version/when_to_use/
  arguments/allowed-tools/user-invocable/disable-model-invocation）；
- 触发 = 把技能 md 渲染成一段"本条助手轮次要遵守的工作流"注入上下文，不是 fork；
- 参数插值：$ARGUMENTS / 声明 arguments 时按空格位置映射 $名字；技能目录以正斜杠形式给出。

### 实现（全在 runtime-py，renderer 零改动）
| 文件 | 改动 |
| --- | --- |
| `skills/frontmatter.py`（新） | YAML 极简子集解析：标量 key:value（key 小写、剥成对引号）+ key: 下 ` - item` 列表；无 frontmatter / 缺闭合 / 坏行一律"全文当正文"，加载器宁丢字段不崩（技能是第三方手写文件） |
| `skills/models.py`（新） | frozen dataclass `Skill`（name/description/body/path/base_dir/source/…），`hidden` = not user_invocable |
| `skills/loader.py`（新） | `resolve_skill_roots`：`EMBERPY_SKILLS_DIR` 覆盖 → 单目录；否则 project = workspace 自身 + 直到 home（EMBER_HOME or ~）的祖先，探测 `.agents/skills`/`.claude/skills`；user = home_base 下 `<home>/.ember/skills` 等；home 以上归 project。扫描按真实路径去重；`<name>/SKILL.md` 名取相对根路径，根级单 `<x>.md` 也认 |
| `skills/store.py`（新） | SkillStore：同名多文件根优先级第一个生效；`get` 容错认唯一尾段（review:lint 输 lint）；`render` 输出 `Base directory for this skill: <posix>` 头 + 参数插值；`commands()` 对齐桌面契约 name=`skill:<name>`/source=`skill`/sourceInfo={path,baseDir}；`listing()` 限长限总量 |
| `tools/skill_tool.py`（新） | build_skill_tool：模型驱动 Skill 工具，参数 schema {skill, args}，**fn 形参必须叫 skill**；技能正文作为 tool 结果回上下文；disable-model-invocation 拒绝；category=READ |
| `tools/registry.py` | ToolEnv 加 `skills: Optional[SkillStore]`（默认 None → 既有断言不变） |
| `tools/__init__.py` | default_registry：env.skills 非空才挂 Skill 工具（空 store 不打扰模型） |
| `bridge/worker.py` | persist（桌面有 EMBER_HOME/会话目录 env）才建 `self.skills`；`_ensure_agent` env 挂 skills；`get_commands` 并入技能命令；`_expand_skill_message`：输入 `/skill:名 参数` → 渲染正文作任务喂 agent，**UI 转录保留原始 /skill: 文本**；技能不存在直接回文本不起 agent |

### 验证（全自动）
- pytest **169 collected，exit 0**（M5 新增 unit/skills/test_skill_store.py 14 例 + integration/bridge/
  test_worker_skills.py 4 例：发现/命名空间/去重/优先级、user-invocable=false 隐藏、frontmatter 列表与
  标志、description 回退正文首行、$ARGUMENTS 与具名参数与 CLAUDE_SKILL_DIR 渲染、根解析 env 隔离、
  工具门控、模型调 Skill / 用户 /skill: / 未知技能回文本不起 agent；M4 及更早全绿不回归）。
- vitest 桥 **5 passed**、tsc 绿。
- 真 repo 发现冒烟：根 = 仓库 `.agents/skills` + 用户 `~/.agents/skills` + `~/.claude/skills`，扫到
  5 个技能（continue-long-run、init-long-run、plan-then-act、ember-ui、mmx-cli），commands 正确列出。

### 注记 / 范围克制（有意不做）
- **inline shell 不执行**：claude skills 里那种暗藏命令不在 prompt 阶段静默跑；要跑命令由模型走
  shell 工具（含权限门），更安全。
- conditional / paths 等 frontmatter 字段已 parse 但**未激活**（留待后续）；allowed-tools 亦未做
  工具白名单收窄。
- CLI（cli.py）的技能 enable 未做：get_commands 命令面由 worker 覆盖，M5 验收锚点已达成。
- 技能只在有持久 env（EMBER_HOME/会话目录）时启用，纯库/无 env 用法不受影响（与 M4 memory 同款门控）。

## 八、M6 落地细节（计划模式闭环 + 确认交互）

**目标**：用户能"先出计划、UI 批准、再真动手"，与 claude plan mode 的
EnterPlanMode → 列计划 → ExitPlanMode/批准 语义对齐。**关键判断**：Ember renderer
自己就是模式控制器——渲染端切权限模式时把 `/permissions <mode>` 当作**普通 prompt 文本**
发给引擎，InspectPanel「批准」发 `/plan execute`；引擎只需把这两条识别成引擎级文本命令，
再挂上列计划/澄清用的工具与 plan 模式 system 指引，前端零改动闭环即可成立。

### 参考 claude-code-analysis 源码（只读职责/行为设计，代码自写）
- `src/permission/index.ts`：plan 是权限门的一种模式（read/write/shell 被拒），非独立工具；
  ExitPlanMode 是模型工具但批准动作最终由用户驱动。
- `EnterPlanModeTool`/`AskUserQuestionTool`：前者把任务从"执行"切到"先计划"，后者向用户问
  单选澄清；renderer 的 InspectPanel 从 update_plan / Plan 类工具调用里抽待办渲染批准区。
- Ember 渲染端 collectTodos 契约：**从 assistant 消息的 toolCall 内容块里找 /plan/i 工具
  调用，取其 `args.plan`（{step,status}[]）**。引擎的 update_plan 工具正是喂这个结构。

### 实现（全在 runtime-py，renderer 零改动）
| 文件 | 改动 |
| --- | --- |
| `permission/`（既有） | effective_mode / PermissionGate 已支持 plan 只读门控，M6 复用 |
| `tools/plan_tool.py`（新） | `update_plan`：category=READ（永远可调），参数 `{plan:[{step,status}], summary}`，校验后回显步数——渲染端 collectTodos 解析的就是这个 |
| `tools/ask_tool.py`（新） | `AskUserQuestion`：单选澄清；有 `ask_value` 回调才挂。标题行 `「<header>」<question>`，options 2..4 项 |
| `tools/registry.py` | ToolEnv 加 `ask_value: AskValue`（默认 None → 既有断言不变）；确认交互与 AskUserQuestion 分离 |
| `tools/__init__.py` | default_registry：update_plan **恒挂**；AskUserQuestion 仅在 env.ask_value 非 None 时挂 |
| `agent/prompts.py` | SYSTEM_TEMPLATE rule 4 提及 AskUserQuestion/update_plan；新增 `plan_mode_guide()`：只读调研 → AskUserQuestion 澄清 → update_plan 列计划 → **停下等批准**，明说别调 write/file_edit/run_command |
| `agent/core.py` | `set_mode()`（实时切 gate + 下轮 system prompt）；system prompt **每轮都发**（原先 plan 模式被跳过——潜在 bug，M6 顺带修），plan 模式下拼 plan_mode_guide |
| `bridge/ui.py` | UiConfirm.select 卡片往返：`_request(method=select)` 发 extension_ui_request → `_wait(rid)` 收 extension_ui_response |
| `bridge/worker.py` | 引擎级文本命令 `/permissions <mode>`（切权限，纯状态、不建 agent/会话/落盘）、`/plan execute`（批准：退出 plan 恢复批准前模式，喂 PLAN_EXECUTE_TASK 让 agent 开跑）；`_apply_permission_mode` 进出 plan 记/清 `_mode_before_plan`；`_ensure_agent` env 挂 `ask_value=self.ui.select`；`_start_prompt` 对 /permissions 跳过提前建 agent |

### 验证（全自动）
- pytest **183 passed, 1 skipped，exit 0**（M6 新增：unit/tools/test_plan_tools.py 9 例
  update_plan 校验回显/AskUserQuestion 钩子门控与单选用例；unit/agent/test_plan_prompt.py 3 例
  plan 指引附加/set_mode 实时切换；integration/bridge/test_worker_plan.py 3 例：
  /permissions plan 纯状态不建 agent、plan 轮 update_plan 列计划→/plan execute 批准开跑恢复
  auto、AskUserQuestion select 卡片往返点选回执）。
- vitest 桥 **5 passed**（真 spawn + 索引器 e2e 不回归）、tsc 绿。

### 注记 / 范围克制（有意不做）
- **不落 EnterPlanMode/ExitPlanMode 模型工具**：renderer 已是模式控制器，再给模型一个
  "进 plan"工具会跟渲染端打架；引擎只认 UI 侧的 `/permissions`/`/plan execute` 文本命令。
- **无 plan 文件持久化**：计划本身在上下文里，批准即执行，不落 `plan.md`；后续如需
  todo 落盘可再补。
- AskUserQuestion 只做**单选**（AskUserQuestionTool 契约的多选原留给 M10 打磨；M10 复查 renderer 无多选消费面 → 维持单选，不扩）。
- 计划模式实现沿用现有会话模型；UI 待办渲染依赖 renderer 侧已有 collectTodos 解析 /plan 工具。

## 九、M7 落地细节（自动上下文压缩 compact）

对齐 claude `services/compact` + `query/tokenBudget` 的行为设计（**参考其意图与接口，代码自写**，已按本会话 user 要求通读相关源码）。

### 触发与契约
- **proactive 自动触发**：每轮 `_execute_prompt` 在「回显用户消息之前」调 `_auto_compact_needed()`；压缩会重建转录，必须先压、后 echo，界面顺序仍是「(摘要)…用户本轮」。
- **阈值**（对齐 claude `getEffectiveContextWindowSize`/`getAutoCompactThreshold`）：`threshold = contextWindow − min(maxOutput, 20_000) − 13_000`，即给压缩摘要输出 + 安全缓冲留空间。
- **token 粗算回退**：本引擎无 usage 统计，按 中文 ÷3 字符/token 估算（claude 英文 ≈÷4）；单位错误已修：字符数走 `_chars_to_tokens`，不再 `len(int)`。
- **手动 `/compact`**：dispatch 同步执行 `_compact_history(auto=False)`，对话太短抛 `nothing to compact: …`（renderer 正则 `/nothing to compact|session too small/i` 转 compactTooShort toast）。压缩成功后 renderer 会重拉 get_messages/get_session_stats/sessions。
- **`set_auto_compaction {enabled}`**：桌面端 startAgent 每次都会发 true 打开。

### 摘要回填与边界（与 claude 的差异，有意裁剪）
- **保留"最后一个 user 及之后整轮"原样**，只压缩它之前的旧段：claude 全量压缩不留 verbatim 尾部、靠附件重注入恢复近期上下文，我们没有这套机制 → 保留最近现场更稳。
- 摘要用 **assistant 角色消息 + `＞ 已压缩 N 条…` 中文 marker 行** 回填（claude 用 user+`isVisibleInTranscriptOnly` 隐藏位，Ember renderer 无该位 → 如实可见）。
- **无 compact_boundary 水位标记**：旧消息物理移除后，水位之后=全部当前消息，重复压缩只处理新增段，语义天然成立。
- **不做 plan 模式自动压**（只读调研别在批准前被压）；**熔断**：连败 ≥3 停手（避免每轮烧模型重试 prompt_too_long 场景），成功清零。
- MIN 旧段下限 600 token，env `EMBERPY_COMPACT_MIN_TOKENS` 可覆盖（压测缩阈）。

### 代码落点
- `compact.py`（新）：估算（`estimate_tokens`/`estimate_messages_tokens`/`_chars_to_tokens`）、`effective_context_window`/`auto_compact_threshold`/`should_auto_compact`、9 段式中文 `COMPACT_SYSTEM_PROMPT` + `build_compact_messages` + `summarize`（无工具、空摘要抛 RuntimeError）、`summary_text` marker、`split_old_events`（借 `session.core._last_user_event_index`）、`old_segment_messages`（= `closed_model_messages`）、`env_int`/`min_compact_tokens`。
- `session/core.py`：抽模块级 `closed_model_messages`/`_last_user_event_index`；`resume_messages` 复用它；加 `replace_events`（v2 时原子 temp+os.replace 全量重写磁盘）。
- `session/__init__.py`：导出 `closed_model_messages`。
- `patches/store.py`：加 `high_water`（`_sync_checkpoints` 水位抬升用，防压缩后旧补丁重放）。
- `bridge/worker.py`：`auto_compact_enabled`/`_compact_failures`/`_context_window`(env `EMBERPY_CONTEXT_WINDOW`)；dispatch `compact`+`set_auto_compaction`；`_execute_prompt` 插入 proactive 触发（routing 命令/plan/skill 错误路径不建 agent 也不压缩）；`_compact_history(auto)` 双路径；`_echo_user` 抽出复用。

### 验证（全自动）
- pytest **全绿**：unit/test_compact.py 12 例（估算含 tool 参数、切段保留最后一轮 user、threshold=42808@64k、prompt 形状、FakeLLM 摘要无工具/空抛错、marker 文案）+ integration/bridge/test_worker_compact.py 4 例（手动完整周期 rewrite+续跑、too-short nothing to compact、auto 触发/开关、连败 3 熔断）。
- vitest 桥 **5 passed**、tsc 绿（无回归）。

### 注记 / 范围克制（有意不做）
- 压缩阈值的字符估算口径仍自洽；M10 起引擎对真实模型调用累计**精确 usage**（见十二），context 圆环用最近一次 prompt_tokens，两套并存不冲突。
- 摘要 UI 折叠位、compact 进度 toast、`/compact` 带自定义指令均属 renderer 侧已有/未来能力，本块只做引擎契约。
- 手动 compact 不重复摘要已存在轮次（物理删除旧段），语义上不会二次压缩同一批。

## 十、M8 落地细节（子 agent / 任务系统）

### 命名澄清（claude 源码调研结论）
claude 的 `TaskCreate/Get/List/Update` 工具是 **TODO 列表**（写 plan 步骤并展示到侧栏），不是子 agent 派发器，Ember renderer 已有 plan 模式 + `update_plan` 覆盖该面；真正的派发器是 **AgentTool spawner**（`run_agent`，同步取子 agent 最终文本回父），配 `TaskOutput`/`TaskStop` 管理**后台**任务。Ember 是 chat-queued、turn-based 引擎（桌面 prompt 来了才跑、无自主后台 loop）→ **只落地 run_agent 的同步派生路径**，后台任务生命周期面（TaskOutput/TaskStop、完成后的自主续跑）有意不做。

### 设计
- 工具名 `run_agent`，参数 `{description(必填), prompt(必填), agent_type: general|explore(默认 general)}`，category **READ**（结构上避免子 agent 派生被父级权限兜底拦，纯属白名单位置）。
- **general**：完整写/读/shell 工具池；**explore**：只保留 READ category 工具（read_file/list_dir 等），无 write/shell → 只读调研。
- **子 agent 独立会话**（新 `Session(cwd=workspace)`，只有一条 user=prompt，看不到父历史），**独立重建 registry**（从 `build_subagent_registry` 过滤），**绝不能递归派生**（run_agent 从子池移除 + `allow_subagents=False`）。
- 子 agent 与父 **共享 gate 与 PatchStore**（undo 同账本、改文件可 /undo）；不继承 confirm 问询回调 → 子 agent 一律 fail-closed。
- 返回：子 agent 最后一条 assistant 文本作为 run_agent 的 tool result 注入父上下文；异常/中断/步数上限 → 返回错误文本（带前缀标记）。
- 开关：Agent 新参 `allow_subagents: bool=False`、`subagent_llm`；worker/cli 开 True；子 agent LLM 默认复用父 llm。system prompt 在开时追加子 agent 使用指引段。
- 对 renderer：无新事件类型，就是一条普通工具轨迹（tool_execution_start/end toolName=run_agent）→ 主进程/渲染端**零改动**。

### 代码落点
- `tools/registry.py`：加 `ToolRegistry.all()`（保持插入序）。
- `tools/agent_tool.py`（新）：`SUBAGENT_TOOL_NAME="run_agent"`、`AGENT_TYPES`、`build_agent_tool(spawner)`、`build_subagent_registry(base, agent_type)`。
- `agent/core.py`：`allow_subagents`/`subagent_llm` 参数、registry 挂 run_agent、`_spawn_subagent` 同步跑子 agent、system prompt 指引段。
- `bridge/worker.py` + `cli.py`：Agent 构造传 `allow_subagents=True`。
- `testing.py`：`make_agent` 透传 `llm/subagent_llm/allow_subagents`（子脚本与父脚本分离，避免嵌套挤同一 FakeLLM）。

### 验证（全自动）
- pytest 全绿：unit/agent/test_subagent.py 5 例（开关 / general 独立上下文+结果回父 / explore 池只读 / explore 正常跑 / 子写文件进共享 PatchStore）+ integration/bridge/test_worker_subagent.py 1 例（整条事件链路：主 prompt→run_agent→子答复成工具结果→主答复；主会话事件仅 4 条，子会话临时）。
- vitest 桥 **5 passed**、tsc 绿。

### 注记 / 范围克制（有意不做）
- 不落后台任务面（TaskOutput/TaskStop/完成自主续跑）——Ember 无自主 loop，见上命名澄清。
- 不做父历史片段传给子 agent（同步单 prompt 够用）；不带 include_tool_use/权限代答。
- explore 的精确工具集跟随 default_registry 的 READ 分类走，新 READ 工具自动进子池（策略式，非硬编码清单）。

## 十一、M9 落地细节（MCP 客户端）

### 设计（参考 claude services/mcp，代码自写）
- **只做 stdio 本地 server 的最小集**：Explore agent 精读 claude `services/mcp/{client,config,types,normalization,envExpansion}.ts` 等，提取行为设计；落地时砍掉 OAuth/远程 HTTP-SSE/企业 allowlist/.mcp.json 审批/连接管理 UI——那是 claude 桌面面。
- **纯 stdlib 实现**（不引入 mcp/pydantic 依赖，pyproject 仍只有 httpx）：MCP stdio transport = 单行 JSON-RPC 2.0（非 LSP Content-Length）。新包 `mcp/`：`config.py`（读配置）/ `client.py`（`StdioMcpSession`：spawn 子进程 + 握手 + 同步 request）/ `manager.py`（`McpManager`：命名、发现缓存、路由、失败降级）。
- **协议面**：`initialize`（protocolVersion 2024-11-05、capabilities.roots）→ 收 result 读 capabilities → `notifications/initialized`；应答 server→client 的 `roots/list`（返回工作区 file:// uri）；`tools/list` 首次拉一次缓存；`tools/call` 参数对象原样透传；stderr 单独 pipe 只做日志（上限 64KB）不混协议通道；退出清理 terminate→kill 升级。
- **命名**：全名 `mcp__<server归一化>__<tool归一化>`（非法字符→`_`），第二段才切一次（tool 名内可含 `__`）；调用按前缀解析回 (server, 原始 tool 名) 路由。
- **权限**：新 `ToolCategory.EXTERNAL` + `gate.authorize_external`——FULL/AUTO 放行（显式配置该 server = 信任它干活，与内置写工具在 AUTO 工作区放行同一信任级）、ASK 询问、PLAN 拒绝（plan 是只读调研期，外部副作用不可知）、无 confirm fail-closed。对 renderer 零改动：就是一条普通工具轨迹。
- **失败语义**：单个 server spawn/握手/拉工具失败 → 跳过它、其它照常、启动不崩；配置空 = 整体无 MCP（不 spawn 任何进程）。**断线自愈**：调用时发现进程已退出 → 自动重连一次（新 spawn+握手+重拉工具）再发；工具级错误/远端 isError 转成文本回模型。
- **结果归一化**：content text 块拼接 / structuredContent JSON / isError 前缀"错误（MCP 工具…）"；非文本块（image/audio/resource）给可读占位不内联二进制；大结果截断 60k。
- **配置与启用**：唯一开关 env `EMBERPY_MCP_CONFIG` → JSON（`{"mcpServers":{name:{command,args,env}}}`，复用 claude .mcp.json 形状）；command/args/env 支持 `${VAR}`/`${VAR:-def}` 展开；server 名 `^[a-zA-Z0-9_-]+$`。无 env → worker/cli 完全不建 MCP。

### 代码落点
- `mcp/`（新包）：`config.py`（`read_config`/`manager_from_env`/`McpServerConfig`）、`client.py`（`StdioMcpSession` + `McpError`/`McpConnectionError`：连接状态机、握手、同步 request 循环内就地应答 server request、tools/list 缓存、call_tool、stderr 收集、close 幂等、reconnect）、`manager.py`（`McpManager`：ensure_ready 幂等连接+发现、tool_entries、invoke 路由+断线自愈、failures、close、命名 `normalize_name`/`split_full_name`/`_clean_description`/`_safe_schema`）。
- `permission/gate.py`：`authorize_external`；`tools/registry.py`：`ToolCategory.EXTERNAL` + `ToolEnv.mcp`。
- `tools/mcp_tools.py`（新）：`build_mcp_tools(env)` 把发现工具注册成 Tool（category EXTERNAL、fn 内授权 + manager.invoke + 截断）；`tools/__init__.py` default_registry 有 mcp 才挂（放工具表最后）。
- `bridge/worker.py`：`self.mcp`（manager_from_env）+ ToolEnv 挂 mcp + `close()` 释放子进程（main stdin 结束后调）；`cli.py` 同口径挂 mcp。
- 子 agent 隔离：`_spawn_subagent` 的 child_env 不挂 mcp → run_agent 子 agent 工具集绝无外部 MCP 工具（测试断言）。

### 验证（全自动）
- pytest **245 passed 1 skipped**（M9 +37：unit/mcp 配置 8 + client 8（握手/发现/call 参数透传/isError/structured/进程退出/非 JSON 污染/重连/close 幂等）+ manager 11（命名 roundtrip/描述折叠截断/schema 兜底/发现幂等/坏 server 跳过/调用路由/未知名/断线自愈）+ agent 8（工具暴露+auto 实调/权限 ask 拒/ask 批/plan 拒/full/无 mcp 无外部工具/explore 子池滤 EXTERNAL/run_agent 子 agent 无 MCP）+ worker 桥 2（无配置不建 MCP、有配置整条工具轨迹转发到 tool_execution_end）。**离线无网络**：全用 tests/fake_mcp_server.py（stdlib stdio JSON-RPC 假 server）spawn 真进程。
- vitest 桥 **5 passed**、tsc 绿（前端契约未断）。

### 注记 / 范围克制（有意不做）
- 不做远程 transport（http/sse/ws）、OAuth/needs-auth、企业 allow/deny 策略、`.mcp.json` 逐项目审批、`/mcp` 命令面与连接管理 UI（renderer 无此面）。
- 不做运行中工具列表热更新（`notifications/tools/list_changed`）：MCP server 由引擎 spawn 管理、工具列表在连接时发现一次缓存；断线自愈重连会重拉。留注释作扩展点。
- 描述截断 2048 / 大结果截断 60k 是简化口径（claude 描述 2048、结果按 token 估算落盘），够离线与一般 server 用。
- MCP server 是外部受信进程：信任根在"用户显式把它配进 EMBERPY_MCP_CONFIG"；引擎无法预知 server 行为，只能靠 EXTERNAL 权限门约束触发时机（不静默、plan 拒、ask 问、auto/full 放行）。

## 十二、M10 落地细节（cost/usage 统计 · 图片多模态）

M10 三个可选打磨项评估后落地两块、跳过一块：

| 项 | 结论 | 依据 |
| --- | --- | --- |
| cost/usage 统计 | ✅ 做 | renderer `Context & usage` 面板（ui.tsx `cacheHitRate`/context 圆环）真实消费 `AgentSessionStats.tokens/cost/contextUsage`；此前 `_get_stats` 全硬编码 0 |
| 图片多模态 | ✅ 做（vision 工具） | renderer 粘贴图 → `[vision]` handoff 文本带 uploads 绝对路径 → 要求模型"先调用 vision 工具查看"；引擎此前无 vision 工具，旧 Node 扩展（src/extensions/vision.ts）已不挂载 → 端到端没人真正识图 |
| Bash(n) 后台 | ⏭ 跳过 | renderer/main 对 Bash 编号窗口零消费面；引擎 turn-based 串行工具执行，无后台 loop |

### A. cost/usage 统计

**口径**（对齐 claude cost-tracker + Anthropic usage 语义，Explore 调研结论）：
- input **不含** cache；cache read / cache write 单列；context 占用 = input+cache_read+cache_write（不含 output）；cost = Σ token/1e6 × 每 M 单价分量相加（input/output/cacheRead/cacheWrite 各价）。
- DeepSeek（OpenAI 兼容）usage → Anthropic 语义归一：`prompt_cache_hit_tokens`→cacheRead、`prompt_cache_miss_tokens`→cacheWrite（未命中即写缓存）、fresh input = max(0, prompt_tokens−hit−miss)（DeepSeek 恒≈0）；无 cache 字段的网关整个 prompt 当 input。

**代码落点**
- `llm/usage.py`（新）：`UsageTracker`——`track(usage)` 归一化+累计（input/output/cache_read/cache_write/cost/最近 prompt_tokens）、`merge(other)`（子 agent 并入父）、`tokens_dict()`（total 含 cache，对齐 totalTokens）、`context_usage(window)`（percent 饱和 0–100，没跑过模型返回 None）。默认单价 USD/1M：input 0.27 / output 1.10 / cache_read 0.07 / cache_write 0.27（DeepSeek 官方价，可 env `EMBERPY_COST_{INPUT,OUTPUT,CACHE_READ,CACHE_WRITE}_PER_MT` 覆盖；价格非密钥）。
- `agent/core.py`：`self.usage`；`run()` 主循环每次 `_call_model` 后 `track(reply.usage)`；`_spawn_subagent` 里 `child.run` 后 `merge(child.usage)` → 子 agent 的 token/cost 计入父会话。
- `bridge/worker.py`：`_get_stats` 有 agent 且 `usage.any_usage` 时填 `tokens/cost/contextUsage`；无真实用量保持全 0、不发 contextUsage（UI 走"首次回复后出现"fallback）。`_available_models` contextWindow 统一改用 `self._context_window`（env `EMBERPY_CONTEXT_WINDOW` 可覆盖），不再写死 64000。
- `testing.py`：FakeLLM 脚本项可带 `"usage"` 模拟真实模型用量（缺省全 0 → 旧用例零改动）。

### B. 图片多模态（vision 工具）

**链路**（Explore A 调研）：粘贴图 base64 缩略 → `vision:stage` 落盘 `userData/uploads/*` → 文本 `​[vision]<用户原话>…先调用 vision 工具查看：\n- <abs 路径>` 发给引擎 → 模型应"调用 vision 工具"识图。引擎此前无该工具 → 补齐。

**代码落点**
- `tools/vision.py`（新）：`build_vision_tool(env)` 注册固定小写名 **`vision`**（renderer 按字面量识别成"识图"轨迹，conversation.ts `tool.name === "vision"`）。参数 `{paths[], prompt?}`（required paths）。fn：白名单 roots（uploads 目录 + workspace，`EMBERPY_VISION_UPLOADS` 可覆盖）isVisionReadable 等价检查 → 读文件 base64 data-URI → POST 配置的 chat/completions（OpenAI 兼容 image_url，httpx 180s）→ 提 content 文本回模型。category READ。
- 配置：env `EMBERPY_VISION_CONFIG`（测试/覆盖）→ 桌面主进程写下的 `vision-config.json`（`<appData>/Ember/`，Windows `%APPDATA%`/mac `~/Library/Application Support`/Linux `~/.config`）→ 默认 GLM 端点 + env key（custom 用 `ZHIPU_API_KEY`、deepseek 用 `DEEPSEEK_API_KEY`）。主进程把它归一化成顶层 `{provider, endpoint, model, apiKey}` → 引擎直接消费，**src/main 零改动**。
- `tools/__init__.py`：default_registry 在 shell 后、mcp 前常驻挂 vision。

**范围克制（有意不做）**：无 key 时**不内置 MinerU 免费 OCR** fallback（Node 扩展有异步上传+轮询+回读，多跳外部服务；返回引导"设置→图片识别 配 key"）；`details`（轨迹识图 chips/engine 标签）不随工具回传——引擎工具轨迹只有文本结果，renderer 视作普通"识图"轨迹，核心识别文本照常给模型；工具常驻（不随"本轮有图"动态显隐）——fn 无图/白名单外/无 key 都返回带"错误"前缀的引导文本，模型学到有图才调。

### 验证（全自动）
- pytest **全绿**：unit/llm/test_usage.py 10 例（DeepSeek 归一/tokens/cost 分量/跨次累加/None 忽略/context percent/饱和/最近一次 prompt/env 覆盖/merge）+ unit/agent/test_usage_agent.py 4 例（单轮 track/工具轮累加/无 usage 空/子 agent 并入父）+ integration/bridge/test_worker_stats.py 2 例（真实 usage 填 stats、空会话全 0 无 contextUsage）+ unit/tools/test_vision_tool.py 7 例（名字/schema、真调 fake 视觉 API 返回识别文本、无图引导、白名单拦截、无 key 引导、文件缺失、agentic 端到端）。测试全离线：fake 视觉 API 用 stdlib ThreadingHTTPServer。
- vitest 桥 **5 passed**（进程契约未断）。

### 注记
- cost 数值是 DeepSeek 官方价近似 + env 可覆盖；面板里 `cost` 当前 renderer 未展示（契约字段保留），tokens/context 圆环即时可见。
- Bash(n) 如未来 UI 加 Bash 窗口编号，再按 claude `BashTool` 的并发窗口命名补；引擎 `run_shell` 已是单窗口语义。

## 十三、M11 补齐 P1–P6（claude 能力差距落地）

承接任务 #46 的全量对照账本（docs/GAP-ANALYSIS.md 全量复查节），把引擎内可独立落地
且价值明确的缺口分 6 阶段实现。**边界**：全部改动在 `runtime-py/emberpy/`，renderer
与 main（TS）零改动——联网走 host 已写好的 `~/.ember/web-search.json`、图片视觉走
host 的 `vision-config.json`；renderer 认的工具名（web_search/fetch_content/glob 等）
已按 src/renderer/conversation.ts 的卡片/轨迹识别核对。用户确认的三项决策：① 六阶段
全做；② 写前必读 = **严格强制**；③ hooks 信任 = 用户级恒读 + 项目级需
`EMBERPY_PROJECT_HOOKS=1`。

| 阶段 | 内容 | 提交 |
| --- | --- | --- |
| P1 | 本地文件/搜索工具升级：glob 工具、grep output_mode(content/files/count)、read_file 行分页、**严格写前必读 + 重复读去重** | 8c37c4d |
| P2 | 联网工具：`web_search`/`fetch_content`（读 host web-search.json 的 key，brave/tavily/jina/exa 四 provider；EXTERNAL 权限；无 key 引导） | ea671b6 |
| P3 | API 健壮性：chat.py 有限重试 + 指数退避 + SSE 看门狗；超限自适应恢复（裁旧段重发一次）；token 估算按内容类型分权 | adc3fe5 |
| P4 | Skills：frontmatter `context: fork` → 隔离子 agent 执行（正文不进主上下文、共享 PatchStore、无 spawner 回退 inline）；`SkillStore.refresh()` 动态发现 | 727ece3 |
| P5 | Hooks：新 `hooks/` 包（config + runner），Agent 触发 PreToolUse/PostToolUse(./Failure)/SubagentStart/Stop/Stop，Worker 触发 SessionStart/End/UserPromptSubmit/PreCompact/PostCompact | 94daefa |
| P6 | 记忆两级相关召回：`build_memory_prompt(store, task)`，task 非空且索引 >60 条时按字符重叠挑 top ≤3 条全文注入 | d997499 |

### P1 先读后改（决策：严格强制）
- `ToolEnv.read_state` 记录 (mtime_ns, size)；`read_file` 全量/分页读后登记。
- 已存在文件未 read 过 / 读后被外部改动（指纹变）→ `write_file`/`file_edit` 拒绝并引导先读。
- 连续重复读同一未变文件 → 返回 `file_unchanged` stub（省 token）。
- 回归：既有 FakeLLM 脚本"直接对 fixture 写/改而没先读"的用例补 read_file 一步。

### P2 联网工具
- `web_search`：按 `WEB_SEARCH_KEY_FIELDS` 顺序挑第一个有 key 的 provider 直调其 HTTP API，
  结果回 `- [标题](url) + 摘要`（renderer 从 markdown 链接提取 sources 卡）。无 key 引导去
  「设置 → 联网搜索」。
- `fetch_content`：httpx GET，HTML 剥标签简易文本化，~150KB 上限。
- category=EXTERNAL：fn 内先 `gate.authorize_external`（FULL/AUTO 放行、PLAN 拒、ASK 问）。
  explore 子 agent（只留 READ）不带联网工具。

### P3 API 健壮性
- 仅 408/409/429/5xx 重试，3 次指数退避，尊重 Retry-After；httpx timeout 拆分 + 流式 idle 看门狗。
- 400 body 含 context/exceed 特征 → `LLMError.context_exceeded`；`Agent.run` 兜底**一次**裁旧段重发。
- compact 估算：JSON/tool 段 /2、普通文本 /3，触发线更准。

### P4 Skills fork + 动态发现
- `Skill.context: fork` → `fork=True`；ToolEnv 增 `spawner`；allow_subagents 时先挂 spawner 再建默认
  注册表（顺序敏感）。fork 技能有 spawner → 渲染正文当 prompt 跑 general 子 agent（子事件不外发、
  文件写进共享 PatchStore）；无 spawner 回退 inline。
- `SkillStore.refresh()` 原地重扫；`Agent.run` 开始处刷新，会话内新技能即时可见。

### P5 Hooks（配置 + 触发点）
- `<home>/.ember/hooks.json` 恒读；`<workspace>/.ember/hooks.json` 仅 `EMBERPY_PROJECT_HOOKS=1`。
- 兼容 claude 现代 `{"hooks": {...}}` 外壳与事件名直接放顶层两种形状；事件名用 claude 规范名
  （事件去空白后按 `_KNOWN_EVENTS` 白名单判定）；只实现 `type: command`，其余告警忽略。
- runner：shlex/自写 Windows 分词 → subprocess **不经 shell**；stdin 喂事件 JSON；注入
  HOOK_EVENT/CWD/PERMISSION_MODE/TRANSCRIPT_PATH；退出码 0=回灌文本、2=阻塞、其它=非阻塞错误；
  stdout JSON 支持 `decision:block`/`continue:false`/`modifiedPrompt`；超时 60s；输出截 10k；
  子进程注入 PYTHONUTF8=1 保证中文 stdin/stdout 可预测。
- Agent：PreToolUse（matcher 拒 → denied、fn 不跑）→ fn → PostToolUse / 异常 → PostToolUseFailure；
  SubagentStart/Stop 包 `_spawn_subagent`；run 收尾 Stop。
- Worker：UserPromptSubmit（阻塞 → 回文本不起 agent；modifiedPrompt → 以改写文本跑）；
  SessionStart/End（_ensure_session/_new_session/close/_open_session）；PreCompact/PostCompact
  包 `_compact_history`。仅在桌面场景（persist）装配 → 测试/纯库不受真实用户配置干扰。
- 安全：命令不经 shell、项目级默认关、文件解析宽容（坏配置进 warnings 不拖垮 worker）。

### P6 记忆两级相关召回
- 索引（MEMORY.md）常驻行为不变；`build_memory_prompt(store, task="")` 在 task 非空且索引
  >60 条时，按 task × (name+description) 的确定性字符重叠打分（ASCII 词 + 中文双字组），
  top ≤3 条读全文（单条截 2000）附「## 与本任务最相关的记忆」；task 空/记忆少/零命中全回退。
- Agent 两处 system prompt 构建把当前用户任务传入。

### 验证（全自动）
- P1–P6 每阶段 pytest 全量绿 + 相关单测（本文件各节验证段）；P5 额外 17 条 hooks 单测 +
  6 条 agent 接线 + 4 条 worker 集成；P6 新增 5 条相关召回测试。
- vitest 桥 `agent-host.python.test.ts` **5 passed**（P5 动 worker 后进程契约未断）。

### 注记 / 范围克制（有意不做，记账于 GAP-ANALYSIS）
- hooks 的 prompt/agent/http 三种 type 与 Notification 事件（无桌面 toast 通道）。
- skills paths 条件激活（已在 十四 的 Wave C 落地）、inline shell。
- 会话标题生成 / 上下文分桶 / 跨会话搜索（host UI 槽位）。
- 记忆自动抽取（extractMemories）与文件级快照回滚列为后续。

## 十四、M11 后续三波 Wave A/B/C（GAP-ANALYSIS 增量账本 R-A…R-H 落地）

承接 GAP-ANALYSIS「落地批次建议」（202–208 行）按三小波落地，每波自验 + commit、不 push。
改动全在 `runtime-py/emberpy/`，renderer/main（TS）零改动。覆盖了 十三『有意不做』里的
**skills paths 条件激活**；inline shell 及 R-块内其余边界（parser 差分、notifier/toast、
子 agent 挂 memory/skills/mcp、worktree 隔离）保持不做，记账于 GAP-ANALYSIS。

| 波 | 内容（增量账本块） | 提交 |
| --- | --- | --- |
| Wave A | R-A #8 CLAUDE.md/AGENTS.md 注入 · R-G① SessionEnd 白名单 bug · R-H .ipynb 读取 | c5dad56 |
| Wave B | R-B #7 deny + 敏感集合 full/auto 仍拦 · R-C #18 symlink 词法+终值双查 · R-D #6 bash 目标解析加固 | aeb21ab |
| Wave C | R-G②③④ hooks 增量 · R-F schemas 每步动态化 + skills paths 条件激活 · R-E 自定义 agent 定义文件 | 544b20a |

### Wave C 细节
- **R-G② StopFailure**：`run()` 模型调用出错结束（且超限恢复未救回）break 前触发
  StopFailure（原只有 Stop 无触发位）。
- **R-G③ PermissionDenied**：fs/shell/web/mcp 不再各自 catch Denied 吞成普通文本；
  改由 `Agent._execute` 统一 `except Denied` → 触发 PermissionDenied hook、返回
  `(str(exc), True)`（denied 标志由恒 False 变真）。`worker._handle_tool_result` 依
  `data.get("denied") is True` 标 isError，UI 能区分"拒绝"与"普通错误"。
- **R-G④ PermissionRequest**：gate 增 `decider`/`request_context`；Agent 装了 hooks 就挂
  `_permission_decision`，gate 每次询问人前调用 → hook 回 allow（免问）/deny（直接拒）/
  ask（照旧）；没配事件 decider 返回 None no-op。
- **R-F schemas 每步动态化**：`run()` 把 `schemas = registry.schemas()` 移进 while 内
  每步求值（一处改动兑现 skills paths 激活 + R-E agents 清单 + store.refresh 热更）。
  `Tool.description` 支持 `str | Callable[[], str]`，schema() 每步求值。
- **R-F skills paths 条件激活**：SkillStore 增 `conditional()`/`activated`/
  `is_active()`/`activate_for_paths(touched)`（gitignore 风格匹配，`**/` 可零层）；
  带 `paths` 的技能初始不可见（conditional），fs 的 read_file/write_file/file_edit
  成功点回调激活后才进 `available()`/listing()/Skill 工具/命令面。
- **R-E 自定义 agent 定义文件**：新 `agents/` 包（loader.py + `__init__`），扫 workspace
  `.claude/agents`/`.ember/agents` + home 同子目录（`EMBERPY_AGENTS_DIR` 整体覆盖、
  `EMBER_HOME` 隔离、同真实文件去重、project>user 冲突让位）。AgentDef 承载
  name/description/body/tools/disallowed_tools/max_turns/permission_mode/initial_prompt。
  `run_agent` 的 agent_type 变自由字符串，工具 description（callable）动态列内置 +
  已发现的自定义名；`_spawn_subagent_inner` 按定义：正文当子 agent system prompt、
  initialPrompt 前置进任务、permissionMode/maxTurns 覆盖、`apply_agent_def_filters`
  按 tools 白名单 / disallowedTools 黑名单过滤子工具池（run_agent 仍结构性不递归）。
  `Agent.run` 每轮开头 refresh agents；worker 持久场景装配 env.agents。

### 验证（全自动）
- Wave A/B/C 各波全量 pytest 绿；Wave C 新增/回归：`test_skill_paths.py` 9 例、
  `tests/unit/agents/test_agents.py` 12 例（发现/解析/store/agentic 端到端）、
  hooks（config 9 + runner 9 + test_hooks_tool 6）、web/tools Denied 语义改
  `pytest.raises(Denied)`、test_subagent 契约随 agent_type 自由化更新。
- vitest 桥 `agent-host.python.test.ts` **5 passed**（Wave C 后进程契约未断）。

### 注记 / 范围克制
- R-F 只做"激活机制"（激活后即进可用清单）；"激活即注入正文 / 深层目录随触碰发现"后置。
- R-E 只做 mode/tools/maxTurns/initialPrompt/正文 system；model/skills/memory scope/
  worktree/effort/hooks 子集覆盖=裁（见 agents/loader.py docstring）。

## 十五、差距账本下一批：#10 记忆自动抽取（extractMemories 单次调用版）

承接 GAP-ANALYSIS 顶部「未动」清单，经探查收敛只做 **#10 记忆自动抽取**（探查剔除：
#11 因 M7 compact 已留 marker 边际小、#16 被 renderer crop(12000) 打折、#12 正确落点在
host 桌面 /undo 绕过引擎）。改动全在 `runtime-py/emberpy/`，renderer/main（TS）零改动、
不新增事件类型给 UI。提交 `75e1596`。

### 方案（推荐 B：worker 侧单次 LLM 调用，非 fork 子 agent）
claude 原样 = 回合末 fork 只读+memory 工具子 agent 自去重后写 memdir。否决 fork 的理由：
记忆写入只是原子 `store.save`，套子 agent 完整循环是浪费；子 agent 现不继承 memory（M8
有意裁），为它新开记忆挂接恰是要避免的复杂语义。

**做法**：worker 在每轮收尾（`message_end` 已出、对话主流程落定后、`response success`
前）同步做一次非流式 `llm.complete(tools=None)`（同款先例：compact 的 `summarize`）。
喂"本轮紧凑工作档案 + 现有记忆索引"→ 模型一次回 JSON 记忆数组 → 引擎侧确定性
parse/去重/校验后逐条 `MemoryStore.save`。

### 代码落点
- 新 `emberpy/memory/extract.py`（纯逻辑 + LLM 编排，离线可测，不 import worker）：
  `AUTO_MEMORY_ENV` / `auto_memory_enabled()`（1/true/yes/on 为真）；`MIN_FINAL_CHARS`(80，
  env `EMBERPY_AUTO_MEMORY_MIN_FINAL_CHARS` 覆盖) 与 `should_extract_turn`（有 patch → 抽；
  否则无工具且答复空/过短 → 跳过琐碎寒暄；其余抽）；`EXTRACT_SYSTEM_PROMPT`（中文"记忆
  策展人"：四 type 释义 + 更新优于新建/复用 name/只存跨会话/宁缺毋滥/严格 JSON 数组）；
  `build_extract_messages`（task ≤500、final ≤1200 截断）；`patch_lines`（动作+相对路径、
  cap 15）；`tool_digest(events)`（assistant.tool_calls 取工具名去重 + 错误前缀结果片段
  cap 3×200）；严格 `parse_memory_payload`（首`[`..末`]` json.loads；name 过 store._NAME_RE、
  type 闭集、content 非空；坏条目进 issues 单条丢不整体失败）；`MergeReport` dataclass
  （created/updated/dropped，saved=前两者和）；`merge_memories`（同名去重只留最后 → 覆盖；
  坏条目/save 异常逐条 drop 不中断）；`run_memory_extraction`（build→complete→parse→merge，
  reply 空/散文回空报告绝不抛）。
- `bridge/worker.py`：
  - `_execute_prompt` 末尾（`_close_window()` 之后、`return result` 之前）插
    `self._auto_extract_memories(result)`——仍在 run 线程内、`response success` 前 →
    保持"单任务"不变量，**无后台并发写索引**；引擎命令提前 return 天然不触发。
  - 新私有 `_auto_extract_memories`（整体 try/except 自吞）：守卫 `self.memory is None` /
    `not auto_memory_enabled()` / `result None` / `self._last_error`（本轮模型失败）→
    return；取最后一个 user 事件到结尾的事件切片做档案；`should_extract_turn` 门槛；
    `run_memory_extraction(self._make_llm(), self.memory, ...)`（索引取 store.index_text()）；
    成功后推进 `self._auto_mem_seen_patch_seq = patches.high_water`（下次只提炼新改动）。
  - `__init__` 近 auto_compact 字段群加 `self._auto_mem_seen_patch_seq = 0`；`_new_session`
    归零（新会话 agent PatchStore 序号从 0 重新计，不归零会漏下一会话全部改动）。
- **env 门控默认关**：`EMBERPY_AUTO_MEMORY` 未设 = 零回归（现有持久场景测试设了
  EMBER_HOME/EMBERPY_MEMORY_DIR 但没设本 env，FakeLLM 脚本不被额外消耗）；纯库
  memory=None 天然免疫。门槛只做"开了之后跳过琐碎轮"的第二道闸。

### 验证（全自动）
- 单测 `tests/unit/memory/test_extract.py` 18 例：parse（合法/带废话抽中/非 JSON 不抛/
  坏条目单条丢）、`should_extract_turn`（琐碎跳过/有 patch 抽/长答复抽）、merge（同名覆盖
  created False + 索引单行/新名新建/同名去重最后赢/坏条目与 save 异常不中断整批）、
  run_extraction（FakeLLM 合法 JSON → 落盘+索引行；空/散文 no-op）、patch_lines/tool_digest。
- 桥接 `tests/integration/bridge/test_worker_auto_memory.py` 5 例（RPC prompt 驱动全链路）：
  env=1 + write_file → 记忆 .md + 索引行（FakeLLM complete_calls==3 = agent 2 + 抽取 1）；
  不开 env → 无落盘且 complete_calls==1；琐碎轮 → 不再发第二次 complete；两轮同名 → 单
  .md/索引单行/正文为第二轮；抽取回散文或那次 complete 抛异常 → 主轮 success、可再发下一轮。
- 全量 pytest 绿（2 skip 为既有）；vitest 桥 `agent-host.python.test.ts` **5 passed**。

### 注记 / 范围克制
- 语义去重（同话题新名算新条）靠 prompt 引导复用 name，不做向量层；旧记忆清理/淘汰不做
  （上限由既有 cap 截断兜底）；模型选择/回退不做；子 agent 挂 memory、renderer 事件、
  CLI/纯库路径、抽取 usage 计入面板、后台线程，全不做。
- 抽取调用在 run 线程内、`response success` 前（非后台）：默认关闭时无额外成本；开启后
  每非琐碎轮多一次模型调用，属有意取舍（换取免手动的长期记忆沉淀）。

## 十六、差距账本收官批次：settings.json 权限规则 · shell 大输出落盘 · 记账定案

> 定位：账本到 #10 实质完成。用户"把剩下的做完"经探查收敛为 **精选小批量 + 收官**
> （多数剩余条目此前判过低 ROI / 落点在 host，落"定案不做"而非硬做）。2026-09。

### A. 项目级 `.claude/settings.json` 工具级 allow/deny（落地 #19 免反复询问 + claude settings 对齐）

- 新 `permission/settings.py`：读 `<workspace>/.claude/settings.json` 的 `permissions.allow/deny`，
  **env 门控 `EMBERPY_PROJECT_SETTINGS=1`**（沿用项目文件默认不信任口径，镜像
  hooks/permissions.json）。坏 JSON/缺文件/坏条目一律静默丢弃。
- `permission/policy.py`：`PermissionPolicy` 增工具级 `_tool_deny`/`_tool_allow` 与
  `denies_tool/allows_tool`；规则文法 `Tool` / `Tool(pattern)`，Tool 可写引擎名或 claude
  CamelCase（Bash/Read/Write/Edit/WebSearch/WebFetch 别名表；写工具 write_file/file_edit 归一组）。
  匹配 = fnmatch 全匹配 或（无通配时）前缀规则（claude 语义）。
- `permission/gate.py`：各 `authorize_write/command/read/external` 顶部加**无条件工具级 deny**
  （压过 full/confirm）；allow 只在各"普通询问"分支前短路——**敏感写（.env/.git…）仍要人工
  批准、deny 与硬拦截不可被 allow 绕过**（安全不变量，测试锁定）。
- 明确不做：permissions.defaultMode、用户级 settings 合并、allow 豁免敏感集合、
  claude `pattern:*` 后缀语法。

### B. shell 大输出全文落盘（#16 窄版）

- `tools/shell.py`：`run_shell` 输出超 `MAX_OUTPUT`(80KB) 时，把**全文**写到
  `<workspace>/.ember/out/shell-<hex8>.log`，返回文本附工作区相对路径指针；模型用
  既有 `read_file offset/limit` 按行读回被截的中间段。写盘失败退回纯截断（不破坏命令结果）。
  只做 shell 一处；MCP/web/read 各自独立 cap 语义保留（renderer crop 下其余 spill 收益打折）。
- `bridge/worker.py`：`_prune_shell_spill()` 在 `_new_session`/`close` 删除 `.ember/out` 里
  超过 24h 的日志（crash 残留兜底），best-effort 失败静默。

### C. 记账定案（其余条目保持裁剪，不做实现）

- ROADMAP 二节 M2 行翻 `✅`（真机冒烟已用 file_edit，账目过时）。
- GAP-ANALYSIS：#22（Skill fork，P4 已落地）、#24（.ipynb 读取，Wave A 已落地）行补 `✅`；
  #16 注窄版已落地、#19 注经 settings allow 落地；#11/#12/#17/#20/#23 与未排期"值得补"项落
  定案不做（会话标题/分桶/跨会话搜索属 host UI 槽位或二期；见 GAP-ANALYSIS 定案块）。

### 验证（全自动）

- 全量 pytest **444 passed, 3 skipped**（新增 `tests/unit/permission/test_settings_rules.py`、
  `tests/unit/tools/test_shell_spill.py`；既有权限/工具测试零回归）。
- vitest 桥 `agent-host.python.test.ts` **5 passed**（worker 装配接口未破）。
