"""联网工具：web_search（网页搜索）+ fetch_content（抓取单页内容）。

名称对齐 renderer 的字面量识别：src/renderer/conversation.ts 用
`/web_search|fetch_content|get_search_content/` 决定把工具轨迹渲染成"联网搜索"
卡片，并从工具结果里的 markdown 链接提取 sources。因此：
- 工具名固定 `web_search` / `fetch_content`（小写）；
- web_search 结果以 ``- [标题](url)`` 的行形式返回，附简短摘要；
- 结尾提醒模型在最终答复里用 markdown 链接引用来源（对齐 claude WebSearchTool）。

无内置 API：web_search 靠读取桌面主进程写下的搜索 key 配置（与
ember-core 的 home 解析同源），选第一个已配置的 provider 做 HTTP 适配。
未配置任何 key 时返回清晰引导错误（对齐 vision 无 key 时的做法），不内置免 key 兜底。

安全：
- fetch 只允许 http/https，杜绝 file:// / data: 等本地读取；
- 跟随重定向有上限（默认 5），单次请求 30s 超时，响应体有字节上限；
- 权限走 gate.authorize_external（EXTERNAL 类）：PLAN 拒、ASK 询问、无 confirm
  fail-closed；explore 子 agent 只留 READ 工具，天然不带联网能力。
"""
from __future__ import annotations

import html as html_module
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import httpx

from ..permission import PermissionGate
from .registry import Tool, ToolCategory, ToolEnv

WEB_SEARCH_TOOL_NAME = "web_search"
WEB_FETCH_TOOL_NAME = "fetch_content"

# 与 shared/integrations.ts WEB_SEARCH_KEY_FIELDS 相同的字段顺序（配置优先级）。
# 桌面主进程把 key 存到 ~/.ember/web-search.json（EMBER_HOME 覆盖时可换位）。
SEARCH_KEY_FIELDS = ("braveApiKey", "tavilyApiKey", "jinaApiKey", "exaApiKey")

# provider 端点（可被测试 monkeypatch 到本地假服务器）。
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
JINA_SEARCH_URL = "https://s.jina.ai/"
EXA_SEARCH_URL = "https://api.exa.ai/search"

FETCH_TIMEOUT = 30.0       # 秒；对齐 claude WebFetch 的 30s 上限
FETCH_MAX_REDIRECTS = 5
FETCH_MAX_BYTES = 150_000  # 响应体读取上限（防灌爆上下文）
FETCH_RETURN_CHARS = 80_000

_JINA_URL_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_TAG_RE = re.compile(r"<[^>]+>")


def _ember_home() -> Path:
    """与 skills/agents loader 相同的 home 解析：EMBER_HOME 优先，否则 ~。"""
    env_home = os.environ.get("EMBER_HOME")
    return (Path(env_home).expanduser() if env_home else Path.home()).resolve()


def _config_candidates() -> list[Path]:
    paths: list[Path] = []
    raw = os.environ.get("EMBERPY_WEB_SEARCH_CONFIG")
    if raw:
        paths.append(Path(raw).expanduser())
    paths.append(_ember_home() / ".ember" / "web-search.json")
    return paths


def _load_search_config() -> dict[str, str]:
    """读搜索配置，返回已配置的 key（字段名 -> key）。解析失败当空配置。"""
    raw: dict[str, Any] = {}
    for path in _config_candidates():
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            raw = data
            break
    keys: dict[str, str] = {}
    for field in SEARCH_KEY_FIELDS:
        text = str(raw.get(field) or "").strip()
        if text:
            keys[field] = text
    return keys


def _pick_provider(keys: dict[str, str]) -> Optional[tuple[str, str]]:
    """按配置字段顺序挑第一个有 key 的 provider。返回 (字段名, 展示名) 或 None。"""
    display = {"braveApiKey": "Brave", "tavilyApiKey": "Tavily", "jinaApiKey": "Jina", "exaApiKey": "Exa"}
    for field in SEARCH_KEY_FIELDS:
        if field in keys:
            return field, display[field]
    return None


def _no_key_hint() -> str:
    return (
        "错误：未配置联网搜索。请先在 设置 → 联网搜索 里填写任意一个搜索服务的 "
        "API Key（Brave / Tavily / Jina / Exa）。配置会保存到 web-search.json 供引擎读取。"
    )


def _domain_allowed(url: str, allowed: list[str] | None, blocked: list[str] | None) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    if blocked:
        if any(d in host for d in blocked):
            return False
    if allowed:
        return any(d in host for d in allowed)
    return True


# -- 各 provider 适配器（HTTP 调用约 10~20 行一个） -------------------------


