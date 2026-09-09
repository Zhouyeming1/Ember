"""MCP stdio 客户端核心：一个 server = 一个子进程 + 换行分隔 JSON-RPC 会话。

协议要点（MCP stdio transport）：
- 帧 = 单行 JSON-RPC 2.0（``json.dumps + \\n``），不是 LSP 的 Content-Length。
- 握手：客户端发 ``initialize`` request → 服务器回 result → 客户端发
  ``notifications/initialized`` 通知（无 id）→ 就绪。
- 服务器可能反过来发 request（std 下主要是 ``roots/list``，返回当前工作区
  的 file:// uri）；notification 忽略。服务器无法被"选中"的能力（tools 缺）
  不算错，按没有该能力对待。
- 子进程 stderr 单独接管（只用于日志/排障），绝不混入 stdout 协议通道。
- 断线/子进程退出：本次调用抛 McpConnectionError；上层可重连（重新 spawn +
  重新握手 + 重新拉工具，绝不复用旧会话缓存）。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .config import McpServerConfig

# MCP 当前仍普遍接受的稳定协议版本（向后兼容老 server）
_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "emberpy", "version": "0.1.0"}
_STDERR_CAP = 64 * 1024  # 只留最近 64KB stderr 用于报错，防撑爆


class McpError(Exception):
    """协议级错误（JSON-RPC error 或服务器 isError 结果）。"""


class McpConnectionError(McpError):
    """连接级失败：spawn 失败 / 进程退出 / 管道断 / 握手失败。"""


class _JsonLines:
    """进程一对管道的换行 JSON 读写（UTF-8）。"""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc

    def write(self, obj: dict[str, Any]) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(obj).encode("utf-8") + b"\n")
        self._proc.stdin.flush()

    def read(self) -> Optional[dict[str, Any]]:
        """读一行并解析；EOF 返回 None；解析失败抛 McpError（协议通道被污染）。"""
        assert self._proc.stdout is not None
        line = self._proc.stdout.readline()
        if not line:
            return None  # EOF：子进程退出或管道关闭
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            return self.read()  # 跳过空行
        try:
            obj = json.loads(text)
        except ValueError as exc:
            raise McpError(f"MCP server stdout 出现非 JSON 行，协议被污染：{exc}") from exc
        if not isinstance(obj, dict):
            raise McpError(f"MCP server 回的非对象 JSON：{obj!r}")
        return obj


class StdioMcpSession:
    """一条 stdio MCP server 连接：握手 + 同步 request + tools 发现/调用。

    同步实现（无后台 loop）：worker 的 agent 运行线程逐轮调用工具，同一时刻
    只有一个线程读写本连接，无需锁。request() 阻塞直到拿到匹配 id 的 response，
    期间就地应答服务器发来的 request（roots/list）。
    """

    def __init__(
        self,
        config: McpServerConfig,
        cwd: str,
        *,
        connect_timeout: float = 30.0,
        request_timeout: Optional[float] = None,
    ) -> None:
        self.config = config
        self.cwd = str(Path(cwd).resolve())
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout  # None = 无限（stdio 工具常跑很久）
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._io: Optional[_JsonLines] = None
        self._next_id = 0
        self._capabilities: dict[str, Any] = {}
        self._server_info: dict[str, Any] = {}
        self._instructions: Optional[str] = None
        self._tools: Optional[list[dict[str, Any]]] = None  # tools/list 缓存
        self._stderr: list[str] = []
        self._stderr_thread: Optional[threading.Thread] = None
        self._cwd_uri = Path(self.cwd).as_uri()

    # -- 生命周期 --------------------------------------------------------
    def connect(self) -> None:
        """spawn 子进程并完成 MCP 握手。已连则 no-op；连失败抛 McpConnectionError。"""
        if self._proc is not None and self._proc.poll() is None:
            return
        env = os.environ.copy()
        env.update(self.config.env)
        try:
            proc = subprocess.Popen(
                [self.config.command, *self.config.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                # 子进程是协议方不是 shell；Windows 无 Close 继承、CREATE_NO_WINDOW 防弹窗
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise McpConnectionError(f"spawn MCP server {self.config.name!r} 失败：{exc}") from exc
        self._proc = proc
        self._io = _JsonLines(proc)
        self._stderr = []
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        try:
            self._handshake()
        except Exception:
            self.close()
            raise

    def _handshake(self) -> None:
        """initialize → 存 capability → notifications/initialized。"""
        result = self.request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"roots": {"listChanged": False}},
                "clientInfo": _CLIENT_INFO,
            },
            timeout=self._connect_timeout,
        )
        self._capabilities = dict(result.get("capabilities") or {})
        self._server_info = dict(result.get("serverInfo") or {})
        raw_instructions = result.get("instructions")
        if isinstance(raw_instructions, str):
            self._instructions = raw_instructions[:2048]
        self.notify("notifications/initialized")

    def close(self) -> None:
        """断开：终止子进程（terminate → 超时 kill 升级），释放管道。幂等。"""
        proc, self._proc = self._proc, None
        self._io = None
        self._tools = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=0.8)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        thread, self._stderr_thread = self._stderr_thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.3)

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # -- JSON-RPC --------------------------------------------------------
    def request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """发一个 request 并阻塞等它的 response。协议错误抛 McpError。"""
        io = self._require_io()
        timeout = self._request_timeout if timeout is None else timeout
        deadline = None if timeout is None else time.monotonic() + timeout
        req_id = self._alloc_id()
        io.write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        while True:
            if deadline is not None and time.monotonic() > deadline:
                raise McpError(f"MCP request {method} 超时（>{timeout}s）")
            msg = io.read()
            if msg is None:
                raise McpConnectionError(f"MCP server {self.config.name!r} 已退出")
            if "method" in msg:
                # server→client：request（带 id）就地应答，notification（无 id）忽略
                self._handle_server_request(msg)
                continue
            if "id" in msg:
                # 上面已排除 request，到这里带 id 的就是本请求的 response
                if msg["id"] != req_id:
                    raise McpError(f"MCP server 返回了意外的 response id：{msg['id']}")
                if "error" in msg and msg["error"] is not None:
                    err = msg["error"]
                    code = err.get("code") if isinstance(err, dict) else None
                    message = err.get("message") if isinstance(err, dict) else str(err)
                    raise McpError(f"MCP {method} 错误 ({code})：{message}")
                result = msg.get("result")
                if not isinstance(result, dict):
                    raise McpError(f"MCP {method} 返回非对象 result：{result!r}")
                return result
            # 无 method 无 id：协议外消息，忽略（容错）

    def notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        self._require_io().write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # -- 服务器 → 客户端请求 ---------------------------------------------
    def _handle_server_request(self, msg: dict[str, Any]) -> None:
        io = self._require_io()
        method = str(msg.get("method", ""))
        req_id = msg.get("id")
        if method == "roots/list":
            result = {"roots": [{"uri": self._cwd_uri}]}
            if req_id is not None:
                io.write({"jsonrpc": "2.0", "id": req_id, "result": result})
            return
        # 不认识的方法：回 method-not-found（-32601）
        if req_id is not None:
            io.write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
            )

    # -- MCP 工具面 ------------------------------------------------------
    def list_tools(self) -> list[dict[str, Any]]:
        """tools/list 拉取（首次连接后拉一次并缓存，不每次刷新）。"""
        if self._tools is None:
            if "tools" not in self._capabilities:
                self._tools = []  # 服务器声明不支持工具：返回空，不算错
            else:
                result = self.request("tools/list")
                tools = result.get("tools")
                if not isinstance(tools, list):
                    raise McpError(f"MCP tools/list 返回非数组：{tools!r}")
                self._tools = [t for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)]
        return list(self._tools)

    def call_tool(self, name: str, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        """tools/call：name 是原始工具名（无前缀），arguments 逐字段透传。"""
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        content = result.get("content")
        if content is None and "structuredContent" not in result:
            raise McpError(f"MCP tools/call 返回缺少 content：{result!r}")
        return result

    def reconnect(self) -> None:
        """断开重连（新 spawn + 新握手 + 重新拉工具）。失败抛 McpConnectionError。"""
        self.close()
        self.connect()

    # -- 内部 ------------------------------------------------------------
    def _require_io(self) -> _JsonLines:
        if self._io is None or not self.is_alive():
            raise McpConnectionError(f"MCP server {self.config.name!r} 未连接或已退出")
        return self._io

    def _alloc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _drain_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        try:
            for raw in proc.stderr:
                text = raw.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                self._stderr.append(text)
                total = sum(len(line) for line in self._stderr)
                if total > _STDERR_CAP:  # 只留最近一块
                    while self._stderr and sum(len(x) for x in self._stderr) > _STDERR_CAP:
                        self._stderr.pop(0)
        except (OSError, ValueError):
            pass  # 进程退出/管道关闭时自然结束

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr[-20:])
