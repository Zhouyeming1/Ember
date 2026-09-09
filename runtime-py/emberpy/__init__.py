"""emberpy —— Ember 的 Python agent 引擎。

本包是从零实现的 agent 运行时，与桌面端本地 TS 数据层（ember-core）配套，
目标是：前端 UI 保持不变，用 Python 引擎作为服务端完成 agent 任务。

域分包（每个目录一个职责，参照 claude-code-analysis/src 的分层思路）：
- agent        编排内核：Agent 循环 / RuntimeInput / 系统提示词
- llm          模型层：领域类型(base) + OpenAI 兼容客户端(chat)
- tools        工具层：注册表 / 文件系统 / shell
- permission   权限层：模式换算(modes) / 命令规则(rules) / 路径作用域(scope) / 权限门(gate)
- patches      文件整写补丁与 checkpoint（/undo）
- session      JSONL 会话落盘与断点续跑（v2 磁盘持久化基础）
- bridge       对桌面 UI 的桥：JSON-RPC worker / 界面转录 / 权限确认往返
- cli          无头终端入口（run / chat）
- errors       统一异常目录（跨域共享错误都收在这）

进程入口：
- ``python -m emberpy run/chat``  -> 终端 CLI（entrypoints）
- ``python -m emberpy.rpc``        -> 桌面 JSON-RPC worker（Electron 主进程 spawn 它）
"""
from .agent import Agent, EventCallback, RunResult, RuntimeInput, run_once
from .llm import ChatLLM, LLM, Message
from .patches import PatchStore
from .permission import PermissionMode
from .session import Session

__version__ = "0.1.0"

__all__ = [
    "Agent",
    "RunResult",
    "RuntimeInput",
    "EventCallback",
    "run_once",
    "ChatLLM",
    "LLM",
    "Message",
    "PatchStore",
    "PermissionMode",
    "Session",
    "__version__",
]
