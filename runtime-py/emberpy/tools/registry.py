"""工具注册表。

每个工具 = 元信息（名称/描述/参数 JSON Schema/类别）+ 可调用函数 fn。
元信息用来生成发给模型的 OpenAI tools 数组；类别决定走权限门里的哪条检查。

安全设计：权限检查在工具内部执行（读取/写入/命令各自过 gate），
循环层不重复判断——这样即使漏挂某一个工具，也不会绕过权限。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from ..patches import PatchStore
from ..permission import Confirm, PermissionGate

# 单选交互钩子：问用户一个问题并返回其选择（None=取消）。由宿主（桌面桥/CLI）注入。
AskValue = Callable[[str, list[str]], Optional[str]]

# 派生子 agent 的回调（与 agent_tool.Spawner 同一形状）：spawner(desc, prompt, agent_type) -> 文本。
# allow_subagents 的主 agent 注入给 env；fork 技能 / run_agent 靠它把工作流隔离到子上下文执行。
SubagentSpawner = Callable[[str, str, str], str]


class ToolCategory(str, Enum):
    READ = "read"      # 只读：读文件/列目录/搜索
    WRITE = "write"    # 写文件（过权限门 + 记 checkpoint）
    SHELL = "shell"    # 执行命令（过权限门 + 危险命令拦截）
    EXTERNAL = "external"  # 外部进程工具（MCP）：副作用不可知，单独一条授权路径


@dataclass
class ToolEnv:
    """工具共享的执行环境（每个 agent run 构造一份，闭包进工具函数）。"""

    workspace: Path
    gate: PermissionGate
    patches: PatchStore
    confirm: Optional[Confirm] = None       # CLI 交互时会指向 input() 包装
    ask_value: Optional[AskValue] = None    # 非 None 时挂 AskUserQuestion 工具（单选澄清）
    memory: Optional["MemoryStore"] = None  # 非 None 时挂 memory_* 工具（跨会话记忆）
    skills: Optional["SkillStore"] = None   # 非 None 且有技能时挂 Skill 工具（模型驱动技能）
    mcp: Optional["McpManager"] = None      # 非 None 时把 MCP server 工具挂进注册表（外部工具）
    # 会话内"已读过哪些文件"的状态表：绝对路径 -> (st_mtime_ns, st_size)。
    # read_file/write 成功后登记；write_file/file_edit 对已存在文件先查它（严格写前必读：
    # 没读过拒、读后被外部改过拒）。跨工具实例共享，一个 agent run 内生效。
    read_state: dict[str, tuple[int, int]] = field(default_factory=dict)
    # 子 agent 派生回调：allow_subagents 的主 agent 在 __init__ 里先注入再建 default_registry，
    # fork 技能 / run_agent 经它隔离执行。缺省 None（子 agent 环境）=> fork 技能回退 inline。
    spawner: Optional[SubagentSpawner] = None
    # 自定义 agent 定义注册表（R-E，.claude/agents 等目录的 *.md）。非 None 时
    # run_agent 的 agent_type 除 general/explore 外还能填这些 agent 名。
    agents: Optional["AgentStore"] = None


@dataclass(frozen=True)
class Tool:
    name: str
    # 静态字符串或返回字符串的零参可调用（R-F/R-E：Skill/run_agent 的 description 随
    # 激活技能/agents 清单热更，每次 schema() 求值一次，registry 不必重建）。
    description: str | Callable[[], str]
    parameters: dict[str, Any]  # JSON Schema 子集（不含 $schema）
    category: ToolCategory
    fn: Callable[..., str]      # **kwargs -> 回传给模型的文本

    def schema(self) -> dict[str, Any]:
        """转成 OpenAI tools 数组里的一项。"""
        desc = self.description() if callable(self.description) else self.description
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters.get("properties", {}),
                    "required": self.parameters.get("required", []),
                },
            },
        }


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def all(self) -> list[Tool]:
        """按注册顺序返回全部工具（供过滤/重建注册表）。"""
        return list(self._tools.values())

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools
