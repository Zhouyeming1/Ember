"""模型后端的领域类型与协议（不依赖具体 HTTP 客户端）。

对齐 claude-code-analysis 里 services 的思路：把"模型能返回什么"抽象成强类型，
换模型（DeepSeek / Ollama / vLLM / OneAPI）不换代码。

本模块是纯数据 + 解析逻辑，可独立单测；发请求的实现见 chat.py。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


@dataclass(frozen=True)
class ToolCall:
    """模型要调用的一个工具。"""

    id: str
    name: str
    arguments: dict[str, Any]
    raw: str  # 原始参数字符串，诊断用
    parse_error: Optional[str] = None  # 参数非法 JSON 时的说明；正常为 None


@dataclass
class AssistantReply:
    """模型对一轮 complete 的答复。"""

    content: Optional[str]
    tool_calls: list[ToolCall]
    thinking: Optional[str] = None  # 推理/思考文本（reasoner 模型才有；聊天模型为 None）
    usage: Optional[dict[str, Any]] = None
    # 模型返回的原始 assistant 消息 dict，原样交给 Session 落盘，保证历史可重建
    raw_message: dict[str, Any] = field(default_factory=dict)


# 发给模型的消息统一用 OpenAI 兼容 dict（role/content/tool_calls/tool_call_id）
Message = dict[str, Any]


def without_reasoning(message: Message) -> Message:
    """去掉 assistant 消息里的推理字段，用于把它回发给模型。

    DeepSeek reasoner 多轮规范：推理文本（reasoning_content）只能展示给人看，
    回发请求时必须剥掉，否则接口报错。落盘/展示保留原文，仅在"进上下文"
    这一步调它。
    """
    if not isinstance(message, dict) or "reasoning_content" not in message:
        return message
    cleaned = dict(message)
    cleaned.pop("reasoning_content", None)
    return cleaned


class LLM(Protocol):
    """任何实现 complete 的对象都可作为 agent 的模型后端。

    tests 里用 FakeLLM 实现它，从而离线验证循环逻辑。
    """

    def complete(self, messages: list[Message], tools: Optional[list[dict[str, Any]]] = None) -> AssistantReply: ...

    @property
    def model_id(self) -> str: ...


def parse_tool_calls(raw_message: dict[str, Any]) -> list[ToolCall]:
    """把 API 返回的 assistant 消息里的 tool_calls 解析成强类型 ToolCall。

    某一条参数不是合法 JSON 时，只把该条标记 parse_error，不整体抛错——
    这样循环可以把错误原样回传给模型，让它自己修正后重试。
    """
    calls: list[ToolCall] = []
    for entry in raw_message.get("tool_calls") or []:
        function = entry.get("function", {})
        name = function.get("name", "")
        raw = function.get("arguments", "{}") or "{}"
        try:
            arguments = json.loads(raw)
            if not isinstance(arguments, dict):
                raise ValueError("参数必须是 JSON 对象")
            calls.append(ToolCall(id=entry.get("id", ""), name=name, arguments=arguments, raw=raw))
        except (json.JSONDecodeError, ValueError) as exc:
            calls.append(
                ToolCall(
                    id=entry.get("id", ""),
                    name=name,
                    arguments={},
                    raw=raw,
                    parse_error=f"工具 {name} 的参数不是合法 JSON 对象：{raw[:120]!r}",
                )
            )
    return calls