def _brave_search(client: httpx.Client, key: str, query: str, max_results: int) -> list[dict[str, str]]:
    response = client.get(
        BRAVE_SEARCH_URL,
        params={"q": query, "count": max_results},
        headers={"Accept": "application/json", "X-Subscription-Token": key},
    )
    if response.status_code != 200:
        raise ValueError(f"Brave 搜索返回 {response.status_code}：{response.text[:200]}")
    items: list[dict[str, str]] = []
    for hit in (response.json().get("web") or {}).get("results") or []:
        items.append({
            "title": hit.get("title") or hit.get("url") or "",
            "url": hit.get("url") or "",
            "snippet": hit.get("description") or hit.get("snippet") or "",
        })
    return items


def _tavily_search(client: httpx.Client, key: str, query: str, max_results: int) -> list[dict[str, str]]:
    response = client.post(
        TAVILY_SEARCH_URL,
        json={"api_key": key, "query": query, "max_results": max_results},
        headers={"Content-Type": "application/json"},
    )
    if response.status_code != 200:
        raise ValueError(f"Tavily 搜索返回 {response.status_code}：{response.text[:200]}")
    items: list[dict[str, str]] = []
    for hit in response.json().get("results") or []:
        items.append({
            "title": hit.get("title") or hit.get("url") or "",
            "url": hit.get("url") or "",
            "snippet": hit.get("content") or "",
        })
    return items


def _jina_search(client: httpx.Client, key: str, query: str, max_results: int) -> list[dict[str, str]]:
    # Jina Search：GET https://s.jina.ai/?q=<query>，返回 markdown 风格文本
    sep = "&" if "?" in JINA_SEARCH_URL else "?"
    url = f"{JINA_SEARCH_URL}{sep}{urllib.parse.urlencode({'q': query})}"
    response = client.get(url, headers={"Authorization": f"Bearer {key}"})
    if response.status_code != 200:
        raise ValueError(f"Jina 搜索返回 {response.status_code}：{response.text[:200]}")
    items: list[dict[str, str]] = []
    lines = response.text.splitlines()
    for line in lines:
        for title, link in _JINA_URL_RE.findall(line):
            items.append({"title": title.strip(), "url": link, "snippet": line[:160]})
            if len(items) >= max_results:
                return items
    return items


def _exa_search(client: httpx.Client, key: str, query: str, max_results: int) -> list[dict[str, str]]:
    response = client.post(
        EXA_SEARCH_URL,
        json={"query": query, "numResults": max_results},
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    if response.status_code != 200:
        raise ValueError(f"Exa 搜索返回 {response.status_code}：{response.text[:200]}")
    items: list[dict[str, str]] = []
    for hit in response.json().get("results") or []:
        items.append({
            "title": hit.get("title") or hit.get("url") or "",
            "url": hit.get("url") or "",
            "snippet": (hit.get("text") or hit.get("snippet") or "")[:240],
        })
    return items


_ADAPTERS = {
    "braveApiKey": _brave_search,
    "tavilyApiKey": _tavily_search,
    "jinaApiKey": _jina_search,
    "exaApiKey": _exa_search,
}


# -- fetch_content ----------------------------------------------------------


def _html_to_text(raw: str) -> str:
    """剥掉 script/style/标签的最小化 HTML->文本（无第三方依赖）。"""
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(p|div|h[1-6]|li|tr|section|article)>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html_module.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _fetch_url(target: str) -> tuple[str, str]:
    """抓取单个页面（仅 http/https），返回 (最终 URL, 文本内容)。"""
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"只支持 http/https 地址，收到 {target!r}。")
    current = target
    with httpx.Client(timeout=FETCH_TIMEOUT) as client:
        for _ in range(FETCH_MAX_REDIRECTS + 1):
            response = client.get(current, follow_redirects=False, headers={"User-Agent": "emberpy/1.0"})
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    break
                current = urllib.parse.urljoin(current, location)
                next_scheme = urllib.parse.urlparse(current).scheme
                if next_scheme not in ("http", "https"):
                    raise ValueError("重定向到不支持的协议，已停止。")
                continue
            break
        if response.status_code != 200:
            raise ValueError(f"抓取失败（HTTP {response.status_code}）")
        raw = response.content[:FETCH_MAX_BYTES]
    text = raw.decode("utf-8", errors="replace")
    if "<" in text[:512] and ">" in text[:512]:
        text = _html_to_text(text)
    return current, text


# -- 工具挂载 ---------------------------------------------------------------


