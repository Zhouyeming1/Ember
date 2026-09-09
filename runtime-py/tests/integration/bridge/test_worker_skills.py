"""worker 技能链路离线测试。

场景：
① 启用技能（EMBER_HOME 隔离 home + EMBERPY_SKILLS_DIR 指临时技能根）：get_commands
   返回 skill 命令、agent registry 有 Skill 工具、命令带 skill: 前缀与 source 元数据；
② 模型驱动：模型脚本调 Skill(skill, args) -> 技能正文（含 base-dir 头、$ARGUMENTS
   替换）作为 tool 结果回到上下文，随后模型照常收尾；
③ 用户驱动：输入 /skill:summarize 现在总结 -> UI 转录保留原始 /skill: 文本，喂给
   agent 的任务是展开后的技能正文（base-dir 头 + 参数已替换）；
④ 技能不存在：/skill:nope -> 直接回文本列出可用技能，不起 agent（假模型脚本未消耗）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from emberpy.rpc import Worker, _Out
from emberpy.testing import FakeLLM, tool_call


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_skills_root(root: Path) -> None:
    _write(
        root / "summarize" / "SKILL.md",
        "---\nname: summarize\ndescription: 把输入总结成要点\n---\n"
        "把 ${ARGUMENTS} 总结成 3 个要点，逐条写清。基准目录见文件头。",
    )


def make_worker(workspace: Path, skills_root: Path, script: list[dict[str, Any]], **kwargs) -> tuple[Worker, list[str]]:
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


def run_prompt(worker: Worker, sink: list[str], message: str, rid: str = "p1") -> None:
    send(worker, "prompt", rid, message=message)
    wait_run_done(worker)
    for line in reversed(sink):
        obj = json.loads(line)
        if obj.get("type") == "response" and obj.get("id") == rid:
            assert obj.get("success") is True
            return
    raise AssertionError("没有 prompt 响应。")


def enabled(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """把 env 指到临时目录，返回 (workspace, skills_root)。"""
    home = tmp_path / "home"
    home.mkdir()
    skills_root = tmp_path / "skills"
    make_skills_root(skills_root)
    monkeypatch.setenv("EMBER_HOME", str(home))
    monkeypatch.setenv("EMBERPY_SKILLS_DIR", str(skills_root))
    return tmp_path / "proj", skills_root


def request_data(worker: Worker, method: str, rid: str) -> Any:
    sink: list[str] = []
    worker.out = _Out(sink.append)  # 换一个捕获器，避免污染既有断言
    send(worker, method, rid)
    for line in sink:
        obj = json.loads(line)
        if obj.get("type") == "response" and obj.get("id") == rid:
            return obj.get("data")
    raise AssertionError(f"没有 {method} 响应。")


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


# ---------------------------------------------------------------------------
# ① 启用：命令面 + Skill 工具
# ---------------------------------------------------------------------------


def test_skills_enabled_expose_commands_and_tool(tmp_path: Path, monkeypatch) -> None:
    workspace, _root = enabled(tmp_path, monkeypatch)
    worker, _sink = make_worker(workspace, _root, [{"content": "ok"}])

    assert worker.skills is not None
    assert worker.skills.names() == ["summarize"]

    agent = worker._ensure_agent()
    assert "Skill" in agent.registry.names()

    commands = worker.skills.commands()
    assert commands[0]["name"] == "skill:summarize"
    assert commands[0]["source"] == "skill"
    assert commands[0]["sourceInfo"]["baseDir"].endswith("summarize")


def test_get_commands_rpc_lists_skills(tmp_path: Path, monkeypatch) -> None:
    workspace, root = enabled(tmp_path, monkeypatch)
    worker, sink = make_worker(workspace, root, [{"content": "ok"}])
    data = request_data(worker, "get_commands", "c1")
    names = [c["name"] for c in data["commands"]]
    assert names == ["skill:summarize"]


# ---------------------------------------------------------------------------
# ② 模型驱动：Skill 工具执行 -> 技能正文回上下文
# ---------------------------------------------------------------------------


def test_model_invokes_skill_tool(tmp_path: Path, monkeypatch) -> None:
    workspace, root = enabled(tmp_path, monkeypatch)
    script = [
        {
            "content": "先加载技能",
            "calls": [tool_call("c1", "Skill", {"skill": "summarize", "args": "本周报告"})],
        },
        {"content": "已按技能总结完毕。"},
    ]
    worker, sink = make_worker(workspace, root, script)
    run_prompt(worker, sink, "总结本周报告")

    tool_ends = []
    for line in sink:
        obj = json.loads(line)
        if obj.get("type") == "tool_execution_end":
            tool_ends.append(obj)
    assert len(tool_ends) == 1
    result = tool_ends[0]["result"]
    assert "已加载技能「summarize」" in result
    assert "Base directory for this skill:" in result
    assert "把 本周报告 总结成 3 个要点" in result
    assert tool_ends[0]["isError"] is False
    # 最终助手文本到齐
    texts = assistant_texts(sink)
    assert any("已按技能总结完毕" in t for t in texts)


# ---------------------------------------------------------------------------
# ③ 用户驱动：/skill: 展开成任务
# ---------------------------------------------------------------------------


def test_user_slash_skill_expands_to_task(tmp_path: Path, monkeypatch) -> None:
    workspace, root = enabled(tmp_path, monkeypatch)
    worker, sink = make_worker(workspace, root, [{"content": "开始干活。"}])
    run_prompt(worker, sink, "/skill:summarize 现在总结")

    # UI 转录保留原始 /skill: 文本（同一条 user 消息，content 是 parts 数组）
    users = [m for m in worker.transcript.get_messages() if m.get("role") == "user"]
    assert users
    content = users[-1]["content"]
    if isinstance(content, str):
        user_text = content
    else:
        user_text = "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    assert user_text == "/skill:summarize 现在总结"

    # 喂给 agent 的 session 任务 = 展开后的技能正文（base-dir 头 + $ARGUMENTS 替换）
    session = worker.session
    assert session is not None
    user_contents = [
        (ev.get("message") or {}).get("content")
        for ev in session.events()
        if ev.get("type") == "user"
    ]
    expanded = [c for c in user_contents if isinstance(c, str) and c.startswith("Base directory for this skill:")]
    assert len(expanded) == 1
    assert "把 现在总结 总结成 3 个要点" in expanded[0]


# ---------------------------------------------------------------------------
# ④ 技能不存在：直接回文本，不起 agent
# ---------------------------------------------------------------------------


def test_slash_skill_missing_replies_without_agent(tmp_path: Path, monkeypatch) -> None:
    workspace, root = enabled(tmp_path, monkeypatch)
    script = [{"content": "不该被消费"}]
    worker, sink = make_worker(workspace, root, script)
    run_prompt(worker, sink, "/skill:does_not_exist 参数")

    texts = assistant_texts(sink)
    assert any("没有技能" in t and "summarize" in t for t in texts)
    # agent 没跑：假模型脚本原封未动
    assert len(worker.injected_llm._script) == 1
