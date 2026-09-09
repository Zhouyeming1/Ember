"""rpc worker（JSON-RPC 桥）离线测试。

用 FakeLLM 脚本驱动 Worker，验证发给桌面前端的协议是界面能吃的形状：
命令响应、message_*/tool_execution_*/agent_settled 事件、转录 get_messages /
get_entries（含 /undo checkpoint）、ask 确认往返、steer / abort / new_session。

Worker 从 emberpy.rpc 导入——薄壳与真实入口同源，正好验证进程契约没断。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

from emberpy.rpc import Worker, _Out
from emberpy.testing import FakeLLM, tool_call


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def make_worker(workspace: Path, script: list[dict[str, Any]], **kwargs) -> tuple[Worker, list[str]]:
    """造一个把输出写进内存 list 的 worker（便于逐行断言协议）。"""
    sink: list[str] = []
    worker = Worker(workspace=workspace, llm=FakeLLM(script), out=_Out(sink.append), **kwargs)
    return worker, sink


def rpc_send(worker: Worker, method: str, rid: str, **params: Any) -> str:
    worker.handle_line(json.dumps({"type": method, "id": rid, **params}, ensure_ascii=False))
    return rid


def last_response(sink: list[str], rid: str) -> dict[str, Any]:
    for line in reversed(sink):
        obj = json.loads(line)
        if obj.get("type") == "response" and obj.get("id") == rid:
            return obj
    raise AssertionError(f"没有 id={rid} 的响应。\n输出：\n" + "\n".join(sink))


def events(sink: list[str]) -> list[dict[str, Any]]:
    """所有非 response 行 = 发给界面的事件。"""
    return [json.loads(line) for line in sink if json.loads(line).get("type") != "response"]


def event_types(sink: list[str]) -> list[str]:
    return [obj["type"] for obj in events(sink)]


def wait_run_done(worker: Worker, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        thread = worker._run_thread
        if thread is None or not thread.is_alive():
            return
        time.sleep(0.02)
    thread = worker._run_thread
    if thread is not None:
        thread.join(timeout=2.0)
        raise AssertionError("运行线程在超时内没结束")


def run_prompt(worker: Worker, sink: list[str], rid: str, message: str) -> None:
    rpc_send(worker, "prompt", rid, message=message)
    wait_run_done(worker)


# ---------------------------------------------------------------------------
# 快照 / 查询命令
# ---------------------------------------------------------------------------


def test_snapshot_commands_respond(workspace: Path) -> None:
    w, sink = make_worker(workspace, [])
    pairs = [
        ("get_state", "r1"),
        ("get_messages", "r2"),
        ("get_entries", "r3"),
        ("get_session_stats", "r4"),
        ("get_available_models", "r5"),
        ("get_available_thinking_levels", "r6"),
        ("get_commands", "r7"),
        ("get_fork_messages", "r8"),
    ]
    for method, rid in pairs:
        rpc_send(w, method, rid)
        resp = last_response(sink, rid)
        assert resp["success"] is True, resp
        assert resp["data"] is not None

    state = last_response(sink, "r1")["data"]
    assert state["isStreaming"] is False
    assert state["mode"] == "auto"
    assert last_response(sink, "r2")["data"]["messages"] == []
    assert last_response(sink, "r4")["data"]["totalMessages"] == 0
    # 查询命令不应漏发事件
    assert event_types(sink) == []


def test_new_session_resets_transcript(workspace: Path) -> None:
    w, sink = make_worker(workspace, [{"content": "只答一句", "calls": None}])
    run_prompt(w, sink, "p1", "你好")
    assert last_response(sink, "p1")["success"] is True

    rpc_send(w, "get_session_stats", "s1")
    assert last_response(sink, "s1")["data"]["totalMessages"] == 2  # user + assistant

    rpc_send(w, "new_session", "n1")
    assert last_response(sink, "n1")["data"]["ok"] is True
    rpc_send(w, "get_session_stats", "s2")
    assert last_response(sink, "s2")["data"]["totalMessages"] == 0
    rpc_send(w, "get_messages", "s3")
    assert last_response(sink, "s3")["data"]["messages"] == []


# ---------------------------------------------------------------------------
# 一轮"写文件"的完整事件流 + 转录 / checkpoint
# ---------------------------------------------------------------------------


def test_prompt_write_round_events_and_transcript(workspace: Path) -> None:
    script = [
        {
            "content": "我先建个笔记文件",
            "calls": [tool_call("c1", "write_file", {"path": "notes.txt", "content": "你好 emberpy"})],
        },
        {"content": "完成", "calls": None},
    ]
    w, sink = make_worker(workspace, script)
    run_prompt(w, sink, "p1", "请写文件")
    assert last_response(sink, "p1")["success"] is True

    # 事件流：user 回显在前，助手文本/工具交替，最后 agent_settled
    types = event_types(sink)
    assert types[0] == "message_start", types
    assert types[-1] == "agent_settled", types
    assert "tool_execution_start" in types
    assert "tool_execution_end" in types
    assert "message_end" in types

    evts = events(sink)
    user_evt = next(e for e in evts if e["type"] == "message_start" and e["message"]["role"] == "user")
    assert user_evt["message"]["content"][0]["text"] == "请写文件"

    tool_start = next(e for e in evts if e["type"] == "tool_execution_start")
    assert tool_start["toolName"] == "write_file"
    assert tool_start["toolCallId"] == "c1"
    assert tool_start["args"]["path"] == "notes.txt"

    tool_end = next(e for e in evts if e["type"] == "tool_execution_end")
    assert tool_end["toolCallId"] == "c1"
    assert tool_end["isError"] is False
    assert tool_end["result"].startswith("已写入")

    # 助手最终文本累计到 message_end
    final = next(e for e in reversed(evts) if e["type"] == "message_end")
    assert "完成" in final["message"]["content"][0]["text"]

    # 文件真实写盘
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "你好 emberpy"

    # get_messages：user -> assistant(文本+toolCall) -> toolResult -> assistant(收尾)
    rpc_send(w, "get_messages", "g1")
    msgs = last_response(sink, "g1")["data"]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "toolResult", "assistant"]
    r1 = msgs[1]
    assert [p["type"] for p in r1["content"]] == ["text", "toolCall"]
    assert r1["content"][0]["text"] == "我先建个笔记文件"
    assert r1["content"][1]["name"] == "write_file"
    assert r1["content"][1]["id"] == "c1"
    assert msgs[2]["toolCallId"] == "c1"
    assert msgs[2]["isError"] is False
    assert msgs[3]["content"][0]["text"] == "完成"

    # get_entries：含 ember-checkpoint（/undo 用），路径是工作区相对路径
    rpc_send(w, "get_entries", "g2")
    entries = last_response(sink, "g2")["data"]["entries"]
    checkpoint = next(e for e in entries if e.get("customType") == "ember-checkpoint")
    assert checkpoint["data"]["before"][0]["path"] == "notes.txt"
    assert checkpoint["data"]["before"][0]["content"] is None  # 新文件：改前为空

    # stats
    rpc_send(w, "get_session_stats", "g3")
    stats = last_response(sink, "g3")["data"]
    assert stats["userMessages"] == 1
    assert stats["assistantMessages"] == 2
    assert stats["toolCalls"] == 1
    assert stats["toolResults"] == 1


def test_file_edit_round_events_and_checkpoint(workspace: Path) -> None:
    """file_edit：精确替换一次工具调用，事件/转录/checkpoint 都正确。"""
    script = [
        # 严格写前必读：编辑已存在文件前先 read_file
        {"content": None, "calls": [tool_call("r1", "read_file", {"path": "src/app.py"})]},
        {
            "content": "我把函数改名",
            "calls": [tool_call("e1", "file_edit", {"path": "src/app.py", "old_string": "def greet", "new_string": "def hello"})],
        },
        {"content": "改好了", "calls": None},
    ]
    w, sink = make_worker(workspace, script)
    run_prompt(w, sink, "p1", "把 greet 函数改名成 hello")

    assert last_response(sink, "p1")["success"] is True
    text = (workspace / "src" / "app.py").read_text(encoding="utf-8")
    assert "def hello():" in text
    assert "def greet():" not in text

    # 事件：file_edit 一次工具执行，非错误（read 那轮的事件在它之前）
    evts = events(sink)
    tool_start = next(e for e in evts if e["type"] == "tool_execution_start" and e["toolName"] == "file_edit")
    assert tool_start["toolCallId"] == "e1"
    assert tool_start["args"]["old_string"] == "def greet"
    tool_end = next(e for e in evts if e["type"] == "tool_execution_end" and e["toolName"] == "file_edit")
    assert tool_end["toolCallId"] == "e1"
    assert tool_end["isError"] is False

    # checkpoint：before 记录的是改前整文件原文（/undo 可整体还原）
    rpc_send(w, "get_entries", "g1")
    entries = last_response(sink, "g1")["data"]["entries"]
    checkpoint = next(e for e in entries if e.get("customType") == "ember-checkpoint")
    before_entry = checkpoint["data"]["before"][0]
    assert before_entry["path"] == "src/app.py"
    assert isinstance(before_entry["content"], str)
    assert "def greet():" in before_entry["content"]

    # 转录：助手消息含 file_edit 的 toolCall part
    rpc_send(w, "get_messages", "g2")
    msgs = last_response(sink, "g2")["data"]["messages"]
    assert any(
        p.get("type") == "toolCall" and p.get("name") == "file_edit"
        for m in msgs
        if m["role"] == "assistant"
        for p in m["content"]
    )


# ---------------------------------------------------------------------------
# ask 模式：权限确认对话框往返
# ---------------------------------------------------------------------------


def test_ask_mode_ui_confirm_roundtrip(workspace: Path) -> None:
    script = [
        {"content": None, "calls": [tool_call("c1", "write_file", {"path": "ok.txt", "content": "x"})]},
        {"content": "已写入", "calls": None},
    ]
    w, sink = make_worker(workspace, script, permission="ask", sandbox="workspace-write")

    rpc_send(w, "prompt", "p1", message="写个文件")

    # 等 extension_ui_request 出现，模拟界面点"允许"
    rid_seen: Optional[str] = None
    deadline = time.time() + 5
    while time.time() < deadline and rid_seen is None:
        for evt in events(sink):
            if evt.get("type") == "extension_ui_request" and evt.get("method") == "confirm":
                rid_seen = evt["id"]
                break
        if rid_seen is None:
            time.sleep(0.02)
    assert rid_seen is not None, "ask 模式应发出确认请求"

    # 主线程代表界面回执
    rpc_send(w, "extension_ui_response", rid_seen, confirmed=True)
    wait_run_done(w)

    assert last_response(sink, "p1")["success"] is True
    assert (workspace / "ok.txt").exists()
    tool_end = next(e for e in events(sink) if e["type"] == "tool_execution_end")
    assert tool_end["isError"] is False


# ---------------------------------------------------------------------------
# 运行中 steer 插话
# ---------------------------------------------------------------------------


class _GatedLLM(FakeLLM):
    """第 wait_at 次 complete 前卡住，直到主线程 gate.set() 放行。"""

    def __init__(self, script: list[dict[str, Any]], wait_at: int) -> None:
        super().__init__(script)
        self.wait_at = wait_at
        self.calls = 0
        self.gate = threading.Event()

    def complete(self, messages, tools=None):
        self.calls += 1
        if self.calls >= self.wait_at:
            self.gate.wait(timeout=10)
        return super().complete(messages, tools=tools)


def test_steer_injected_as_user_turn(workspace: Path) -> None:
    script = [
        # 第一轮只调工具（读文件）
        {"content": None, "calls": [tool_call("c1", "read_file", {"path": "hello.txt"})]},
        # steer 在第一轮工具之后、第二轮模型调用前被注入成 user 消息
        {"content": "按你的新指示处理", "calls": None},
    ]
    sink: list[str] = []
    llm = _GatedLLM(script, wait_at=1)  # 卡在第一轮模型调用，给主线程发 steer 的时间
    w = Worker(workspace=workspace, llm=llm, out=_Out(sink.append))

    rpc_send(w, "prompt", "p1", message="先读文件")
    deadline = time.time() + 5
    while time.time() < deadline:
        if llm.calls >= 1:  # 已进入第一轮 complete（卡点），运行线程尚未执行任何工具
            break
        time.sleep(0.02)
    rpc_send(w, "steer", "p2", message="新指示：用一句话总结")
    assert last_response(sink, "p2")["success"] is True
    llm.gate.set()
    wait_run_done(w)

    assert last_response(sink, "p1")["success"] is True

    user_texts = [
        e["message"]["content"][0]["text"]
        for e in events(sink)
        if e.get("type") == "message_start" and e.get("message", {}).get("role") == "user"
    ]
    assert user_texts == ["先读文件", "新指示：用一句话总结"], user_texts

    # 转录里有两个 user（主 prompt + steer）
    rpc_send(w, "get_session_stats", "g1")
    assert last_response(sink, "g1")["data"]["userMessages"] == 2


# ---------------------------------------------------------------------------
# abort：外部停止在步间生效，事件流正常收尾
# ---------------------------------------------------------------------------


def test_abort_stops_between_rounds(workspace: Path) -> None:
    script = [
        {"content": None, "calls": [tool_call("c1", "read_file", {"path": "hello.txt"})]},
        {"content": None, "calls": [tool_call("c2", "read_file", {"path": "hello.txt"})]},
        {"content": "这轮不该到", "calls": None},
    ]
    sink: list[str] = []
    llm = _GatedLLM(script, wait_at=2)  # 第二轮模型调用前卡住
    w = Worker(workspace=workspace, llm=llm, out=_Out(sink.append))

    rpc_send(w, "prompt", "p1", message="跑两轮工具")
    # 等运行线程进入第二轮 complete（卡点），此时第一轮工具已执行完
    deadline = time.time() + 5
    while time.time() < deadline:
        if w._agent is not None and llm.calls >= 2:
            break
        time.sleep(0.02)
    rpc_send(w, "abort", "a1")
    assert last_response(sink, "a1")["data"]["ok"] is True
    llm.gate.set()  # 放行第二轮，随后引擎在第三轮步间看到 stop 而中断
    wait_run_done(w)

    assert last_response(sink, "p1")["success"] is True
    types = event_types(sink)
    assert types[-1] == "agent_settled", types
    # abort 后第三轮最终文本不该出现
    assert "这轮不该到" not in "\n".join(sink)


# ---------------------------------------------------------------------------
# 思考过程（thinking）渲染：reasoner 的推理文本 → thinking part
# ---------------------------------------------------------------------------


def test_thinking_rendered_as_part_and_message_end(workspace: Path) -> None:
    script = [{"content": "完成", "reasoning": "先想一下怎么做", "calls": None}]
    w, sink = make_worker(workspace, script)
    run_prompt(w, sink, "p1", "帮我看下")

    assert last_response(sink, "p1")["success"] is True
    evts = events(sink)
    asst_starts = [e for e in evts if e["type"] == "message_start" and e["message"]["role"] == "assistant"]
    assert asst_starts, "应有助手 message_start"
    content = asst_starts[0]["message"]["content"]
    # thinking part 形状与排序：{type:"thinking",thinking} 在 text 之前
    assert content[0] == {"type": "thinking", "thinking": "先想一下怎么做"}, content
    assert [p["text"] for p in content if p.get("type") == "text"] == ["完成"]

    # message_end 必须带最终 text（UI 靠它停止流式动画）
    ends = [e for e in evts if e["type"] == "message_end"]
    assert ends, "应有 message_end"
    end_parts = ends[-1]["message"]["content"]
    assert any(p.get("type") == "text" and p.get("text") == "完成" for p in end_parts)

    # 转录同样保留 thinking part（历史重载可见思考面板）
    rpc_send(w, "get_messages", "g1")
    msgs = last_response(sink, "g1")["data"]["messages"]
    asst = [m for m in msgs if m["role"] == "assistant"][-1]
    assert asst["content"][0] == {"type": "thinking", "thinking": "先想一下怎么做"}


def test_thinking_level_switches_model(workspace: Path) -> None:
    """等级=模型开关：off→deepseek-chat，其余→deepseek-reasoner，二者始终一致。"""
    w, sink = make_worker(workspace, [])

    def state() -> dict[str, Any]:
        rpc_send(w, "get_state", "q")
        return last_response(sink, "q")["data"]

    # 等级还没同步：跟随默认模型选择
    assert state()["model"] == "deepseek-chat"

    rpc_send(w, "set_thinking_level", "l1", level="off")
    d = state()
    assert d["model"] == "deepseek-chat"
    assert d["thinkingLevel"] == "off"

    rpc_send(w, "set_thinking_level", "l2", level="high")
    d = state()
    assert d["model"] == "deepseek-reasoner"
    assert d["thinkingLevel"] == "high"

    # 显式切模型会回写等级，两个控件不打架
    rpc_send(w, "set_model", "m1", modelId="deepseek-chat")
    d = state()
    assert d["model"] == "deepseek-chat" and d["thinkingLevel"] == "off"
    rpc_send(w, "set_model", "m2", modelId="deepseek-reasoner")
    d = state()
    assert d["model"] == "deepseek-reasoner" and d["thinkingLevel"] == "high"