def build_web_tools(env: ToolEnv) -> list[Tool]:
    gate: PermissionGate = env.gate

    def web_search(
        query: str,
        max_results: int = 8,
        allowed_domains: Any = None,
        blocked_domains: Any = None,
    ) -> str:
        # Denied 上抛给 agent._execute（R-G③）：EXTERNAL 授权被拒 -> PermissionDenied hook
        gate.authorize_external(WEB_SEARCH_TOOL_NAME, env.confirm)
        query = (query or "").strip()
        if not query:
            return "错误：query 不能为空。"
        allowed = [d for d in (allowed_domains or []) if isinstance(d, str) and d] if allowed_domains else []
        blocked = [d for d in (blocked_domains or []) if isinstance(d, str) and d] if blocked_domains else []
        if allowed and blocked:
            return "错误：allowed_domains 与 blocked_domains 不能同时指定。"
        count = int(max_results) if isinstance(max_results, int) and 1 <= max_results <= 20 else 8

        keys = _load_search_config()
        picked = _pick_provider(keys)
        if picked is None:
            return _no_key_hint()
        field, display = picked

        try:
            with httpx.Client(timeout=30.0) as client:
                hits = _ADAPTERS[field](client, keys[field], query, count)
        except ValueError as exc:
            return f"错误：{exc}"
        except httpx.TimeoutException:
            return "错误：搜索请求超时，请稍后重试。"
        except httpx.HTTPError as exc:
            return f"错误：搜索请求失败：{exc}"

        if allowed or blocked:
            hits = [h for h in hits if h.get("url") and _domain_allowed(h["url"], allowed, blocked)]
        if not hits:
            return f"搜索“{query}”没有返回结果。"

        lines = [f"搜索“{query}”（来源 {display}，{len(hits)} 条）：", ""]
        for hit in hits[:count]:
            title = (hit.get("title") or "").strip()
            url = (hit.get("url") or "").strip()
            snippet = (hit.get("snippet") or "").strip()
            if not url:
                continue
            lines.append(f"- [{title or url}]({url})")
            if snippet:
                lines.append(f"  {snippet[:200]}")
        lines.append("")
        lines.append("请在你的最终答复里用 markdown 链接引用以上来源。")
        return "\n".join(lines)

    def fetch_content(url: str) -> str:
        # Denied 上抛给 agent._execute（R-G③）
        gate.authorize_external(WEB_FETCH_TOOL_NAME, env.confirm)
        url = (url or "").strip()
        if not url:
            return "错误：url 不能为空。"
        try:
            final_url, text = _fetch_url(url)
        except ValueError as exc:
            return f"错误：{exc}"
        except httpx.TimeoutException:
            return "错误：抓取超时（30s），请换更快的地址或稍后重试。"
        except httpx.HTTPError as exc:
            return f"错误：抓取失败：{exc}"
        if not text.strip():
            return f"（页面 {url} 无可提取的文本内容，已跟随重定向到 {final_url}）"
        clipped = text if len(text) <= FETCH_RETURN_CHARS else (
            text[: FETCH_RETURN_CHARS // 2]
            + f"\n……[中间省略 {len(text) - FETCH_RETURN_CHARS} 字符]……\n"
            + text[-(FETCH_RETURN_CHARS // 2):]
        )
        return f"来自 {final_url} 的网页内容：\n\n{clipped}"

    return [
        Tool(
            name=WEB_SEARCH_TOOL_NAME,
            description=(
                "联网搜索（读取用户在设置里配置的搜索服务 key）。搜索外部网页获取"
                "最新/事实性信息，返回带链接的结果列表。结果需要时效性或超出本地"
                "知识时使用；每次搜索会调用真实网络请求。结果里的来源要在最终答复"
                "里用 markdown 链接引用。未配置搜索服务时请提示用户去 设置→联网搜索 填 key。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词（同搜索引擎自然语言即可）"},
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回几条结果（1-20，默认 8）",
                        "default": 8,
                    },
                    "allowed_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "只保留这些域名内的结果（可选）",
                    },
                    "blocked_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "排除这些域名内的结果（可选，不能与 allowed 同用）",
                    },
                },
                "required": ["query"],
            },
            category=ToolCategory.EXTERNAL,
            fn=web_search,
        ),
        Tool(
            name=WEB_FETCH_TOOL_NAME,
            description=(
                "抓取一个网页的正文内容（仅 http/https，30s 超时，最大 150KB）。"
                "适合 web_search 命中后深读某个页面、或读取接口文档/新闻原文。"
                "返回去标签后的文本。对 PDF/图片等二进制会返回无可提取文本提示。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要抓取的完整 URL（http/https）"},
                },
                "required": ["url"],
            },
            category=ToolCategory.EXTERNAL,
            fn=fetch_content,
        ),
    ]
