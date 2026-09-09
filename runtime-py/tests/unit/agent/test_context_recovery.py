"""上下文超限自适应恢复测试：模型首抛 context_exceeded -> 裁旧段重发一次。

验证：只兜底一次、旧历史被裁掉、当前任务仍在上下文、正常恢复出最终答复。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from emberpy.agent import Agent
from emberpy.errors import LLMError
from emberpy.llm import AssistantReply
from emberpy.session import Session


class _BoomOnceLLM:
    """第一次 complete 抛上下文超限，之后正常返回（记下每次收到的消息）。"""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self._raised = False

    model_id = "boom-stub"

    def complete(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None) -> AssistantReply:
        self.calls.append(list(messages))
        if not self._raised:
            self._raised = True
            raise LLMError(
                "This model's maximum context length is 128000 tokens.",
                status=400,
                context_exceeded=True,
            )
        return AssistantReply(
            content="恢复成功",
            thinking=None,
            tool_calls=[],
            usage={},
            raw_message={"role": "assistant", "content": "恢复成功"},
        )


def _user_texts(messages: list[dict[str, Any]]) -> list[str]:
    return [str(m.get("content") or "") for m in messages if m.get("role") == "user"]


def test_context_exceeded_trims_old_events_and_retries(workspace: Path) -> None:
    sess = Session(cwd=workspace)  # path=None -> 内存会话
    sess.add_user("早期任务")
    sess.add_assistant({"role": "assistant", "content": "早期答复"})
    stub = _BoomOnceLLM()

    agent = Agent(llm=stub, workspace=workspace, mode="auto", session=sess, max_steps=5)
    result = agent.run("当前任务")

    assert result.final_content == "恢复成功"
    assert len(stub.calls) == 2
    # 第一次尝试：早期历史还在上下文里
    assert any("早期任务" in text for text in _user_texts(stub.calls[0]))
    # 恢复后的重发：早期 user 已被裁掉，只剩当前任务
    second_users = _user_texts(stub.calls[1])
    assert "当前任务" in second_users
    assert not any("早期任务" in text for text in second_users)
    # 会话事件也已同步裁掉（disk 上若持久化会是原子重写；内存这里直接看 events）
    remaining_users = [
        e["message"]["content"] for e in sess.events() if e.get("type") == "user"
    ]
    assert remaining_users == ["当前任务"]


def test_context_exceeded_with_nothing_to_trim_breaks_cleanly(workspace: Path) -> None:
    """没有任何旧事件可裁时（历史里只有当前任务的 user）不再重试，正常收场。"""
    stub = _BoomOnceLLM()
    agent = Agent(llm=stub, workspace=workspace, mode="auto", max_steps=5)
    result = agent.run("只有这个任务")
    assert len(stub.calls) == 1  # 只调了一次，没有无限重发
    assert result.final_content is None
    assert result.steps == 0
