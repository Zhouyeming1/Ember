"""离线测试/嵌入用的小工具：假模型 + 造工具调用 + 便捷 Agent 工厂。

为什么放在包里而不是 tests/：tests/ 会被拆成多层目录，若把 FakeLLM 放在
conftest 里，深层测试 import 它要走一堆 sys.path 技巧；放包里则任何测试
都能 ``from emberpy.testing import FakeLLM``。也方便下游做二次开发时
（本地跑一个 Agent、不烧 API）直接复用。

这些只是测试替身，绝不参与任何线上路径（rpc/cli 都不会 import 本模块）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from .agent import Agent
from .llm import AssistantReply, Message, parse_tool_calls
from .permission import Confirm
from .session import Session
from .tools import ToolEnv


def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """构造 OpenAI 风格的 tool_call dict（arguments 是 JSON 字符串）。"""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


class FakeLLM:
    """假模型：complete() 按顺序弹出脚本里的回复。

    每个脚本项是 {"content": str|None, "calls": [tool_call dict,...]|None,
    "reasoning": str|None}。reasoning 模拟 reasoner 的推理文本（写进
    raw 的 reasoning_content，可测 thinking 渲染链路）。
    脚本耗尽后固定返回一条文本，避免无限循环。
    """

    def __init__(self, script: list[dict[str, Any]], model: str = "fake-model") -> None:
        self._script = list(script)
        self.model = model
        self.complete_calls: list[tuple[int, Optional[list]]] = []  # (消息数, tools)

    @property
    def model_id(self) -> str:
        return self.model

    def complete(self, messages: list[Message], tools: Optional[list[dict[str, Any]]] = None) -> AssistantReply:
        self.complete_calls.append((len(messages), tools))
        if self._script:
            item = self._script.pop(0)
        else:
            item = {"content": "（假模型脚本已耗尽）", "calls": None}
        return self._reply_from(item)

    def _reply_from(self, item: dict[str, Any]) -> AssistantReply:
        """把一个脚本项变成 AssistantReply（complete / stream 共用）。"""
        raw: dict[str, Any] = {"role": "assistant"}
        if item.get("content") is not None:
            raw["content"] = item["content"]
        if item.get("calls"):
            raw["tool_calls"] = item["calls"]
        reasoning = item.get("reasoning")
        if isinstance(reasoning, str) and reasoning.strip():
            raw["reasoning_content"] = reasoning
        return AssistantReply(
            content=item.get("content"),
            thinking=reasoning if isinstance(reasoning, str) and reasoning.strip() else None,
            tool_calls=parse_tool_calls(raw),
            # 脚本项可带 "usage" 模拟真实模型的 token 用量；缺省给全 0 的空 usage
            usage=item.get("usage") if isinstance(item.get("usage"), dict) else {"total_tokens": 0},
            raw_message=raw,
        )


class StreamingLLM(FakeLLM):
    """支持 stream_complete 的假模型：脚本项可带 "deltas" 先逐段发增量再回最终答复。

    deltas: [{"text": "本段文本累计", "thinking": "本段推理累计"}, ...]
    用于离线验证 worker 的 message_update 流式链路（ChatLLM 真实现走 SSE）。
    有 stream_complete 而没有它即纯 complete 的 FakeLLM 保持原样（非流式路径不变）。
    """

    def stream_complete(
        self,
        messages: list[Message],
        tools: Optional[list[dict[str, Any]]] = None,
        on_delta: Optional[Callable[[str, Optional[str]], None]] = None,
    ) -> AssistantReply:
        if self._script:
            item = self._script.pop(0)
        else:
            item = {"content": "（假模型脚本已耗尽）", "calls": None}
        if on_delta:
            for delta in item.get("deltas") or []:
                on_delta(str(delta.get("text", "")), delta.get("thinking"))
        return self._reply_from(item)


def make_agent(
    script: list[dict[str, Any]],
    workspace: Path,
    mode: str = "auto",
    confirm: Optional[Confirm] = None,
    *,
    allow_subagents: bool = False,
    subagent_llm: Optional[Any] = None,
    llm: Optional[Any] = None,
) -> Agent:
    """按脚本 + 模式造一个 Agent（内存会话，不落盘）。

    allow_subagents=True 时注册表带 run_agent；subagent_llm 可另给一个 FakeLLM
    专门喂子 agent（主脚本与子脚本分开写，避免嵌套时挤一个脚本）。
    """
    env = ToolEnv(workspace=workspace, gate=None, patches=None, confirm=confirm)
    return Agent(
        llm=llm if llm is not None else FakeLLM(script),
        workspace=workspace,
        mode=mode,
        env=env,
        session=Session(cwd=workspace),
        allow_subagents=allow_subagents,
        subagent_llm=subagent_llm,
    )
