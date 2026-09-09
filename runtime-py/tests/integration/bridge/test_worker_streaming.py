"""worker 层流式链路（message_start -> message_update -> message_end）离线测试。

用 StreamingLLM（带 stream_complete 的假模型）驱动 worker，验证：
① 流式文本：第一个可见增量发 message_start，后续增量发 message_update（累计全文
   + thinking part），最终 assistant 事件补一条累计 message_update，收尾 message_end
   带权威最终文本；文本单调增长、结尾为最终文本；
② 对照：普通 FakeLLM（无 stream_complete）仍走整包 message_start、绝无 message_update，
   证明非流式路径零改动；
③ 流式文本 + 工具轮 + 再来一段流式文本（reasoner 常见形态）：窗口累计两段、
   message_end 带两段 join，tool_execution_start 事件照发。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from emberpy.rpc import Worker, _Out
from emberpy.testing import FakeLLM, StreamingLLM, tool_call


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def make_worker(workspace: Path, script: list[dict[str, Any]], **kwargs) -> tuple[Worker, list[str]]:
    sink: list[str] = []
    worker = Worker(workspace=workspace, llm=StreamingLLM(script), out=_Out(sink.append), **kwargs)
    return worker, sink


def send(worker: Worker, method: str, rid: str, **params: Any) -> None:
    worker.handle_line(json.dumps({"type": method, "id": rid, **params}, ensure_ascii=False))


def wait_run_done(worker: Worker, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        thread = worker._run_thread
        if thread is None or not thread.is_alive():
            return
        time.sleep(0.02)
    raise AssertionError("运行线程在超时内没结束")


def run_prompt(worker: Worker, sink: list[str], message: str) -> None:
    send(worker, "prompt", "p1", message=message)
    wait_run_done(worker)
    for line in reversed(sink):
        obj = json.loads(line)
        if obj.get("type") == "response" and obj.get("id") == "p1":
            assert obj.get("success") is True
            return
    raise AssertionError("没有 prompt 响应。")


def assistant_stream_events(sink: list[str]) -> list[tuple[str, str]]:
    """按事件顺序取出 (事件类型, 累计 text) —— 只挑 assistant 的 text part 拼接。"""
    out: list[tuple[str, str]] = []
    for line in sink:
        obj = json.loads(line)
        if obj.get("type") not in ("message_start", "message_update", "message_end"):
            continue
        message = obj.get("message") or {}
        if message.get("role") != "assistant":
            continue
        content = message.get("content") or []
        if isinstance(content, str):
            texts = [content]
        else:
            texts = [part["text"] for part in content if isinstance(part, dict) and part.get("type") == "text" and part.get("text")]
        out.append((obj["type"], "\n".join(texts)))
    return out


def assistant_thinking_parts(sink: list[str]) -> list[str]:
    """所有 assistant 事件里 thinking part 的并集（去重后的最终值应是完整推理）。"""
    seen: list[str] = []
    for line in sink:
        obj = json.loads(line)
        message = obj.get("message") or {}
        if message.get("role") != "assistant":
            continue
        content = message.get("content") or []
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "thinking" and part.get("thinking"):
                    text = str(part["thinking"])
                    if text not in seen:
                        seen.append(text)
    return seen


# ---------------------------------------------------------------------------
# ① 流式文本：text 单调增长、message_start -> message_update -> message_end
# ---------------------------------------------------------------------------


def test_streamed_text_uses_message_update(tmp_path: Path) -> None:
    script = [{
        "content": "Hello world",
        "reasoning": "先想一下",
        "deltas": [
            {"text": "H", "thinking": "先"},
            {"text": "Hello", "thinking": "先想"},
            {"text": "Hello world", "thinking": "先想一下"},
        ],
    }]
    worker, sink = make_worker(tmp_path, script)
    run_prompt(worker, sink, "打招呼")

    events = assistant_stream_events(sink)
    kinds = [kind for kind, _ in events]
    # 开头是 message_start、中间 message_update、结尾 message_end
    assert kinds[0] == "message_start"
    assert "message_update" in kinds
    assert kinds[-1] == "message_end"

    texts = [text for _, text in events]
    # 文本单调增长，绝不回缩；message_end 带权威最终文本
    assert texts == texts and all(texts[i] == texts[i - 1] or texts[i].startswith(texts[i - 1]) for i in range(1, len(texts)))
    assert events[-1][1] == "Hello world"
    assert texts[-2] == "Hello world"  # 倒数第二是最终 assistant 事件补的累计 message_update

    # thinking part 最终收敛到完整推理（renderer joinThinking 幂等靠它）
    think = assistant_thinking_parts(sink)
    assert think and think[-1] == "先想一下"

    # 转录只有一条 assistant，文本与推理都齐
    messages = worker.transcript.get_messages()
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 1
    text = ""
    for part in assistant_msgs[0].get("content", []) if isinstance(assistant_msgs[0].get("content"), list) else []:
        if part.get("type") == "text":
            text += part.get("text", "")
    assert text == "Hello world"
    assert worker._window_texts == []  # 收尾后窗口干净


# ---------------------------------------------------------------------------
# ② 非流式对照：普通 FakeLLM 整包 message_start、没有 message_update
# ---------------------------------------------------------------------------


def test_non_streaming_fake_llm_has_no_message_update(tmp_path: Path) -> None:
    sink: list[str] = []
    worker = Worker(workspace=tmp_path, llm=FakeLLM([{"content": "整包回复"}]), out=_Out(sink.append))
    run_prompt(worker, sink, "hi")

    events = assistant_stream_events(sink)
    assert events == [("message_start", "整包回复"), ("message_end", "整包回复")]
    for line in sink:
        assert json.loads(line).get("type") != "message_update"  # 整包路径绝不 update


# ---------------------------------------------------------------------------
# ③ 流式文本 + 工具轮 + 再一段流式文本（reasoner 多轮形态）
# ---------------------------------------------------------------------------


def test_streamed_multi_turn_with_tool(tmp_path: Path) -> None:
    script = [
        {
            "content": "先读文件",
            "calls": [tool_call("t1", "read_file", {"path": "missing.txt"})],
            "deltas": [
                {"text": "先", "thinking": "想"},
                {"text": "先读文件", "thinking": "想"},
            ],
        },
        {
            "content": "结束",
            "deltas": [{"text": "结"}, {"text": "结束"}],
        },
    ]
    worker, sink = make_worker(tmp_path, script)
    run_prompt(worker, sink, "读文件并总结")

    events = assistant_stream_events(sink)
    texts = [text for _, text in events]
    # 两段文本都到齐且收尾带两段 join（窗口语义与非流式 append 一致）
    assert "先读文件" in texts
    assert events[-1][1] == "先读文件\n结束"
    # 中间确实跑过工具轮
    tool_starts = [json.loads(l) for l in sink if json.loads(l).get("type") == "tool_execution_start"]
    assert len(tool_starts) == 1
    assert tool_starts[0]["toolName"] == "read_file"
    assert tool_starts[0]["toolCallId"] == "t1"
