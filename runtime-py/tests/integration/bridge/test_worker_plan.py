"""worker 计划模式闭环 + 确认交互 离线测试。

场景：
① /permissions plan：纯状态命令——不建 agent、不落盘会话、假模型不被消费，
   模式切到 plan 且记住批准前模式；
② plan 轮：用户描述任务，模型只读调研后用 update_plan 列结构化计划并停下；
   工具参数带 plan 数组（UI 据此渲染待办/批准区），权限仍是 plan；
③ /plan execute（InspectPanel「批准」按钮）：退出 plan、恢复批准前模式，
   喂给 agent 的执行任务开启真正干活（此时写权限已放行）；
④ AskUserQuestion：模型问单选澄清 -> worker 发 method=select 的
   extension_ui_request 卡片，测试替身点选回执，选项回给模型接着干。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from emberpy.permission import PermissionMode
from emberpy.rpc import Worker, _Out
from emberpy.testing import FakeLLM, tool_call


def make_worker(workspace: Path, script: list[dict[str, Any]], **kwargs) -> tuple[Worker, list[str]]:
    sink: list[str] = []
    worker = Worker(
        workspace=workspace,
        llm=FakeLLM(script),
        out=_Out(sink.append),
        **kwargs,
    )
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


def wait_response(sink: list[str], rid: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for line in sink:
            obj = json.loads(line)
            if obj.get("type") == "response" and obj.get("id") == rid:
                assert obj.get("success") is True, obj.get("error")
                return obj
        time.sleep(0.02)
    raise AssertionError(f"没有 {rid} 的 prompt 响应。")


def find_events(sink: list[str], event_type: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in sink if json.loads(line).get("type") == event_type]


def assistant_texts(sink: list[str]) -> list[str]:
    out: list[str] = []
    for line in sink:
        obj = json.loads(line)
        message = obj.get("message") or {}
        if message.get("role") != "assistant":
            continue
        content = message.get("content") or []
        if isinstance(content, str):
            out.append(content)
            continue
        parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        out.append("\n".join(parts))
    return out


def run_prompt(worker: Worker, sink: list[str], message: str, rid: str) -> None:
    send(worker, "prompt", rid, message=message)
    wait_run_done(worker)
    wait_response(sink, rid)


def tool_calls_in_transcript(worker: Worker) -> list[dict[str, Any]]:
    """从转录的助手消息里抽出 toolCall 内容块。"""
    found: list[dict[str, Any]] = []
    for message in worker.transcript.get_messages():
        if message.get("role") != "assistant":
            continue
        content = message.get("content") or []
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "toolCall":
                found.append(part)
    return found


# ---------------------------------------------------------------------------
# ① /permissions plan：纯状态命令
# ---------------------------------------------------------------------------


def test_permissions_command_switches_mode_without_agent(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    worker, sink = make_worker(workspace, [{"content": "不该被消费"}])
    assert worker.mode is PermissionMode.AUTO

    run_prompt(worker, sink, "/permissions plan", "p1")

    # 没建 agent / 会话（命令是纯状态变更），假模型原封未动
    assert worker._agent is None
    assert worker.session is None
    assert len(worker.injected_llm._script) == 1
    assert worker.mode is PermissionMode.PLAN
    assert worker._mode_before_plan is PermissionMode.AUTO
    texts = assistant_texts(sink)
    assert any("已切换为 plan" in t for t in texts)

    # get_state 回报新模式（UI 下拉/状态同步用）
    fresh: list[str] = []
    worker.out = _Out(fresh.append)
    send(worker, "get_state", "s1")
    for line in fresh:
        obj = json.loads(line)
        if obj.get("type") == "response" and obj.get("id") == "s1":
            assert obj["data"]["mode"] == "plan"
            return
    raise AssertionError("没有 get_state 响应")


# ---------------------------------------------------------------------------
# ② + ③ plan 轮 -> update_plan 列计划 -> /plan execute 批准开跑
# ---------------------------------------------------------------------------


def test_plan_cycle_update_plan_then_execute(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    script = [
        {"content": None, "calls": [tool_call("t1", "update_plan", {
            "plan": [
                {"step": "读现有配置", "status": "pending"},
                {"step": "改核心逻辑", "status": "pending"},
            ],
        })]},
        {"content": "计划已列好，等你批准。", "calls": None},
        {"content": "好的，开始执行。", "calls": None},
    ]
    worker, sink = make_worker(workspace, script)

    # 进计划模式
    run_prompt(worker, sink, "/permissions plan", "p0")
    assert worker.mode is PermissionMode.PLAN

    # 用户在计划模式下达任务：模型列结构化计划后停下（不起写）
    run_prompt(worker, sink, "给项目加个登录功能", "p1")

    starts = find_events(sink, "tool_execution_start")
    plan_call = next(e for e in starts if e.get("toolName") == "update_plan")
    assert plan_call["args"]["plan"][0]["step"] == "读现有配置"
    assert plan_call["args"]["plan"][0]["status"] == "pending"
    ends = find_events(sink, "tool_execution_end")
    plan_end = next(e for e in ends if e.get("toolName") == "update_plan")
    assert plan_end.get("isError") is False
    # 助手消息里带着 update_plan 的 toolCall 块（渲染端 collectTodos 从这里取待办）
    assert any(p.get("name") == "update_plan" for p in tool_calls_in_transcript(worker))
    # 计划模式仍然生效
    assert worker.mode is PermissionMode.PLAN

    # /plan execute = 用户批准：退出计划模式恢复 auto，喂执行任务让模型真干
    run_prompt(worker, sink, "/plan execute", "p2")
    assert worker.mode is PermissionMode.AUTO
    assert worker._mode_before_plan is None
    assert worker._agent is not None and worker._agent.gate.mode is PermissionMode.AUTO
    # 假模型三处答复全被消费（plan 轮 2 次 + execute 轮 1 次）
    assert len(worker.injected_llm._script) == 0
    texts = assistant_texts(sink)
    assert any("好的，开始执行" in t for t in texts)


# ---------------------------------------------------------------------------
# ④ AskUserQuestion：select 卡片往返
# ---------------------------------------------------------------------------


def test_ask_user_question_select_roundtrip(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    script = [
        {"content": None, "calls": [tool_call(
            "q1", "AskUserQuestion",
            {"question": "要不要做数据迁移？", "options": ["做", "不做"]},
        )]},
        {"content": "已按你的选择继续。", "calls": None},
    ]
    worker, sink = make_worker(workspace, script)

    send(worker, "prompt", "p1", message="帮我确认下需求")

    # 等 worker 发 select 卡片
    deadline = time.time() + 10.0
    rid = None
    while time.time() < deadline:
        for line in sink:
            obj = json.loads(line)
            if obj.get("type") == "extension_ui_request" and obj.get("method") == "select":
                rid = obj.get("id")
                assert obj["title"] == "要不要做数据迁移？"
                assert obj["options"] == ["做", "不做"]
                break
        if rid:
            break
        time.sleep(0.02)
    assert rid is not None, "没有收到 select 的 extension_ui_request"

    # 模拟用户点"做"
    send(worker, "extension_ui_response", rid, value="做")
    wait_run_done(worker)
    wait_response(sink, "p1")

    ends = find_events(sink, "tool_execution_end")
    ask_end = next(e for e in ends if e.get("toolName") == "AskUserQuestion")
    assert ask_end.get("isError") is False
    assert "选择是：做" in ask_end["result"]
    texts = assistant_texts(sink)
    assert any("已按你的选择继续" in t for t in texts)
