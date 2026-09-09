"""消息构件 + 会话转录（界面视角）。

renderer 只消费一小撮事件、字段有限，这里按它需要的形状存转录
（get_messages / get_entries 都能 parse），是"界面是 spec"的实现层。
"""
from __future__ import annotations

import time
from typing import Any, Optional


def now_ms() -> int:
    return int(time.time() * 1000)


def text_part(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def thinking_part(text: str) -> dict[str, str]:
    # renderer 的 getThinking 只认 {type:"thinking", thinking:string}，无 delta/state
    return {"type": "thinking", "thinking": text}


def tool_part(call_id: str, name: str, arguments: Any) -> dict[str, Any]:
    part: dict[str, Any] = {"type": "toolCall", "name": name, "arguments": arguments}
    if call_id:
        part["id"] = call_id
    return part


def pi_message(role: str, content: Any, timestamp: Optional[int] = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": role, "content": content}
    if timestamp is None:
        timestamp = now_ms()
    msg["timestamp"] = timestamp
    return msg


class Transcript:
    """一次进程会话内全部"落定"内容的顺序列表。

    每条 entry：
    - {"type": "message", "message": <消息 record>}               role: user / assistant
    - {"type": "tool_result", "record": {role: toolResult, ...}}  工具输出（挂到前面的助手消息上）
    - {"type": "custom", "customType": "ember-checkpoint", ...}  /undo 用的改前快照

    get_messages()：按顺序摊平成界面的 messages 数组。
    get_entries()：原样返回（界面从中算 /undo 可回滚文件）。
    """

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self._seq = 0

    def add_user(self, text: str) -> dict[str, Any]:
        self._seq += 1
        record = pi_message("user", [text_part(text)])
        record["id"] = f"msg_{self._seq}"
        self.entries.append({"type": "message", "message": record})
        return record

    def add_assistant(
        self,
        text: str,
        tool_parts: list[dict[str, Any]],
        thinking: Optional[str] = None,
    ) -> dict[str, Any]:
        self._seq += 1
        content: list[dict[str, Any]] = []
        # 参考"界面是 spec"：renderer 把思考与可见文本分开渲染，thinking 放最前
        if thinking and thinking.strip():
            content.append(thinking_part(thinking))
        if text:
            content.append(text_part(text))
        content.extend(part for part in tool_parts if part)
        record = pi_message("assistant", content)
        record["id"] = f"msg_{self._seq}"
        self.entries.append({"type": "message", "message": record})
        return record

    def add_tool_result(self, tool_call_id: str, is_error: bool, result_text: str) -> None:
        record: dict[str, Any] = {
            "role": "toolResult",
            "toolCallId": tool_call_id,
            "isError": is_error,
            "timestamp": now_ms(),
            "content": result_text,
        }
        self.entries.append({"type": "tool_result", "record": record})

    def add_checkpoint(self, checkpoint_id: str, files: list[dict[str, Any]]) -> None:
        self.entries.append(
            {
                "type": "custom",
                "customType": "ember-checkpoint",
                "data": {"id": checkpoint_id, "before": files},
            }
        )

    def get_messages(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for entry in self.entries:
            if entry["type"] == "message":
                out.append(entry["message"])
            elif entry["type"] == "tool_result":
                out.append(entry["record"])
        return out

    def get_entries(self) -> list[dict[str, Any]]:
        return list(self.entries)

    def reset(self) -> None:
        self.entries = []
        self._seq = 0

    def count(self) -> dict[str, int]:
        users = assistants = results = tool_calls = 0
        for entry in self.entries:
            if entry["type"] == "tool_result":
                results += 1
                continue
            if entry["type"] != "message":
                continue
            record = entry["message"]
            if record["role"] == "user":
                users += 1
            else:
                assistants += 1
                content = record["content"]
                if isinstance(content, list):
                    tool_calls += sum(1 for p in content if isinstance(p, dict) and p.get("type") == "toolCall")
        return {"users": users, "assistants": assistants, "tool_calls": tool_calls, "tool_results": results}
