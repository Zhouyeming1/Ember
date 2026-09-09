"""hooks（钩子）：在 agent 生命周期/工具调用/对话事件上运行外部命令。

对齐 claude hooks 的行为设计（只取本引擎要的子集，代码自写）：
- 配置：``<home>/.ember/hooks.json``（用户级恒读）+ ``<workspace>/.ember/hooks.json``
  （项目级仅 ``EMBERPY_PROJECT_HOOKS=1`` 时并入——不默认信任）。
- 事件：SessionStart / UserPromptSubmit / PreToolUse / PostToolUse /
  PostToolUseFailure / SubagentStart / SubagentStop / PreCompact / PostCompact / Stop。
- 命令 hook：subprocess 执行、stdin 喂事件 JSON payload、注入 HOOK_EVENT/CWD/
  PERMISSION_MODE/TRANSCRIPT_PATH 等环境变量、超时（默认 60s）、stdout 限长保留。
  决策：退出码 2 或 stdout JSON（``decision: block`` / ``continue: false``）= 阻塞；
  UserPromptSubmit 的 stdout JSON ``modifiedPrompt`` 可改写将要跑的 prompt。
- 安全：命令经 shlex 切词、不经 shell 解释；项目级默认关；无配置 = 全 no-op。
"""
from __future__ import annotations

from .config import HookManager, load_hook_manager
from .runner import HookCommandResult, HookRunOutcome

__all__ = ["HookManager", "HookRunOutcome", "HookCommandResult", "load_hook_manager"]
