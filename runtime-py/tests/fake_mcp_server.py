"""离线测试用假 MCP server（stdio transport，换行分隔 JSON-RPC 2.0）。

行为：initialize 握手 → 收 notifications/initialized 后主动发一次 roots/list
请求验证客户端应答（回执打到 stderr，供测试断言）。工具：
- echo(text: 必填, repeat?: int=1, tags?: [str]) → 文本块
- get_info() → 只读工具（annotations.readOnlyHint=true），固定文本
- fail() → isError=true 的文本错误
- structured() → content 空、只给 structuredContent（验证结构化回退）

被 spawn 时：``python fake_mcp_server.py``。退出：stdin EOF 即结束。
"""
from __future__ import annotations

import json
import sys

for stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

TOOLS = [
    {
        "name": "echo",
        "description": "把 text 原样返回（可重复 repeat 次、附加 tags 列表）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要回显的文本"},
                "repeat": {"type": "integer", "minimum": 1, "description": "重复次数（默认 1）"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "附带标签"},
            },
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "get_info",
        "description": "返回服务器信息（只读）",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "fail",
        "description": "总是失败（用于测试 isError 路径）",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "structured",
        "description": "返回结构化数据（无 content 文本块）",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]

_CAPABILITIES = {"tools": {"listChanged": False}, "prompts": {}, "resources": {}}
_SERVER_INFO = {"name": "fake-mcp-server", "version": "0.1.0"}


def _call_tool(name: str, arguments: dict) -> dict:
    if name == "echo":
        text = str(arguments.get("text", ""))
        repeat = int(arguments.get("repeat", 1))
        tags = arguments.get("tags") or []
        out = "\n".join([text] * max(1, repeat))
        if tags:
            out += "\n[tags: " + ", ".join(str(t) for t in tags) + "]"
        return {"content": [{"type": "text", "text": out}]}
    if name == "get_info":
        return {"content": [{"type": "text", "text": "fake-mcp-server/0.1.0 ready"}]}
    if name == "fail":
        return {"content": [{"type": "text", "text": "服务器故意失败：你要求的一定会坏"}], "isError": True}
    if name == "structured":
        return {"structuredContent": {"ok": True, "items": [1, 2, 3]}}
    return {"content": [{"type": "text", "text": f"未知工具 {name}"}], "isError": True}


def main() -> int:
    client_info = {}
    initialized = False
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            client_info = msg.get("params", {}).get("clientInfo", {})
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
                    "capabilities": _CAPABILITIES,
                    "serverInfo": _SERVER_INFO,
                    "instructions": "这是一个离线测试用的假 MCP 服务器。",
                },
            }
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        elif method == "notifications/initialized":
            initialized = True
            # 主动发一个 roots/list 请求，验证客户端能应答 server→client request
            sys.stdout.write(
                json.dumps({"jsonrpc": "2.0", "id": 9001, "method": "roots/list"}) + "\n"
            )
            sys.stdout.flush()
        elif method == "tools/list":
            sys.stdout.write(
                json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}, ensure_ascii=False)
                + "\n"
            )
            sys.stdout.flush()
        elif method == "tools/call":
            params = msg.get("params", {})
            result = _call_tool(str(params.get("name", "")), params.get("arguments") or {})
            sys.stdout.write(
                json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}, ensure_ascii=False) + "\n"
            )
            sys.stdout.flush()
        elif "method" in msg and msg_id is not None:
            # server→client 请求（roots/list 等）：应答
            if msg["method"] == "roots/list":
                result = {"roots": [{"uri": "file:///workspace"}]}
            else:
                result = {}
            sys.stdout.write(
                json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}, ensure_ascii=False) + "\n"
            )
            sys.stdout.flush()
        # else: 对我们响应/通知，忽略
    # 收尾：把收到的 client 标识打到 stderr（供测试断言 stderr 被接管不混协议）
    if client_info:
        sys.stderr.write(f"[fake-mcp] connected client={json.dumps(client_info)}\n")
        sys.stderr.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
