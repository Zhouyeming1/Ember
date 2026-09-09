# emberpy 真实编码能力基线（DeepSeek 冒烟，2026-09-08）

> 目的：不只看"离线 FakeLLM 链路绿"，而是用**真模型 + 隔离临时工作区**跑几个有代表性的
> 真实编码任务，给 emberpy 引擎的"实际 coding 能力"一个可复现的观察基线。
> 这是**冒烟基线，不是 benchmark**：任务小而定义清晰、单文件级、无模糊需求，别过度外推。

## 方法

- 引擎：`runtime-py/emberpy` 的 `Agent` 直驱（FULL 权限、无子代理、无 worker/RPC），真实
  `ChatLLM`（`api.deepseek.com`，模型 `deepseek-v4-flash`，key 走本机 `.env` 只读映射）。
- 工作区：系统临时目录逐任务重建，**不碰真实项目**；全部任务文件隔离在 `%TEMP%\emberpy-eval-*`。
- 验收：每任务跑完由驱动脚本**外部复核**（重跑测试/命令看退出码与输出），不只信 agent 自述。
- 可复现：驱动脚本在 `%TEMP%\emberpy-eval\driver_a.py` / `driver_bc.py`（含任务文件文本）。

## 结果

| 任务 | 内容 | 引擎动作轨迹 | 结果 | 步骤 | 耗时 |
|---|---|---|---|---|---|
| A 修 bug | 二分 `first_occurrence` 有逻辑错，测试断言失败 | list_dir→glob→read×2→file_edit→run_command | ✅ 自写成标准 lower_bound，9 断言全过，**没动测试文件** | 6 | 9.5s |
| B 加功能+自补测试 | lib 加 `divide`（除零抛 ValueError）+ 新建测试文件 | read×2→file_edit→write_file(新测试)→run_command | ✅ 实现正确；**自写 test_divide.py（506B）** 覆盖正常/除零消息/float/负数小数；旧测试仍过 | 7 | 7.7s |
| C 跨文件去重 | `report.py` 内联清理逻辑与 `util.clean` 重复，抽成复用、行为不变 | read×2→file_edit→run_command | ✅ 改为 `cleaned = clean(text)`，输出 `clean_len=15 words=3` 不变 | 6 | 5.0s |

产物质量抽查（B）：`divide` 实现 4 行干净；test_divide.py 用 `try/except` 验证异常消息、
`isinstance(..., float)` 查类型，条理与手写接近。C：只动了该动的行、保留注释风格。

## 观察结论

**强项（本次冒烟证实）**
- 工具调用链路真实可用：三任务均是 `读→定位→改→跑验证命令确认`，每步 tool_call→tool_result
  配对成功，无 schema 报错 / 权限卡死 / 上下文超限，无需人为干预跑完。
- 先读后改守约：改动前都先 read_file；write/edit 一次成功，没出现"没读就写被拒→重试"的浪费。
- 步骤经济：6–7 步收尾、每任务 <10s，无反复试错；自验后才给最终答复。
- 能遵守"不改测试/不改 util.py、只动目标文件"这类约束（A、C 均未越界）。

**局限（必须说清）**
- 任务小而边界清晰：没测长会话/多文件大重构/模糊需求/需联网查证的 bug；没跑桌面 worker+
  RPC+UI 全链路（该链路由 host vitest `agent-host` 5/5 另行兜底，但那是"契约通"，不是"活干得好"）。
- FULL 权限直驱 ≠ auto/ask 的真实交互体验（确认框、deny 拦截那套没走到）。
- 一次抽样、非统计：结论只说明"这几个任务能办"，不说明失败率/鲁棒性。
- 本冒烟未统计精确 token 用量/成本（DeepSeek 极便宜，每任务量级在千 token 内）。

## 建议的下一步（如要更硬的能力结论）

1. 加大难度再抽 2–3 个：带隐含依赖的跨 3+ 文件改动、需先读 README/文档再实现的"模糊需求"、
   修一个需运行时诊断（复现报错→定位→修）的 bug。
2. 走一次**桌面 worker 真链路**（RPC prompt + auto 权限），把 permission/deny 交互也纳入。
3. 足够样本后再谈"通过率/失败率"这类数字。
