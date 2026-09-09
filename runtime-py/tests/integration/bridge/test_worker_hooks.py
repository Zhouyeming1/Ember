"""worker 侧 hooks 链路离线测试（真实 subprocess hook，读临时 home 的 hooks.json）。

persist 门控：Worker 只有在桌面场景（EMBER_HOME 等 env）才装配 hooks——测试设
EMBER_HOME 到临时 home，并把 user 级 hooks.json 写进 <home>/.ember/hooks.json，
验证四条链路：
① SessionStart/PreCompact/PostCompact 生命周期 hook 在对应时机各触发一次（hook
   往工作区写 marker，按 HOOK_EVENT 计数）——压缩场景照搬 test_worker_compact 的
   触发推导（window=21392 -> threshold 200，第三轮起手自动压缩）；
② UserPromptSubmit 阻塞：本轮不跑 agent（假模型脚本未消耗）、回拦截文本给用户；
③ UserPromptSubmit 改写：agent 拿改写后的文本跑（会话 user = 改写内容），
   transcript 保留原话（界面所见）；
④ PreToolUse matcher=write_file 在 agent 层拦写：write_file 不执行、
   tool_execution_end isError、拒绝理由回给模型。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from emberpy.hooks.runner import quote_command
from emberpy.rpc import Worker, _Out
from emberpy.testing import FakeLLM, tool_call

AUTO_WINDOW = 21392  # auto-compact 触发阈值=200（推导见 test_worker_compact.py docstring）


@pytest.fixture(autouse=True)
def _tiny_min_tokens(monkeypatch) -> None:
    monkeypatch.setenv("EMBERPY_COMPACT_MIN_TOKENS", "1")


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _marker_code() -> str:
    """假 hook：把 HOOK_EVENT 追加进工作区 hook_marker.txt（每触发一行）。"""
    return (
        "import os;f=open(os.path.join(os.environ['CWD'],'hook_marker.txt'),'a',"
        "encoding='utf-8');f.write(os.environ['HOOK_EVENT']+chr(10));f.close()"
    )


def write_hooks(home: Path, rules: dict[str, Any]) -> Path:
    """在 <home>/.ember/hooks.json 写入一组事件 hook（现代外壳形状）。"""
    return _write(home / ".ember" / "hooks.json", json.dumps({"hooks": rules}, ensure_ascii=False))


def make_worker(workspace: Path, home: Path, script: list[dict[str, Any]], **kwargs) -> tuple[Worker, list[str]]:
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
                return obj
        time.sleep(0.02)
    raise AssertionError(f"没有 {rid} 的响应。")


def run_prompt(worker: Worker, sink: list[str], message: str, rid: str) -> None:
    send(worker, "prompt", rid, message=message)
    wait_run_done(worker)
    resp = wait_response(sink, rid)
    assert resp["success"] is True, resp.get("error")


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


def _big(seed: str) -> str:
    return "助手已处理需求。要点：" + seed * 400  # ≈411 字符 -> ÷3 ≈137 token


# ---------------------------------------------------------------------------
# ① SessionStart / PreCompact / PostCompact 各触发一次
# ---------------------------------------------------------------------------


def test_session_and_compact_hooks_fire_once(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    cmd = quote_command(sys.executable, "-c", _marker_code())
    write_hooks(
        home,
        {
            "SessionStart": [{"hooks": [{"type": "command", "command": cmd}]}],
            "PreCompact": [{"hooks": [{"type": "command", "command": cmd}]}],
            "PostCompact": [{"hooks": [{"type": "command", "command": cmd}]}],
        },
    )
    monkeypatch.setenv("EMBER_HOME", str(home))
    monkeypatch.setenv("EMBERPY_CONTEXT_WINDOW", str(AUTO_WINDOW))

    script = [
        {"content": _big("一"), "calls": None},      # p0 答复
        {"content": _big("二"), "calls": None},      # p1 答复
        {"content": "（自动摘要）前两轮已确认。", "calls": None},  # p2 摘要
        {"content": "第三轮答复", "calls": None},    # p2 答复
    ]
    worker, sink = make_worker(workspace, home, script)
    send(worker, "set_auto_compaction", "a1", enabled=True)

    marker = workspace / "hook_marker.txt"
    assert not marker.exists()

    run_prompt(worker, sink, "任务0", "p0")
    # SessionStart 在首个会话建好时触发一次；前两轮历史未超阈值，不自动压缩
    assert marker.read_text(encoding="utf-8").splitlines() == ["SessionStart"]

    run_prompt(worker, sink, "任务1", "p1")
    assert marker.read_text(encoding="utf-8").splitlines() == ["SessionStart"]

    # 第三轮：历史超阈值 -> PreCompact 先触发 -> 压缩 -> PostCompact 触发
    run_prompt(worker, sink, "任务2", "p2")
    assert any("第三轮答复" in t for t in assistant_texts(sink))
    events = marker.read_text(encoding="utf-8").splitlines()
    assert events == ["SessionStart", "PreCompact", "PostCompact"]

    # 会话被重写：最前是摘要气泡
    assert worker.session.events()[0]["message"]["content"].startswith("＞ 已压缩")


# ---------------------------------------------------------------------------
# ② UserPromptSubmit 阻塞：不起 agent
# ---------------------------------------------------------------------------


def test_user_prompt_submit_block_skips_agent(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    block = (
        "import sys;print('暂停接收输入',file=sys.stderr);sys.exit(2)"
    )
    write_hooks(home, {"UserPromptSubmit": [{"type": "command", "command": quote_command(sys.executable, "-c", block)}]})
    monkeypatch.setenv("EMBER_HOME", str(home))

    script = [{"content": "不该被消费"}]
    worker, sink = make_worker(workspace, home, script)
    run_prompt(worker, sink, "写个文件", "p0")

    # agent 没跑：假模型脚本原封未动
    assert len(worker.injected_llm._script) == 1
    texts = assistant_texts(sink)
    assert any("被 UserPromptSubmit hook 拦截" in t and "暂停接收输入" in t for t in texts)
    # 用户的输入有被回显（transcript 视角），但没有任何模型答复
    assert any("写个文件" in t for t in assistant_texts(sink) + [str(m.get("content")) for m in worker.transcript.get_messages() if m.get("role") == "user"])


# ---------------------------------------------------------------------------
# ③ UserPromptSubmit 改写 prompt
# ---------------------------------------------------------------------------


def test_user_prompt_submit_modifies_prompt(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    mod = "import json;print(json.dumps({'modifiedPrompt':'改写后的任务'}))"
    write_hooks(home, {"UserPromptSubmit": [{"type": "command", "command": quote_command(sys.executable, "-c", mod)}]})
    monkeypatch.setenv("EMBER_HOME", str(home))

    worker, sink = make_worker(workspace, home, [{"content": "已按改写任务执行"}])
    run_prompt(worker, sink, "帮我做件事", "p0")

    # agent 跑的是改写后的文本：会话历史里 user 是改写内容
    assert any("已按改写任务执行" in t for t in assistant_texts(sink))
    session_users = [
        (ev.get("message") or {}).get("content")
        for ev in worker.session.events()
        if ev.get("type") == "user"
    ]
    assert session_users == ["改写后的任务"]
    # transcript（界面所见）保留原话
    transcript_users = [m for m in worker.transcript.get_messages() if m.get("role") == "user"]
    assert any("帮我做件事" in str(m.get("content")) for m in transcript_users)


# ---------------------------------------------------------------------------
# ④ PreToolUse matcher 在 agent 层拦 write_file
# ---------------------------------------------------------------------------


def test_pre_tool_matcher_blocks_write_at_agent_layer(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    deny = "import sys;print('禁止写',file=sys.stderr);sys.exit(2)"
    write_hooks(
        home,
        {
            "PreToolUse": [
                {"matcher": "write_file", "hooks": [{"type": "command", "command": quote_command(sys.executable, "-c", deny)}]}
            ]
        },
    )
    monkeypatch.setenv("EMBER_HOME", str(home))

    script = [
        {"content": None, "calls": [tool_call("c1", "write_file", {"path": "note.txt", "content": "x"})]},
        {"content": "不写了", "calls": None},
    ]
    worker, sink = make_worker(workspace, home, script)
    run_prompt(worker, sink, "新建一个文件", "p0")

    assert not (workspace / "note.txt").exists()  # hook 拦截，写没发生
    assert any("不写了" in t for t in assistant_texts(sink))

    ends = []
    for line in sink:
        obj = json.loads(line)
        if obj.get("type") == "tool_execution_end":
            ends.append(obj)
    assert len(ends) == 1
    assert ends[0]["toolName"] == "write_file"
    assert ends[0]["isError"] is True  # 拒绝文本带"权限拒绝："前缀 -> 按错误结果展示
    assert "权限拒绝" in ends[0]["result"]
