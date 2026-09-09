"""自定义 agent 定义文件（R-E）：项目 .claude/agents / .ember/agents 下的 *.md。

每个定义 = frontmatter（name/description/tools/disallowedTools/maxTurns/
permissionMode/initialPrompt）+ 正文（子 agent 的 system prompt）。这些 agent 名
作为 run_agent 的 agent_type 可选值，让模型能按角色委托任务（不止内置 general/explore）。
"""
from .loader import (
    AGENTS_PROJECT_SUBDIRS,
    AgentDef,
    AgentStore,
    build_agent_store,
    load_agent_roots,
    resolve_agent_roots,
    scan_root,
)

__all__ = [
    "AGENTS_PROJECT_SUBDIRS",
    "AgentDef",
    "AgentStore",
    "build_agent_store",
    "load_agent_roots",
    "resolve_agent_roots",
    "scan_root",
]
