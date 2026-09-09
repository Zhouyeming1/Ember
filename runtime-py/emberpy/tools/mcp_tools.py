"""MCP 工具：把已发现的 MCP server 工具注册成 registry Tool。

生成规则：
- 工具名 = ``mcp__<server>__<tool>``（manager 里归一化并全名化）
- category = EXTERNAL：调用前过 ``gate.authorize_external`` 专用授权路径
  （plan 拒 / ask 问 / auto+full 放行 / 无 confirm fail-closed）
- fn 用 ``**kwargs`` 接收模型参数并原样透传给 server（参数名可能是任意键，
  不能写成具名形参）；大输出截断避免烧上下文；拒绝/远端错误转成文本
  （worker 按文本是否以"错误"开头标记 isError，无需专门返回位）
"""
from __future__ import annotations

from typing import Any

from ..mcp import McpManager
from .registry import Tool, ToolCategory, ToolEnv

_MAX_RESULT_CHARS = 60_000  # 大结果截断，与 read_file 的 _capped 同一量级
_DESC_FALLBACK = "外部 MCP 工具，行为由对应服务器定义。"


def _capped(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit // 2):]
    return f"{head}\n……[中间省略 {len(text) - limit} 字符]……\n{tail}"


def build_mcp_tools(env: ToolEnv) -> list[Tool]:
    """env.mcp 非 None 时连接 server 并生成其全部工具（失败 server 跳过）。

    注意副作用：首次调用会真正 spawn 子进程并握手（ensure_ready 幂等，
    仅首次；工具 schema 随后从 manager 缓存读取，后续 registry 重建零成本）。
    """
    manager: McpManager = env.mcp
    manager.ensure_ready()
    tools: list[Tool] = []
    for entry in manager.tool_entries():
        tools.append(
            Tool(
                name=entry.full_name,
                description=(entry.description or _DESC_FALLBACK) + f"\n来自 MCP 服务器：{entry.server_name}",
                parameters=entry.schema,
                category=ToolCategory.EXTERNAL,
                fn=_make_call(manager, entry.full_name, env),
            )
        )
    return tools


def _make_call(manager: McpManager, full_name: str, env: ToolEnv):
    def call(**kwargs: Any) -> str:
        gate = env.gate
        if gate is not None:
            # Denied 上抛给 agent._execute（R-G③）：EXTERNAL 授权被拒 -> PermissionDenied hook
            gate.authorize_external(full_name, env.confirm)
        text, _is_error = manager.invoke(full_name, dict(kwargs))
        return _capped(text)

    return call
