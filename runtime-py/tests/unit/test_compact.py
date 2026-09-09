"""compact 纯函数单元测试：token 估算 / 切段 / 压缩 prompt / 摘要调用。

行为参考 claude services/compact + tokenCountWithEstimation 的回退粗算口径
（接口自设计，非抄码）。
"""
from __future__ import annotations

import pytest

from emberpy import compact
from emberpy.testing import FakeLLM


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _tool(text: str) -> dict:
    return {"role": "tool", "content": text}


def _event(kind: str, message: dict) -> dict:
    return {"type": kind, "message": message}


class TestEstimateTokens:
    def test_nonempty_positive(self) -> None:
        assert compact.estimate_tokens("") == 0
        assert compact.estimate_tokens("你好") >= 1
        assert compact.estimate_tokens("a" * 900) >= 200  # ÷3 粗算

    def test_monotonic_with_length(self) -> None:
        small = compact.estimate_tokens("x" * 100)
        large = compact.estimate_tokens("x" * 1000)
        assert large > small


class TestEstimateMessagesTokens:
    def test_sums_content_and_tool_args(self) -> None:
        messages = [
            _user("用户消息" * 50),
            _assistant("助手消息" * 50),
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path": "a/b.py"}'}},
            ]},
            _tool("工具结果" * 100),
        ]
        total = compact.estimate_messages_tokens(messages)
        assert total > 0
        # 单独算：工具参数也该计入
        only_args = compact.estimate_messages_tokens([messages[2]])
        assert only_args >= 1

    def test_tool_result_weights_tighter_than_plain_text(self) -> None:
        # P3 分权：工具结果/JSON 按 ÷2 估，普通文本 ÷3 -> 等量字符工具结果估出更多 token
        text_only = compact.estimate_messages_tokens([_assistant("a" * 900)])
        tool_only = compact.estimate_messages_tokens([_tool("a" * 900)])
        assert tool_only > text_only

    def test_tool_arguments_counted_dense(self) -> None:
        message = {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "write_file", "arguments": '{"path": "/x/y.py", "content": "' + "b" * 600 + '"}'}},
        ]}
        # 参数 JSON ÷2；与同样长度的纯文本消息（÷3）相比应更大
        args_tokens = compact.estimate_messages_tokens([message])
        text_tokens = compact.estimate_messages_tokens([_assistant("b" * 600)])
        assert args_tokens > text_tokens


class TestSplit:
    def test_keeps_last_user_turn(self) -> None:
        events = [
            _event("user", {"role": "user", "content": "u1"}),
            _event("assistant", {"role": "assistant", "content": "a1"}),
            _event("user", {"role": "user", "content": "u2"}),
            _event("assistant", {"role": "assistant", "content": "a2"}),
        ]
        old, keep = compact.split_old_events(events)
        assert [e["message"]["content"] for e in old] == ["u1", "a1"]
        assert [e["message"]["content"] for e in keep] == ["u2", "a2"]

    def test_no_user_means_nothing_old(self) -> None:
        events = [_event("assistant", {"role": "assistant", "content": "a"})]
        old, keep = compact.split_old_events(events)
        assert old == []
        assert keep == events

    def test_old_segment_drops_dangling_tool_turn(self) -> None:
        # 旧段末尾是"有 tool_calls 没结果"的半截轮：喂给压紧模型前必须裁掉
        events = [
            _event("user", {"role": "user", "content": "u1"}),
            _event("assistant", {"role": "assistant", "content": "a1",
                                 "tool_calls": [{"id": "c1", "type": "function",
                                                 "function": {"name": "x", "arguments": "{}"}}]}),
            _event("user", {"role": "user", "content": "u2"}),  # 截断点后无 tool result
        ]
        old, _keep = compact.split_old_events(events)
        segment = compact.old_segment_messages(old)
        # u1 在；半截 assistant 的 tool_calls 轮被裁（否则 OpenAI 会 400）
        assert [m["role"] for m in segment] == ["user"]


class TestThreshold:
    def test_auto_compact_threshold_reserves_buffers(self) -> None:
        # 64k 窗 − 摘要输出预留 min(8192,20000) − 13k 缓冲 = 42808
        assert compact.auto_compact_threshold(64000) == 64000 - 8192 - 13000

    def test_should_auto_compact_against_threshold(self) -> None:
        messages = [_user("字" * 30_000)]  # ÷3 ≈ 10k tokens
        assert compact.should_auto_compact(messages, threshold=5000) is True
        assert compact.should_auto_compact(messages, threshold=20_000) is False
        assert compact.should_auto_compact([], threshold=0) is False


class TestPromptAndSummarize:
    def test_build_compact_messages_shape(self) -> None:
        segment = [_user("u1"), _assistant("a1")]
        msgs = compact.build_compact_messages(segment, extra_instruction="聚焦登录模块")
        assert msgs[0]["role"] == "system"
        assert "聚焦登录模块" in msgs[0]["content"]
        assert msgs[1:-1] == segment
        assert msgs[-1]["role"] == "user"
        assert "压缩摘要" in msgs[-1]["content"]

    def test_summarize_with_fake_llm(self) -> None:
        fake = FakeLLM([{"content": "摘要：用户想要登录功能。", "calls": None}])
        out = compact.summarize(fake, [_user("要做登录"), _assistant("好的")])
        assert out == "摘要：用户想要登录功能。"
        assert len(fake.complete_calls) == 1
        # 压紧调用不带工具
        assert fake.complete_calls[0][1] is None

    def test_summarize_raises_on_empty_output(self) -> None:
        fake = FakeLLM([{"content": "", "calls": None}])
        with pytest.raises(RuntimeError):
            compact.summarize(fake, [_user("x")])

    def test_summary_text_marks_removed(self) -> None:
        text = compact.summary_text("旧摘要", removed=4, tokens_before=1200)
        assert "4 条" in text and "已压缩" in text and "旧摘要" in text
