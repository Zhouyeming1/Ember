"""OpenAI 兼容的 chat/completions 客户端（DeepSeek / Ollama / vLLM / OneAPI…）。

只负责"把消息发出去、把答复读进来"，不做任何 agent 逻辑。

健壮性（P3）：
- 有限重试：408/409/429/5xx 与瞬时网络错误重试 3 次（指数退避 0.5s/1s/2s，
  可被 retry_delays 覆盖用于测试），尊重服务端 Retry-After；其它 4xx 不重试。
- 上下文超限识别：400/413 且错误文本含上下文特征 -> LLMError.context_exceeded，
  由 agent 循环做"裁旧段重发一次"的兜底（正常路径靠 auto-compact 预防）。
- 超时拆分 connect/read/write/pool：流式 SSE 的 read 空闲看门狗（默认 180s
  无数据判超时），不再半挂卡死整轮。
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional

import httpx

from ..errors import LLMError
from .base import Message, AssistantReply, parse_tool_calls

# 可重试的状态码：服务器过载/网关类瞬时问题
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
# 上下文超限错误文本特征（命中任一即标记 context_exceeded）
_CONTEXT_MARKERS = (
    "context_length_exceeded",
    "maximum context length",
    "context window",
    "context length",
    "input is too long",
    "prompt is too long",
    "request too large",
    "too many tokens",
    "exceeds the maximum",
    "max_input_tokens",
    "max_input_length",
)
_RETRY_DELAYS = (0.5, 1.0, 2.0)  # 指数退避基线（秒）


def _is_context_exceeded(status: int, detail: str) -> bool:
    if status not in (400, 413):
        return False
    lower = detail.lower()
    return any(marker in lower for marker in _CONTEXT_MARKERS)


def _build_error(status: int, detail: str) -> LLMError:
    text = detail[:300]
    return LLMError(
        f"模型接口返回 {status}：{text}",
        status=status,
        context_exceeded=_is_context_exceeded(status, text),
    )


class ChatLLM:
    """OpenAI 兼容的 chat/completions 客户端。"""

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com",
        timeout: float = 180.0,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        retry_delays: Optional[tuple[float, ...]] = None,
    ) -> None:
        if not api_key:
            raise ValueError("缺少 API Key：请设置环境变量 DEEPSEEK_API_KEY（或显式传入 api_key）")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.retry_delays = retry_delays if retry_delays is not None else _RETRY_DELAYS
        # 超时拆分：read 是"两次读到数据间的空闲上限"（SSE 看门狗用整个 timeout），
        # connect/write 给较短上限，避免连不上/卡上传时也等满一个总超时
        self._timeout = httpx.Timeout(
            connect=min(30.0, timeout),
            read=timeout,
            write=min(90.0, timeout),
            pool=15.0,
        )

    def _sleep_before_retry(self, attempt: int, response: Optional[httpx.Response]) -> None:
        """按指数退避（可被 Retry-After 覆盖）等待下一次重试。"""
        wait: Optional[float] = None
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = None  # HTTP-date 形式不值得解析，走指数退避
        if wait is None:
            index = min(attempt - 1, len(self.retry_delays) - 1)
            wait = self.retry_delays[index]
        time.sleep(max(0.0, min(wait, 15.0)))

    @property
    def model_id(self) -> str:
        return self.model

    def _is_reasoner(self) -> bool:
        """reasoner 模型（如 deepseek-reasoner）走推理；协议上与聊天模型有差异。"""
        return "reasoner" in self.model.lower()

    def _endpoint(self) -> str:
        # 容忍用户填 https://api.deepseek.com 或 https://api.deepseek.com/v1 两种风格
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    def _build_payload(self, messages: list[Message], tools: Optional[list[dict[str, Any]]], *, stream: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if stream:
            payload["stream"] = True
        # DeepSeek reasoner 不支持 temperature/top_p 等采样参数，带了会 400；聊天模型照常发
        if not self._is_reasoner():
            payload["temperature"] = self.temperature
        if tools:
            payload["tools"] = tools
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _should_retry(self, response: Optional[httpx.Response], status_code: Optional[int]) -> bool:
        """可重试条件：状态码命中集合，或响应体还没回来就断（网络级瞬时失败）。"""
        if status_code is not None:
            return status_code in _RETRYABLE_STATUS
        # 无 HTTP 状态 = 请求还没收到完整响应（超时/连接被断），瞬时网络问题也重试
        return response is None

    def complete(self, messages: list[Message], tools: Optional[list[dict[str, Any]]] = None) -> AssistantReply:
        payload = self._build_payload(messages, tools)
        headers = self._headers()

        with httpx.Client(timeout=self._timeout) as client:
            response: Optional[httpx.Response] = None
            for attempt in range(1, len(self.retry_delays) + 2):
                try:
                    response = client.post(self._endpoint(), headers=headers, json=payload)
                except httpx.TimeoutException as exc:
                    if attempt < len(self.retry_delays) + 1:
                        self._sleep_before_retry(attempt, None)
                        continue
                    raise LLMError(f"模型请求超时（{self.timeout}s）") from exc
                except httpx.HTTPError as exc:
                    raise LLMError(f"模型请求失败：{exc}") from exc

                if response.status_code == 200:
                    break
                if response.status_code in _RETRYABLE_STATUS and attempt < len(self.retry_delays) + 1:
                    self._sleep_before_retry(attempt, response)
                    continue
                # 不可重试的错误（参数/鉴权/上下文超限等）：带标记抛出
                raise _build_error(response.status_code, response.text)

        if response is None or response.status_code != 200:
            raise LLMError("模型请求失败（重试耗尽后仍无响应）")
        data = response.json()
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError) as exc:
            raise LLMError("模型接口返回格式异常（缺少 choices）") from exc

        raw_message = choice.get("message", {}) or {}
        content = raw_message.get("content")
        # DeepSeek reasoner 把推理文本放在 message.reasoning_content；聊天模型没有该字段
        thinking = raw_message.get("reasoning_content") or None
        if isinstance(thinking, str):
            thinking = thinking.strip() or None
        tool_calls = parse_tool_calls(raw_message)
        return AssistantReply(
            content=content,
            thinking=thinking,
            tool_calls=tool_calls,
            usage=data.get("usage"),
            raw_message=raw_message,
        )

    def stream_complete(
        self,
        messages: list[Message],
        tools: Optional[list[dict[str, Any]]] = None,
        on_delta: Optional[Callable[[str, Optional[str]], None]] = None,
    ) -> AssistantReply:
        """流式完整答复：SSE 逐段把 reasoning/content delta 累加后回调。

        on_delta(text_累计, thinking_累计) 每次增量调一次，参数是"当前完整文本"
        与"当前完整推理文本"——调用方（worker）据此发 message_update 累计全文，
        renderer 的整文本覆盖/thinking 去重语义天然幂等。仍在结尾一次性返回
        完整 AssistantReply（含 tool_calls），循环逻辑与 complete 完全一致。
        """
        payload = self._build_payload(messages, tools, stream=True)
        headers = self._headers()
        text_parts: list[str] = []
        think_parts: list[str] = []
        tool_acc: dict[int, dict[str, Any]] = {}

        def _flush() -> None:
            if on_delta is None:
                return
            text = "".join(text_parts)
            thinking = "".join(think_parts).strip() or None
            on_delta(text, thinking)

        def _apply(delta: dict[str, Any]) -> None:
            changed = False
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                think_parts.append(reasoning)
                changed = True
            content = delta.get("content")
            if isinstance(content, str) and content:
                text_parts.append(content)
                changed = True
            for tc in delta.get("tool_calls") or []:
                index = tc.get("index", 0)
                entry = tool_acc.setdefault(
                    index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if tc.get("id"):
                    entry["id"] = tc["id"]
                function = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                name = function.get("name")
                if name:
                    entry["function"]["name"] = name  # 服务端通常只送一次全名
                arguments = function.get("arguments")
                if isinstance(arguments, str) and arguments:
                    entry["function"]["arguments"] += arguments
                changed = True
            if changed:
                _flush()

        usage: dict[str, Any] | None = None
        max_attempts = len(self.retry_delays) + 1
        for attempt in range(1, max_attempts + 1):
            # 每次尝试重新累积：429/5xx 重试前可能已发半截内容，从零开始最干净
            text_parts.clear()
            think_parts.clear()
            tool_acc.clear()
            ok = False
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    with client.stream("POST", self._endpoint(), headers=headers, json=payload) as response:
                        if response.status_code != 200:
                            detail = response.read().decode("utf-8", errors="replace")[:300]
                            if response.status_code in _RETRYABLE_STATUS and attempt < max_attempts:
                                self._sleep_before_retry(attempt, response)
                                continue  # 退出两个 with，进入下一轮尝试
                            raise _build_error(response.status_code, detail)
                        usage = None
                        finished = False
                        for raw_line in response.iter_lines():
                            if not raw_line or not raw_line.startswith("data:"):
                                continue
                            data = raw_line[5:].strip()
                            if not data or data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            # usage 可能跟 finish_reason 同块、也可能在其后单独一条（DeepSeek 两者都有）：
                            # 记下但不因它提前结束，继续读到 [DONE] 以防漏最后一条 usage
                            if chunk.get("usage"):
                                usage = chunk["usage"]
                            choices = chunk.get("choices") or []
                            if not choices or finished:
                                continue
                            delta = choices[0].get("delta", {}) or {}
                            if isinstance(delta, dict):
                                _apply(delta)
                            if choices[0].get("finish_reason") is not None:
                                finished = True
                        ok = True
            except httpx.TimeoutException as exc:
                if attempt < max_attempts:
                    self._sleep_before_retry(attempt, None)
                    continue
                raise LLMError(f"模型请求超时（{self.timeout}s）") from exc
            except httpx.HTTPError as exc:
                if attempt < max_attempts:
                    self._sleep_before_retry(attempt, None)
                    continue
                raise LLMError(f"模型请求失败：{exc}") from exc
            if ok:
                break

        content = "".join(text_parts) or None
        thinking = "".join(think_parts).strip() or None
        raw_message: dict[str, Any] = {"role": "assistant"}
        if content is not None:
            raw_message["content"] = content
        if thinking:
            raw_message["reasoning_content"] = thinking
        if tool_acc:
            ordered = [tool_acc[index] for index in sorted(tool_acc)]
            raw_message["tool_calls"] = ordered
        return AssistantReply(
            content=content,
            thinking=thinking,
            tool_calls=parse_tool_calls(raw_message),
            usage=usage,
            raw_message=raw_message,
        )
