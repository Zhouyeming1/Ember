"""会话统计（独立成文件：stats 是纯聚合，不依赖落盘细节）。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionStats:
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    tool_results: int = 0
    patches: int = 0

    @property
    def total_messages(self) -> int:
        return self.user_messages + self.assistant_messages + self.tool_results
