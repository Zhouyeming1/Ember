"""子 agent（subagent）派生工具：run_agent。

参考 claude-code-analysis `tools/AgentTool/` 的**同步路径**设计（Agent 工具，别名
Task），只取接口意图、代码自写。本引擎复刻它的最小契约：
- 主 agent 调 `run_agent(description, prompt, agent_type)`，在**当前这一轮工具调用里
  同步跑完**一个独立子 agent，返回它**最后一条 assistant 文本**当工具结果。
- 子 agent 有**独立会话**（只有一条由 prompt 生成的 user 消息，看不到父会话历史，
  主 agent 须把背景全写进 prompt）——照 claude "像给新同事交代背景"。
- 子 agent 的工具池**没有 run_agent**：默认不支持再派生子 agent（照 claude
  ALL_AGENT_DISALLOWED_TOOLS 把 Agent 从子 agent 池拿掉，外部用户最多一层）。
- agent_type：general=完整工具（可写文件/跑命令）；explore=只读调研工具
  （对应 claude 内建 explore agent 的只读语义，推荐用于搜索/读代码）。

有意裁剪（与 claude 的差异，见 roadmap M8 节）：不做 `run_in_background` 后台 +
task-notification 通知路径（Ember 是"用户发消息才跑一轮"的聊天式引擎，无自延续
循环，后台完成无处续跑）；不做 AgentTool 的 subagent_type 定义文件/模型覆盖等
扩展；子 agent 的侧链转写不落盘（临时内存会话，只有工具结果回父）。

本模块不 import agent 内核（避免循环）：它只产出"调用一个 spawner 回调"的 Tool，
spawner 由 Agent 注入（真正造子 agent 的逻辑在 agent/core.py 的 _spawn_subagent）。
"""
from __future__ import annotations

from typing import Callable, Optional

from .registry import Tool, ToolCategory, ToolRegistry

# 模型可见的工具名（对应 claude 的 Agent/Task spawner）
SUBAGENT_TOOL_NAME = "run_agent"

# 内置子 agent 类型
AGENT_TYPE_GENERAL = "general"
AGENT_TYPE_EXPLORE = "explore"
AGENT_TYPES = (AGENT_TYPE_GENERAL, AGENT_TYPE_EXPLORE)

# 派生子 agent 的回调：spawner(description, prompt, agent_type) -> 文本（直接当工具结果）
Spawner = Callable[[str, str, str], str]


def _agent_types_text(store: Optional[object]) -> str:
    """description 用的 agent_type 取值说明：内置两个 + 发现的 custom agent 名。

    store 鸭子类型（有 names()/listing()）；None 或空 -> 只列内置。
    """
    lines = ["agent_type：", f"- {AGENT_TYPE_GENERAL}=完整工具（可读写文件、可跑命令）", f"- {AGENT_TYPE_EXPLORE}=只读调研（只留读/搜工具）"]
    if store is not None:
        names = []
        try:
            names = list(store.names())
        except Exception:
            names = []
        for name in names:
            lines.append(f"- {name}=项目里定义的自定义 agent（角色见其描述）")
    return "\n".join(lines)


def build_agent_tool(spawner: Spawner, store: Optional[object] = None) -> Tool:
    """构造 run_agent 工具。spawner 由 Agent 注入；store 是 env.agents（自定义 agent 定义）。

    description 用 callable（R-E/R-F）：每次 schema() 求值时重算 agent 清单，
    磁盘上新增/删掉 agent 定义后，下一步模型就在描述里看到最新可选 agent_type。
    """
    def _describe() -> str:
        lines = [
            "把一段独立子任务委托给子 agent 完成。子 agent 看不到本对话历史，请把需要的背景"
            "全部写进 prompt；执行期间你原地等它，工具结果就是它的最终回答，请在给用户的最终"
            "答复里概括该结果。agent_type 可选内置或项目自定义角色：\n",
            _agent_types_text(store),
        ]
        return "\n".join(lines)

    return Tool(
        name=SUBAGENT_TOOL_NAME,
        description=_describe,
        parameters={
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "一两句话说清派什么活（用于界面/日志展示）。",
                },
                "prompt": {
                    "type": "string",
                    "description": "子 agent 的完整任务指令，含它需要的全部背景（它会像新同事一样从这条消息开始干活）。",
                },
                # 故意不做 enum：可选值随自定义 agent 定义动态变化，用自由字符串 +
                # 工具描述里的取值清单引导模型（enum 固定下来反而挡掉自定义名）。
                "agent_type": {
                    "type": "string",
                    "description": "子 agent 角色。内置 general/explore；或项目定义的自定义 agent 名（见工具描述清单）。",
                    "default": AGENT_TYPE_GENERAL,
                },
            },
            "required": ["description", "prompt"],
        },
        category=ToolCategory.READ,
        fn=lambda description, prompt, agent_type=AGENT_TYPE_GENERAL: spawner(
            description or "", prompt or "", agent_type or AGENT_TYPE_GENERAL
        ),
    )


def build_subagent_registry(base: ToolRegistry, agent_type: str) -> ToolRegistry:
    """给子 agent 重建一个工具注册表（默认不递归派生）。

    - 无论如何都剔除 run_agent（照 claude：子 agent 不能再派生子 agent）；
    - explore 只保留 READ 类工具（读文件/列目录/搜索等），杜绝写文件/跑命令。
    """
    tools = [tool for tool in base.all() if tool.name != SUBAGENT_TOOL_NAME]
    if agent_type == AGENT_TYPE_EXPLORE:
        tools = [tool for tool in tools if tool.category is ToolCategory.READ]
    return ToolRegistry(tools)


def apply_agent_def_filters(registry: ToolRegistry, agent_def) -> ToolRegistry:
    """按自定义 agent 定义的 tools/disallowedTools 过滤注册表（R-E）。

    tools 白名单只留列出的引擎工具名；disallowedTools 黑名单拿掉对应工具。两者都空
    = 不限（只保留默认去掉了 run_agent 的子 agent 池）。工具名用引擎注册表名
    （read_file/write_file/run_command/web_search/Skill…，模型 schema 里见到的）。
    """
    tools = list(registry.all())
    if agent_def.disallowed_tools:
        tools = [t for t in tools if t.name not in agent_def.disallowed_tools]
    if agent_def.tools:
        tools = [t for t in tools if t.name in agent_def.tools]
    return ToolRegistry(tools)
