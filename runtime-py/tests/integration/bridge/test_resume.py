"""rpc worker 会话持久化 + resume/fork（离线，进程内两次 Worker 模拟重启）。

覆盖（M3 方案测试三例）：
① A worker 带 session_file 跑一轮写盘 -> B worker 同文件启动，转录与 A 一致、
   sessionFile 相同、历史 checkpoint 已 replay（before=全文）-> B 再跑一轮，消息含旧+新；
② 桌面场景（env 设 EMBER_SESSIONS_DIR）无 session 启动，首个 prompt 自动
   落盘到该目录、sessionFile 有值、文件首行是 v2 会话头；
③ fork 建独立字节级副本、原会话现场不变；无文件时返回 ok=False。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from emberpy.rpc import Worker, _Out
from emberpy.testing import FakeLLM, tool_call


# ---------------------------------------------------------------------------
# 辅助（与 test_worker.py 同款，保持独立不互相 import）
# ---------------------------------------------------------------------------


def make_worker(workspace: Path, script: list[dict[str, Any]], **kwargs) -> tuple[Worker, list[str]]:
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
    assert last_response(sink, rid)["success"] is True


def get_messages(worker: Worker, sink: list[str], rid: str) -> list[dict[str, Any]]:
    rpc_send(worker, "get_messages", rid)
    return last_response(sink, rid)["data"]["messages"]


def get_entries(worker: Worker, sink: list[str], rid: str) -> list[dict[str, Any]]:
    rpc_send(worker, "get_entries", rid)
    return last_response(sink, rid)["data"]["entries"]


def get_state(worker: Worker, sink: list[str], rid: str) -> dict[str, Any]:
    rpc_send(worker, "get_state", rid)
    return last_response(sink, rid)["data"]


def _drop_ephemeral(record: dict[str, Any]) -> dict[str, Any]:
    """去掉界面/进程每次生成都不同的 id、timestamp，便于 A/B 转录对等比较。"""
    return {k: v for k, v in record.items() if k not in ("id", "timestamp")}


# ---------------------------------------------------------------------------
# ① 跨 worker resume：历史完整 + checkpoint replay + 续写
# ---------------------------------------------------------------------------


def test_resume_same_session_file_replays_history(workspace: Path, tmp_path: Path) -> None:
    session_path = tmp_path / "sessions" / "s1.jsonl"
    script_a = [
        # 严格写前必读：先 read_file 旧文件，下一轮才 write_file
        {"content": "我先读一下 hello", "calls": [tool_call("c0", "read_file", {"path": "hello.txt"})]},
        {
            "content": "我先改 hello",
            "calls": [tool_call("c1", "write_file", {"path": "hello.txt", "content": "改成这样"})],
        },
        {"content": "改完了", "reasoning": "确认下结果", "calls": None},
    ]
    w_a, sink_a = make_worker(workspace, script_a, session_file=str(session_path))
    run_prompt(w_a, sink_a, "pA", "请改写 hello.txt")
    assert session_path.exists()

    msgs_a = get_messages(w_a, sink_a, "gA")
    assert [m["role"] for m in msgs_a] == [
        "user", "assistant", "toolResult", "assistant", "toolResult", "assistant",
    ]
    assert msgs_a[5]["content"][0]["type"] == "thinking"  # reasoner 推理展示

    # B worker 同 session 文件"重启"：不用跑任何 prompt，历史就该完整可见
    w_b, sink_b = make_worker(workspace, [], session_file=str(session_path))
    state = get_state(w_b, sink_b, "sB")
    assert state["sessionFile"] == str(session_path.resolve())
    msgs_b = get_messages(w_b, sink_b, "gB")
    assert [_drop_ephemeral(m) for m in msgs_b] == [_drop_ephemeral(m) for m in msgs_a]

    # 历史 checkpoint 已 replay：hello.txt 的改前全文可 /undo
    entries_b = get_entries(w_b, sink_b, "eB")
    checkpoints = [e for e in entries_b if e.get("customType") == "ember-checkpoint"]
    assert checkpoints, "resume 后应能 /undo 历史改动"
    hello_cp = next(c for c in checkpoints if c["data"]["before"][0]["path"] == "hello.txt")
    assert hello_cp["data"]["before"][0]["content"] == "你好，世界\n"  # 改前全文


def test_resume_then_continue_appends_new_history(workspace: Path, tmp_path: Path) -> None:
    session_path = tmp_path / "sessions" / "s2.jsonl"
    script_a = [{"content": "完成", "calls": None}]
    w_a, sink_a = make_worker(workspace, script_a, session_file=str(session_path))
    run_prompt(w_a, sink_a, "pA", "第一轮")
    msgs_a = get_messages(w_a, sink_a, "gA")

    # B 同文件续跑第二轮：消息 = 旧历史 + 新轮
    script_b = [
        # 新 worker 会话内无 read 记录：编辑 src/app.py 前先读
        {"content": None, "calls": [tool_call("c1", "read_file", {"path": "src/app.py"})]},
        {"content": None, "calls": [tool_call("c2", "file_edit", {"path": "src/app.py", "old_string": "def greet", "new_string": "def hello"})]},
        {"content": "第二轮也好了", "calls": None},
    ]
    w_b, sink_b = make_worker(workspace, script_b, session_file=str(session_path))
    run_prompt(w_b, sink_b, "pB", "继续，改 greet 函数")

    msgs_b = get_messages(w_b, sink_b, "gB")
    assert len(msgs_b) > len(msgs_a)
    texts = "".join(
        p.get("text", "") for m in msgs_b if m["role"] == "assistant" for p in m["content"] if p.get("type") == "text"
    )
    assert "完成" in texts and "第二轮也好了" in texts
    assert w_b.session_file == str(session_path.resolve())
    assert (workspace / "src" / "app.py").read_text(encoding="utf-8").startswith("def hello():")

    # resume 后的新写盘补丁也要能同步成 checkpoint（水位归零正确）
    entries = get_entries(w_b, sink_b, "eB")
    app_cp = next(
        (e for e in entries if e.get("customType") == "ember-checkpoint" and e["data"]["before"][0]["path"] == "src/app.py"),
        None,
    )
    assert app_cp is not None, "resume 后的写盘应继续产出 checkpoint"
    assert "def greet():" in app_cp["data"]["before"][0]["content"]

    # 文件仍是合法 v2：首行头、最后一行第二轮 user 消息
    rows = [l for l in session_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    first = json.loads(rows[0])
    assert first["type"] == "session" and first["cwd"] == str(workspace.resolve())
    assert json.loads(rows[-1])["type"] == "message"


# ---------------------------------------------------------------------------
# ② 桌面场景：无 --session，首个 prompt 自动落盘到会话目录
# ---------------------------------------------------------------------------


def test_desktop_auto_allocates_session_on_first_prompt(workspace: Path, tmp_path: Path, monkeypatch) -> None:
    sessions_dir = tmp_path / "desktop-sessions"
    monkeypatch.setenv("EMBER_SESSIONS_DIR", str(sessions_dir))

    w, sink = make_worker(workspace, [{"content": "直接答", "calls": None}])
    # 首个 prompt 前还没有文件（不污染侧栏）
    assert w.session_file is None
    run_prompt(w, sink, "p1", "你好")

    rpc_send(w, "get_session_stats", "s1")
    stats = last_response(sink, "s1")["data"]
    assert stats["sessionFile"], stats
    session_path = Path(stats["sessionFile"])
    assert session_path.parent == sessions_dir.resolve()
    assert session_path.exists()
    assert get_state(w, sink, "s2")["sessionFile"] == str(session_path)

    rows = [l for l in session_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    head = json.loads(rows[0])
    assert head["type"] == "session"
    assert head["cwd"] == str(workspace.resolve())
    # 用户消息已写进文件
    assert any(
        r.get("type") == "message" and r["message"]["role"] == "user" and r["message"]["content"] == "你好"
        for r in (json.loads(l) for l in rows)
    )


# ---------------------------------------------------------------------------
# ③ fork：字节级独立副本，原会话不动
# ---------------------------------------------------------------------------


def test_fork_copies_session_to_new_file(workspace: Path, tmp_path: Path, monkeypatch) -> None:
    sessions_dir = tmp_path / "desktop-sessions"
    monkeypatch.setenv("EMBER_SESSIONS_DIR", str(sessions_dir))
    original = tmp_path / "orig.jsonl"

    w, sink = make_worker(workspace, [{"content": "写好了", "calls": None}], session_file=str(original))
    run_prompt(w, sink, "p1", "任务")
    before_entries = get_entries(w, sink, "e1")

    rpc_send(w, "fork", "f1")
    resp = last_response(sink, "f1")
    assert resp["data"]["ok"] is True
    new_path = Path(resp["data"]["sessionFile"])
    assert new_path != original
    assert new_path.exists()
    assert new_path.parent == sessions_dir.resolve()
    # 字节级副本（同一现场 fork）
    assert new_path.read_bytes() == original.read_bytes()

    # 原会话现场没被切换
    assert w.session_file == str(original.resolve())
    assert get_entries(w, sink, "e2") == before_entries


def test_fork_without_file_returns_error(workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("EMBER_SESSIONS_DIR", str(Path("unused")))
    w, sink = make_worker(workspace, [])
    rpc_send(w, "fork", "f1")
    resp = last_response(sink, "f1")
    assert resp["data"]["ok"] is False  # 纯内存会话没有可 fork 的落盘文件
