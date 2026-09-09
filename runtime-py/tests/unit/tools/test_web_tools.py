"""联网工具测试（本地假 HTTP 服务器，不碰真实网络）。

覆盖：工具注册与 schema、无 key 引导、provider 选取顺序（brave/tavily/exa）、
各适配器的请求形状（header/body/path）、域名过滤、权限（plan 拒 / ask 无
confirm 拒）、fetch_content 的文本抽取 / 重定向 / 协议限制。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from emberpy.errors import Denied
from emberpy.patches import PatchStore
from emberpy.permission import PermissionGate, PermissionMode
from emberpy.tools import ToolEnv, ToolCategory, default_registry
from emberpy.tools import web as web_mod

BRAVE_HIT = {
    "web": {
        "results": [
            {"title": "Anthropic", "url": "https://www.anthropic.com", "description": "Claude 的家"},
            {"title": "Example", "url": "https://example.com", "description": "无关站点"},
        ]
    }
}
EXA_HITS = {
    "results": [
        {"title": "Exa 结果", "url": "https://exa.ai", "text": "神经搜索"},
    ]
}
TAVILY_HITS = {
    "results": [
        {"title": "Tavily 结果", "url": "https://tavily.com", "content": "搜索 API"},
    ]
}


class _Handler(BaseHTTPRequestHandler):
    """按路径回不同 payload 的假搜索/网页服务器，并记录请求供断言。"""

    requests: list[tuple[str, str, dict[str, str], bytes]] = []

    def log_message(self, *_: Any) -> None:  # 静默访问日志
        pass

    def _record(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        _Handler.requests.append((self.command, self.path, dict(self.headers), body))

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _text(self, code: int, body: str, headers: dict[str, str] | None = None) -> None:
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        self._record()
        if self.path.startswith("/redirect"):
            self._text(302, "", {"Location": "/page"})
        elif self.path.startswith("/page"):
            self._text(200, "<html><head><title>页</title></head><body><script>var x=1;</script><p>正文你好</p></body></html>")
        elif self.path.startswith("/brave"):
            self._json(200, BRAVE_HIT)
        elif self.path.startswith("/exa"):
            self._json(200, EXA_HITS)
        elif self.path.startswith("/jina"):
            self._text(200, "- [Jina 文档](https://jina.ai/reader)\n简介一行\n")
        else:
            self._text(404, "not found")

    def do_POST(self) -> None:
        self._record()
        if self.path.startswith("/tavily"):
            self._json(200, TAVILY_HITS)
        elif self.path.startswith("/exa"):
            self._json(200, EXA_HITS)
        else:
            self._text(404, "not found")


@pytest.fixture()
def web_server() -> Any:
    _Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()


@pytest.fixture()
def config_file(tmp_path: Path, monkeypatch: Any) -> Path:
    """写一份 web-search.json 到 tmp，并让配置读取指向它。"""
    path = tmp_path / "web-search.json"
    monkeypatch.setenv("EMBERPY_WEB_SEARCH_CONFIG", str(path))
    return path


def _env(workspace: Path, mode: str = "auto", confirm=None):
    gate = PermissionGate(PermissionMode(mode), workspace)
    patches = PatchStore()
    env = ToolEnv(workspace=workspace, gate=gate, patches=patches, confirm=confirm)
    return default_registry(env)


class TestRegistration:
    def test_web_tools_present_and_external(self, workspace: Path) -> None:
        reg = _env(workspace)
        assert "web_search" in reg.names()
        assert "fetch_content" in reg.names()
        assert reg.get("web_search").category is ToolCategory.EXTERNAL
        assert reg.get("fetch_content").category is ToolCategory.EXTERNAL
        schema = reg.get("web_search").schema()["function"]
        assert "query" in schema["parameters"]["required"]

    def test_no_config_guides_user(self, workspace: Path, config_file: Path) -> None:
        config_file.write_text(json.dumps({"workflow": "auto-summary"}), encoding="utf-8")
        reg = _env(workspace)
        out = reg.get("web_search").fn(query="pytest 用法")
        assert "未配置联网搜索" in out


class TestSearchProviders:
    def test_brave_request_shape_and_markdown(self, workspace: Path, config_file: Path, web_server: Any, monkeypatch: Any) -> None:
        config_file.write_text(json.dumps({"workflow": "auto-summary", "braveApiKey": "bk"}), encoding="utf-8")
        base = f"http://127.0.0.1:{web_server.server_address[1]}"
        monkeypatch.setattr(web_mod, "BRAVE_SEARCH_URL", base + "/brave")
        monkeypatch.setattr(web_mod, "EXA_SEARCH_URL", base + "/exa")  # 防误选
        reg = _env(workspace)
        out = reg.get("web_search").fn(query="claude", allowed_domains=["anthropic.com"])

        # 只留 allowed 域名内的结果；markdown 链接 + 来源提示
        assert "- [Anthropic](https://www.anthropic.com)" in out
        assert "Example" not in out
        assert "请在你的最终答复里用 markdown 链接引用以上来源" in out

        method, path, headers, _ = _Handler.requests[0]
        assert method == "GET" and path.startswith("/brave")
        assert headers.get("X-Subscription-Token") == "bk"

    def test_exa_selected_when_only_exa_key(self, workspace: Path, config_file: Path, web_server: Any, monkeypatch: Any) -> None:
        config_file.write_text(json.dumps({"exaApiKey": "ek"}), encoding="utf-8")
        base = f"http://127.0.0.1:{web_server.server_address[1]}"
        monkeypatch.setattr(web_mod, "EXA_SEARCH_URL", base + "/exa")
        reg = _env(workspace)
        out = reg.get("web_search").fn(query="agent")
        assert "- [Exa 结果](https://exa.ai)" in out
        assert _Handler.requests[0][0] == "POST" and _Handler.requests[0][1].startswith("/exa")

    def test_tavily_post_body(self, workspace: Path, config_file: Path, web_server: Any, monkeypatch: Any) -> None:
        config_file.write_text(json.dumps({"tavilyApiKey": "tk"}), encoding="utf-8")
        base = f"http://127.0.0.1:{web_server.server_address[1]}"
        monkeypatch.setattr(web_mod, "TAVILY_SEARCH_URL", base + "/tavily")
        reg = _env(workspace)
        out = reg.get("web_search").fn(query="qianfan")
        assert "- [Tavily 结果](https://tavily.com)" in out
        method, path, _, body = _Handler.requests[0]
        payload = json.loads(body)
        assert payload["api_key"] == "tk" and payload["query"] == "qianfan"

    def test_blocked_domains(self, workspace: Path, config_file: Path, web_server: Any, monkeypatch: Any) -> None:
        config_file.write_text(json.dumps({"braveApiKey": "bk"}), encoding="utf-8")
        base = f"http://127.0.0.1:{web_server.server_address[1]}"
        monkeypatch.setattr(web_mod, "BRAVE_SEARCH_URL", base + "/brave")
        reg = _env(workspace)
        out = reg.get("web_search").fn(query="x", blocked_domains=["example.com"])
        assert "Anthropic" in out
        assert "Example" not in out

    def test_allowed_and_blocked_together_rejected(self, workspace: Path, config_file: Path, monkeypatch: Any) -> None:
        config_file.write_text(json.dumps({"braveApiKey": "bk"}), encoding="utf-8")
        reg = _env(workspace)
        out = reg.get("web_search").fn(query="x", allowed_domains=["a.com"], blocked_domains=["b.com"])
        assert "不能同时指定" in out


class TestPermissions:
    def test_plan_mode_denies_web(self, workspace: Path) -> None:
        reg = _env(workspace, mode="plan")
        # R-G③：EXTERNAL 授权被拒的 Denied 上抛（agent 层统一转工具结果）
        with pytest.raises(Denied):
            reg.get("web_search").fn(query="x")

    def test_ask_without_confirm_denies(self, workspace: Path) -> None:
        reg = _env(workspace, mode="ask")
        with pytest.raises(Denied):
            reg.get("fetch_content").fn(url="https://example.com")

    def test_full_mode_allows(self, workspace: Path, web_server: Any) -> None:
        reg = _env(workspace, mode="full")
        # 无配置时到不了网络，先到"未配置"提示也算放行
        out = reg.get("web_search").fn(query="x")
        assert "拒绝" not in out


class TestFetch:
    def test_fetch_strips_html(self, workspace: Path, web_server: Any) -> None:
        base = f"http://127.0.0.1:{web_server.server_address[1]}"
        reg = _env(workspace)
        out = reg.get("fetch_content").fn(url=base + "/page")
        assert out.startswith("来自 " + base + "/page")
        assert "正文你好" in out
        assert "var x=1" not in out  # script 已剥掉

    def test_fetch_follows_redirect(self, workspace: Path, web_server: Any) -> None:
        base = f"http://127.0.0.1:{web_server.server_address[1]}"
        reg = _env(workspace)
        out = reg.get("fetch_content").fn(url=base + "/redirect")
        assert "正文你好" in out
        assert base + "/page" in out  # 报的是最终 URL

    def test_fetch_rejects_non_http(self, workspace: Path) -> None:
        reg = _env(workspace)
        out = reg.get("fetch_content").fn(url="file:///C:/secret.txt")
        assert "只支持 http/https" in out
