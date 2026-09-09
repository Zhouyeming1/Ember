"""推理文本（reasoning_content）进出模型的清洗逻辑。

回发规则（DeepSeek reasoner 规范）：推理文本只给人看，绝不能原样喂回模型；
聊天模型没有该字段。without_reasoning 负责在"进上下文"前剥离。
"""
from __future__ import annotations

from emberpy.llm import AssistantReply, without_reasoning
from emberpy.testing import FakeLLM


class TestWithoutReasoning:
    def test_passthrough_when_no_reasoning(self) -> None:
        msg = {"role": "assistant", "content": "好的"}
        assert without_reasoning(msg) is msg  # 没有推理字段就不新建拷贝

    def test_strips_copy_keeps_original(self) -> None:
        msg = {"role": "assistant", "content": "好的", "reasoning_content": "内部思考"}
        cleaned = without_reasoning(msg)
        assert cleaned == {"role": "assistant", "content": "好的"}
        assert "reasoning_content" not in cleaned
        # 原 dict 不动：落盘/展示仍保留推理文本
        assert msg["reasoning_content"] == "内部思考"

    def test_non_dict_passthrough(self) -> None:
        assert without_reasoning("not a dict") == "not a dict"


class TestFakeLLMReasoning:
    def test_reasoning_reaches_reply_and_raw(self) -> None:
        llm = FakeLLM([{"content": "完成", "reasoning": "先想一下", "calls": None}])
        reply: AssistantReply = llm.complete([])
        assert reply.thinking == "先想一下"
        assert reply.raw_message["reasoning_content"] == "先想一下"

    def test_no_reasoning_key_gives_none(self) -> None:
        llm = FakeLLM([{"content": "完成", "calls": None}])
        reply: AssistantReply = llm.complete([])
        assert reply.thinking is None
        assert "reasoning_content" not in reply.raw_message

    def test_blank_reasoning_gives_none(self) -> None:
        llm = FakeLLM([{"content": "完成", "reasoning": "  ", "calls": None}])
        reply: AssistantReply = llm.complete([])
        assert reply.thinking is None
        assert "reasoning_content" not in reply.raw_message
