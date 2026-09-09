"""vision 工具：看用户上传的图片（对齐桌面端原先 JS 扩展的 GLM 主路径）。

前端把粘贴的图片落盘到 uploads 目录、把一段带 `​[vision]` 的指示文本发给引擎
（"先调用 vision 工具查看：\\n- <abs路径>"）。引擎据此工具读图 → base64 data-URI →
调用配置的视觉模型（chat/completions，OpenAI 兼容 image_url）→ 把识别文本回给主模型。

范围克制（相比 Node 扩展）：
- 只做 GLM/DeepSeek 视觉 API 主路径；**无 key 时返回清晰引导错误**，不内置 MinerU
  免费 OCR fallback（异步上传+轮询多跳，留给后续）。
- 读取范围白名单（isVisionReadable 等价）：只读 uploads 目录与工作区内文件，绝不
  让模型拿这个工具读任意盘上路径。
- 工具常驻注册表；无图/无可读文件时返回错误文本，模型会学到"只有本轮用户带了图
  才调用"。名称固定小写 `vision`（renderer 按字面量识别并渲染成"识图"轨迹）。

配置来源（优先级从高到低）：env `EMBERPY_VISION_CONFIG`（测试/覆盖）→
桌面主进程写下的 `<appData>/Ember/vision-config.json` → 全缺时用默认端点 + env key。
config 顶层形状 {provider, endpoint, model, apiKey}（主进程已归一化好 endpoint/model）。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import sys
from pathlib import Path
from typing import Any, Optional

from .registry import Tool, ToolCategory, ToolEnv

MAX_VISION_IMAGES = 4
_REQUEST_TIMEOUT = 180  # 秒（对齐 Node AbortSignal.timeout(180_000)）

# 与 shared/vision-api.ts 相同的默认视觉模型端点（provider=custom -> GLM）
DEFAULT_CUSTOM_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
DEFAULT_CUSTOM_MODEL = "glm-4v-flash"
DEEPSEEK_BASE = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash-vision-exp"

_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".png": "image/png",
    ".bmp": "image/bmp",
}
_IMAGE_EXTS = (_MIME_BY_EXT.keys())


def _app_data_dir() -> Optional[Path]:
    """等价 Electron app.getPath('appData')：桌面主进程把 userData 放在它下面 /Ember。"""
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "Ember"
    if sys.platform == "darwin":
        home = os.environ.get("HOME")
        if home:
            return Path(home) / "Library" / "Application Support" / "Ember"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "Ember"


def _config_candidates() -> list[Path]:
    paths: list[Path] = []
    for key in ("EMBERPY_VISION_CONFIG",):
        raw = os.environ.get(key)
        if raw:
            paths.append(Path(raw).expanduser())
    app_dir = _app_data_dir()
    if app_dir is not None:
        paths.append(app_dir / "vision-config.json")
    return paths


def _env_api_key(provider: str) -> str:
    # provider deepseek 时优先官方 key；custom（默认 GLM）用 ZHIPU key，也兜底官方 key
    if provider == "deepseek":
        return os.environ.get("DEEPSEEK_API_KEY", "").strip()
    return os.environ.get("ZHIPU_API_KEY", "").strip() or os.environ.get("DEEPSEEK_API_KEY", "").strip()


def load_vision_config() -> dict[str, Any]:
    """读视觉配置。始终返回 {provider, endpoint, model, apiKey}（可缺 key）。"""
    raw: dict[str, Any] = {}
    for path in _config_candidates():
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            raw = data
            break

    provider = "deepseek" if str(raw.get("provider") or "").strip() == "deepseek" else "custom"
    endpoint = (raw.get("endpoint") or "").strip()
    model = (raw.get("model") or "").strip()
    api_key = (raw.get("apiKey") or "").strip()

    if provider == "deepseek":
        root = endpoint or DEEPSEEK_BASE
        root = root.rstrip("/").removesuffix("/chat/completions").rstrip("/")
        return {
            "provider": provider,
            "endpoint": f"{root or DEEPSEEK_BASE}/chat/completions",
            "model": model or DEFAULT_DEEPSEEK_MODEL,
            "apiKey": api_key or _env_api_key(provider),
        }
    return {
        "provider": provider,
        "endpoint": endpoint or DEFAULT_CUSTOM_ENDPOINT,
        "model": model or DEFAULT_CUSTOM_MODEL,
        "apiKey": api_key or _env_api_key(provider),
    }


def _uploads_dir() -> Optional[Path]:
    """uploads 白名单根目录：桌面主进程把粘贴图片落盘到这（userData/uploads）。"""
    raw = os.environ.get("EMBERPY_VISION_UPLOADS")
    if raw:
        return Path(raw)
    app_dir = _app_data_dir()
    return (app_dir / "uploads") if app_dir is not None else None


def _readable_roots(workspace: Path) -> list[str]:
    """允许 vision 读取的根目录集合（isVisionReadable 的白名单）。"""
    roots: list[str] = []
    uploads = _uploads_dir()
    if uploads is not None:
        roots.append(str(uploads))
    roots.append(str(workspace.resolve()))
    return roots


def _is_readable(file: str, roots: list[str]) -> bool:
    resolved = file.replace("\\", "/")
    return any(
        bool(base) and (resolved == base or resolved.startswith(f"{base}/"))
        for base in (root.replace("\\", "/").rstrip("/") for root in roots)
    )


def _to_data_uri(path: Path) -> str:
    mime = _MIME_BY_EXT.get(path.suffix.lower())
    if mime is None:
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
    bytes_ = path.read_bytes()
    return f"data:{mime};base64,{base64.b64encode(bytes_).decode('ascii')}"


def _extract_text(payload: Any) -> str:
    """OpenAI 兼容响应里取助手文本（等价 vision-api.visionText）。"""
    try:
        choices = payload.get("choices") or []
        content = choices[0].get("message", {}).get("content") if choices else None
    except AttributeError:
        content = None
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        text = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ).strip()
        if text:
            return text
    raise ValueError("接口没有返回图片识别结果")


def _call_vision(config: dict[str, Any], data_uris: list[str], prompt: str) -> str:
    """POST chat/completions（与 vision-api.visionRequest / analyzeGlm 等价）。"""
    import httpx

    text = prompt.strip() or "请详细描述这张图片的内容"
    body = {
        "model": config["model"],
        "messages": [
            {
                "role": "user",
                "content": [
                    *({"type": "image_url", "image_url": {"url": uri}} for uri in data_uris),
                    {"type": "text", "text": text},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {config['apiKey']}",
        "Content-Type": "application/json",
    }
    try:
        response = httpx.post(config["endpoint"], json=body, headers=headers, timeout=_REQUEST_TIMEOUT)
    except Exception as exc:  # 网络错误/超时
        raise ValueError(f"请求视觉模型失败：{exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code != 200:
        detail = ""
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                detail = error["message"]
            elif isinstance(payload.get("message"), str):
                detail = payload["message"]
        raise ValueError(detail or f"图片识别失败（{response.status_code}）")
    return _extract_text(payload)


def build_vision_tool(env: ToolEnv) -> Tool:
    """构造 vision 工具（固定小写名，renderer 按字面量识别成"识图"轨迹）。"""

    def vision(paths: Any = None, prompt: Optional[str] = None) -> str:
        # 参数防御：schema 要求 list，个别模型可能传单串
        if isinstance(paths, str):
            paths = [paths]
        files = [p for p in (paths or []) if isinstance(p, str) and p.strip()]
        if not files:
            return "错误：本轮没有图片输入。只有用户本轮消息里贴了图片时才需要调用 vision 工具。"

        roots = _readable_roots(env.workspace)
        blocked = next((f for f in files if not _is_readable(f, roots)), None)
        if blocked is not None:
            return f"错误：图片不在允许目录：{blocked}"

        # 逐个读、逐个做存在性检查，一次性给全错误比丢一半好
        data_uris: list[str] = []
        for file in files[:MAX_VISION_IMAGES]:
            path = Path(file)
            try:
                data_uris.append(_to_data_uri(path))
            except OSError as exc:
                return f"错误：读取图片 {file} 失败：{exc}"

        config = load_vision_config()
        if not config["apiKey"]:
            return (
                "错误：未配置视觉模型 API Key。请先在 设置 → 图片识别 中选择 DeepSeek "
                "或填写自定义 API（GLM-4V 等）。当前引擎未内置免 Key 的 OCR 兜底。"
            )
        try:
            text = _call_vision(config, data_uris, prompt or "")
        except ValueError as exc:
            return f"错误：{exc}"
        if not text:
            return "错误：图片识别未能提取出有效内容"
        return text

    return Tool(
        name="vision",
        description=(
            "Look at user-pasted image files with the configured vision model. "
            "Only call when this turn's user message attached images (they are listed "
            "as absolute paths in the prompt). Never call for HTML, CSS, or code edits."
        ),
        parameters={
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "用户本轮上传图片的绝对路径列表",
                },
                "prompt": {
                    "type": "string",
                    "description": "要看什么。缺省做详细视觉描述。",
                },
            },
            "required": ["paths"],
        },
        category=ToolCategory.READ,
        fn=vision,
    )
