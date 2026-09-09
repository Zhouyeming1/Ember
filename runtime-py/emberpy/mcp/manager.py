"""McpManager：多 server 连接编排 + 工具全名命名 + 调用路由。

- 命名：工具全名 = ``mcp__<server归一化>__<tool归一化>``，``__`` 分隔；
  归一化把非法字符（点/空格等）换成 ``_``。模型永远看到带前缀的全名，
  调用时按前缀解析回 (server, 原始 tool 名) 路由到对应会话。
- 失败语义（对齐 claude）：单个 server spawn/握手/拉工具失败 → 只跳过它，
  其它照常，绝不因一条坏 server 让整个 agent 起不来；配置空 = 整体无 MCP。
- 调用断线：stdio 本地进程崩溃不做后台重连，但"调用时若已断线先重连一次
  再发"（服务端中途退出场景自愈），重连 = 全新 spawn+握手+重拉工具。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from .client import McpConnectionError, McpError, StdioMcpSession
from .config import McpServerConfig

_DESCRIPTION_MAX = 2048          # 描述超长截断，防 server 塞几十 KB 文档烧上下文
_PREFIX = "mcp"
_SEP = "__"

_NORM = re.compile(r"[^a-zA-Z0-9_-]")


def normalize_name(name: str) -> str:
    """把 server / tool 名归一化成可用作前缀段的标识符。"""
    return _NORM.sub("_", name)


def _full_name(server_name: str, tool_name: str) -> str:
    return f"{_PREFIX}{_SEP}{normalize_name(server_name)}{_SEP}{normalize_name(tool_name)}"


def split_full_name(full_name: str) -> Optional[tuple[str, str]]:
    """把 ``mcp__server__tool`` 还原成 (归一化 server 名, 原始 tool 名)。

    tool 名可能本身含 ``__``，故只在第二段处切一次再合并余下部分。
    非 mcp 前缀返回 None。
    """
    if not full_name.startswith(f"{_PREFIX}{_SEP}"):
        return None
    _, server, tool = full_name.split(_SEP, 2)
    return server, tool


def _clean_description(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    folded = " ".join(raw.split())  # 折叠换行/连续空白，避免破坏工具排版
    return folded[:_DESCRIPTION_MAX]


def _safe_schema(raw: Any) -> dict[str, Any]:
    """inputSchema 兜底成 OpenAI 工具 parameters 能用的形状。

    原 schema 原样透传（保留嵌套/anyOf 等），只保证顶层是 object 且
    properties 是 dict（模型工具面缺 properties 会出错）。
    """
    if not isinstance(raw, dict):
        return {"type": "object", "properties": {}}
    props = raw.get("properties")
    if not isinstance(props, dict):
        raw = dict(raw)
        raw["properties"] = {}
    if raw.get("type") != "object":  # 缺 type / 非 object 顶层：一律归一成 object
        raw = dict(raw)
        raw["type"] = "object"
    return raw


class McpToolEntry:
    """一个已发现的 MCP 工具（注册成 registry Tool 的原料）。"""

    __slots__ = ("full_name", "server_name", "tool_name", "description", "schema", "session")

    def __init__(
        self,
        full_name: str,
        server_name: str,
        tool_name: str,
        description: str,
        schema: dict[str, Any],
        session: StdioMcpSession,
    ) -> None:
        self.full_name = full_name
        self.server_name = server_name
        self.tool_name = tool_name
        self.description = description
        self.schema = schema
        self.session = session


class McpManager:
    """连接并暴露一组 stdio MCP server 的工具。默认惰性：不 spawn 任何进程。"""

    def __init__(self, servers: Iterable[McpServerConfig], cwd: str) -> None:
        self.cwd = cwd
        self._sessions: dict[str, StdioMcpSession] = {}
        self._by_norm: dict[str, StdioMcpSession] = {}
        for config in servers:
            session = StdioMcpSession(config, cwd)
            self._sessions[config.name] = session
            self._by_norm[normalize_name(config.name)] = session
        self._ready = False
        self._entries: Optional[list[McpToolEntry]] = None
        self._errors: dict[str, str] = {}  # server 名 → 连接/发现失败原因

    # -- 连接与发现 ------------------------------------------------------
    def ensure_ready(self) -> None:
        """连接所有 server 并拉取工具列表（幂等）。坏 server 跳过并记录原因。"""
        if self._ready:
            return
        self._ready = True
        entries: list[McpToolEntry] = []
        for name, session in self._sessions.items():
            try:
                if not session.is_alive():
                    session.connect()
                tools = session.list_tools()
            except (McpConnectionError, McpError, OSError) as exc:
                self._errors[name] = str(exc)
                session.close()
                continue
            for raw in tools:
                tool_name = str(raw.get("name", ""))
                if not tool_name:
                    continue
                entries.append(
                    McpToolEntry(
                        full_name=_full_name(name, tool_name),
                        server_name=name,
                        tool_name=tool_name,
                        description=_clean_description(raw.get("description")),
                        schema=_safe_schema(raw.get("inputSchema")),
                        session=session,
                    )
                )
        self._entries = entries

    def refresh_entries(self) -> None:
        """重连后重新发现（拉最新工具列表）。供断线重连路径使用。"""
        self._ready = False
        self._entries = None
        self._errors = {}
        self.ensure_ready()

    def tool_entries(self) -> list[McpToolEntry]:
        return list(self._entries or [])

    def failures(self) -> dict[str, str]:
        """连不上的 server：名 → 原因（供宿主打日志，不打断启动）。"""
        return dict(self._errors)

    # -- 调用 ------------------------------------------------------------
    def invoke(self, full_name: str, arguments: Optional[dict[str, Any]]) -> tuple[str, bool]:
        """路由一次 MCP 工具调用 → (给模型的文本, is_error)。

        返回文本已归一化（content blocks → 文本 / structuredContent → JSON /
        isError → 错误前缀）。断线时自动重连一次再发。
        """
        parsed = split_full_name(full_name)
        if parsed is None:
            return f"错误：{full_name} 不是 MCP 工具", True
        server_norm, tool_name = parsed
        session = self._by_norm.get(server_norm)
        if session is None:
            return f"错误：找不到 MCP server {server_norm!r}", True
        try:
            result = session.call_tool(tool_name, arguments or {})
            return _flatten_result(tool_name, result)
        except McpConnectionError:
            # 服务端中途退出：重连一次再发（重连失败把原因返回给模型）
            try:
                session.reconnect()
                session.list_tools()
                result = session.call_tool(tool_name, arguments or {})
                return _flatten_result(tool_name, result)
            except (McpConnectionError, McpError) as exc:
                return f"错误：MCP server 已退出且重连失败：{exc}", True
        except McpError as exc:
            return f"错误：{exc}", True
        except OSError as exc:
            return f"错误：{exc}", True

    def close(self) -> None:
        """断开全部 server 子进程。幂等，可重复调用。"""
        for session in self._sessions.values():
            session.close()

    def server_count(self) -> int:
        return len(self._sessions)


def _flatten_result(tool_name: str, result: dict[str, Any]) -> tuple[str, bool]:
    """MCP tools/call 结果 → (文本, is_error)。参考 claude processMCPResult。"""
    is_error = bool(result.get("isError"))
    parts: list[str] = []

    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            block_type = block.get("type")
            if block_type == "text":
                parts.append(str(block.get("text", "")))
            else:
                # image/audio/resource 等非文本块：不内联二进制，给可读占位
                label = block.get("text") or block.get("uri") or block_type or "?"
                parts.append(f"[{block_type} 资源：{label}]")

    if "structuredContent" in result:
        import json as _json

        structured = result["structuredContent"]
        if isinstance(content, list) and parts:
            parts.append("--- 结构化数据 ---")
        parts.append(_json.dumps(structured, ensure_ascii=False, default=str))

    text = "\n".join(p for p in parts if p).strip()
    if not text:
        text = f"(MCP 工具 {tool_name} 返回空结果)"
    if is_error:
        return f"错误（MCP 工具 {tool_name}）：{text}", True
    return text, False
