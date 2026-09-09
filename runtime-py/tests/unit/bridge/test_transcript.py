"""转录层（界面视角）的 thinking part 支持：形状与排序。

renderer 只认 {type:"thinking", thinking:string}，本层保证 transcript
产出的每条 assistant record 里 thinking 在前、text/toolCall 在后。
"""
from __future__ import annotations

from emberpy.bridge.transcript import Transcript, thinking_part


class TestThinkingPart:
    def test_shape(self) -> None:
        assert thinking_part("先想一下") == {"type": "thinking", "thinking": "先想一下"}

    def test_add_assistant_orders_thinking_first(self) -> None:
        t = Transcript()
        rec = t.add_assistant(
            "答案",
            [{"type": "toolCall", "name": "read_file", "arguments": {}}],
            thinking="先想",
        )
        parts = rec["content"]
        assert [p["type"] for p in parts] == ["thinking", "text", "toolCall"]
        assert parts[0] == {"type": "thinking", "thinking": "先想"}
        assert parts[1] == {"type": "text", "text": "答案"}

    def test_thinking_only_record(self) -> None:
        t = Transcript()
        rec = t.add_assistant("", [], thinking="想")
        assert rec["content"] == [{"type": "thinking", "thinking": "想"}]

    def test_backwards_compatible_without_thinking(self) -> None:
        t = Transcript()
        rec = t.add_assistant("答案", [])
        assert [p["type"] for p in rec["content"]] == ["text"]

    def test_blank_thinking_ignored(self) -> None:
        t = Transcript()
        rec = t.add_assistant("答案", [], thinking="   ")
        assert [p["type"] for p in rec["content"]] == ["text"]
