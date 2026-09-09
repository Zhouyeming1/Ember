"""统一异常目录：跨域共享的异常都收在 errors.py。

单一来源原则：各域模块在包的 __init__ 里重新导出这些异常（旧路径仍可用），
但新代码一律从 emberpy.errors 导入。以后从 claude-code-analysis 移植时，
"某个能力抛什么错"在此归档，避免各处各抛一套：

- 模型/网络层错误  -> LLMError / ToolArgumentsError
- 权限拒绝         -> Denied（工具 / 权限门 / 桥接都会抛）
- 文件 / 补丁层错误 -> FileToolError / NotTextFileError
"""
from __future__ import annotations

from typing import Optional


class EmberpyError(Exception):
    """引擎所有可预期错误的基类（外部集成方可按它统一兜底）。"""


class LLMError(EmberpyError):
    """模型调用层错误（网络、鉴权、超时、接口返回非 200 等）。

    context_exceeded=True 表示请求体超出了模型上下文窗口（400/413 且错误文本
    命中上下文相关特征）。agent 循环据此做"裁旧段重发一次"的自适应恢复；
    普通 4xx（参数错/鉴权）不标记、不重试。
    """

    def __init__(self, message: str, status: Optional[int] = None, context_exceeded: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.context_exceeded = context_exceeded


class ToolArgumentsError(EmberpyError):
    """模型返回的工具参数不是合法 JSON。

    解析层会改为"逐条容忍"（见 ToolCall.parse_error），因此这个异常仅用于
    上层需要整体失败语义的场景，正常循环不依赖它。
    """

    def __init__(self, name: str, raw: str) -> None:
        super().__init__(f"工具 {name} 的参数不是合法 JSON：{raw[:120]!r}")
        self.name = name
        self.raw = raw


class Denied(EmberpyError):
    """某个操作被权限模型拒绝。reason 会原样展示给用户/模型。"""

    def __init__(self, action: str, reason: str) -> None:
        super().__init__(f"{action} 被拒绝：{reason}")
        self.action = action
        self.reason = reason


class FileToolError(EmberpyError):
    """文件工具层的可预期错误（会作为工具结果回传给模型）。"""


class NotTextFileError(FileToolError):
    """目标文件不是 UTF-8 文本，拒绝整写，避免破坏二进制。"""
