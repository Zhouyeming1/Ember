"""模型/提供商层：领域类型 + OpenAI 兼容客户端。

- base 纯数据/解析（无网络），chat 是具体 HTTP 实现
- 异常在 errors.py 定义，这里只做向后兼容的重导出
"""
from ..errors import LLMError, ToolArgumentsError
from .base import LLM, AssistantReply, Message, ToolCall, parse_tool_calls, without_reasoning
from .chat import ChatLLM

__all__ = [
    "LLM",
    "LLMError",
    "ToolArgumentsError",
    "Message",
    "AssistantReply",
    "ToolCall",
    "parse_tool_calls",
    "without_reasoning",
    "ChatLLM",
]
