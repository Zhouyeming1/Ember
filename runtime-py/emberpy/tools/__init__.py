"""工具包：注册表 + 文件系统工具 + 命令工具 + 记忆/技能/计划/澄清工具。"""

from .ask_tool import build_ask_user_question_tool
from .fs import build_fs_tools
from .memory_tools import build_memory_tools
from .mcp_tools import build_mcp_tools
from .plan_tool import build_update_plan_tool
from .registry import AskValue, Tool, ToolCategory, ToolEnv, ToolRegistry
from .shell import build_shell_tool, run_shell
from .skill_tool import build_skill_tool
from .vision import build_vision_tool
from .web import build_web_tools

__all__ = [
    "AskValue",
    "Tool",
    "ToolCategory",
    "ToolEnv",
    "ToolRegistry",
    "build_ask_user_question_tool",
    "build_fs_tools",
    "build_mcp_tools",
    "build_memory_tools",
    "build_shell_tool",
    "build_skill_tool",
    "build_update_plan_tool",
    "build_vision_tool",
    "build_web_tools",
    "run_shell",
]


def default_registry(env: ToolEnv) -> ToolRegistry:
    """按顺序组装默认工具集：先读后写，计划/命令放最后。

    顺序会影响模型先看到哪些工具，因此把最常用的读工具放前面。按能力挂载：
    env.ask_value 非 None（交互宿主）才挂 AskUserQuestion（单选澄清）；
    env.memory 挂上 MemoryStore 才追加 memory_*（跨会话记忆只显式启用时出现）；
    env.skills 挂上有技能的 SkillStore 才追加 Skill 工具。update_plan 是纯上下文
    记账（不写文件、无权限副作用），任何宿主都常驻，供计划模式与执行进度使用。
    """
    tools: list[Tool] = []
    tools.extend(build_fs_tools(env))
    tools.append(build_update_plan_tool())
    if env.ask_value is not None:
        tools.append(build_ask_user_question_tool(env.ask_value))
    if env.memory is not None:
        tools.extend(build_memory_tools(env.memory))
    if env.skills is not None and env.skills.all():
        tools.append(build_skill_tool(env))
    tools.append(build_shell_tool(env))
    tools.append(build_vision_tool(env))  # 图片识别：只读上传目录/工作区图，需视觉 key
    tools.extend(build_web_tools(env))  # 联网搜索/抓页：需设置里配的搜索 key
    if env.mcp is not None:
        # MCP 工具放最后：最"外部"、低优先，先给模型看到本地工具
        tools.extend(build_mcp_tools(env))
    return ToolRegistry(tools)
