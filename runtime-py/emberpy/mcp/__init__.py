"""MCP（Model Context Protocol）最小客户端。

把本地 stdio MCP 服务器接进来，把它们声明的工具暴露给 agent 调用——
模型看到的名字统一为 ``mcp__<server>__<tool>``，调用时由工具函数路由回
对应服务器的子进程。参考 claude-code `services/mcp/` 的行为设计（连接/工具
发现/命名/路由分层、失败降级为"该 server 不挂工具"），代码自写。

范围克制：只支持 stdio transport（本地子进程），不做远程 HTTP/SSE、OAuth、
企业 allowlist、.mcp.json 审批与连接管理 UI——那是 claude 桌面面。启用完全
靠显式配置（EMBERPY_MCP_CONFIG 指向 JSON），无配置则本包完全隐身。
"""
from .client import McpError, StdioMcpSession
from .config import McpServerConfig, manager_from_env, read_config
from .manager import McpManager

__all__ = [
    "McpError",
    "StdioMcpSession",
    "McpServerConfig",
    "read_config",
    "manager_from_env",
    "McpManager",
]
