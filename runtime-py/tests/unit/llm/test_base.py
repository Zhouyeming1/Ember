"""LLM 抽象层纯逻辑测试：tool_calls 解析、schema 生成（不发网络请求）。"""

import json

from emberpy.llm import parse_tool_calls
from emberpy.tools import Tool, ToolCategory, ToolRegistry


class TestParseToolCalls:
    def test_valid_arguments(self) -> None:
        raw = {
            "role": "assistant",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}
            ],
        }
        calls = parse_tool_calls(raw)
        assert len(calls) == 1
        assert calls[0].name == "read_file"
        assert calls[0].arguments == {"path": "a.txt"}
        assert calls[0].parse_error is None

    def test_invalid_json_sets_parse_error(self) -> None:
        raw = {
            "role": "assistant",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": "{not json"}}
            ],
        }
        calls = parse_tool_calls(raw)
        assert len(calls) == 1
        assert calls[0].parse_error is not None
        assert "不是合法 JSON" in calls[0].parse_error

    def test_empty(self) -> None:
        assert parse_tool_calls({}) == []
        assert parse_tool_calls({"tool_calls": None}) == []


class TestSchema:
    def test_openai_schema_shape(self) -> None:
        tool = Tool(
            name="read_file",
            description="读取文件",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            category=ToolCategory.READ,
            fn=lambda **_: "x",
        )
        reg = ToolRegistry([tool])
        schema = reg.schemas()[0]
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] == "read_file"
        assert fn["parameters"]["required"] == ["path"]
        assert json.loads(json.dumps(schema))  # 可 JSON 序列化（发给模型的前提）
