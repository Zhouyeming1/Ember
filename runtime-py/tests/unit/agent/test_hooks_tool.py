"""Agent hooks 接线端到端测试：真实 subprocess hook 拦/观/报工具执行。

验证（对齐 claude 的 hooks 契约在引擎里的落点）：
- PreToolUse matcher 命中 write_file -> 拒绝执行（denied=True、文件未写、循环不崩）；
- PreToolUse matcher 不命中的工具照常跑（只拦目标工具）；
- PostToolUse 成功侧触发（hook 能从 stdin 读到 tool_name/tool_response）；
- 工具抛异常 -> PostToolUseFailure 而非 PostToolUse；
- run 收尾触发 Stop。

hook 命令用 ``python -c`` 当假 hook（真实 subprocess；经 quote_command 组跨平台命令）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from emberpy.agent import Agent
from emberpy.hooks import HookManager
from emberpy.hooks.config import HookRule
from emberpy.hooks.runner import quote_command
from emberpy.session import Session
from emberpy.testing import FakeLLM, tool_call


def _cmd(code: str) -> str:
    return quote_command(sys.executable, "-c", code)


def _block_write() -> HookManager:
    return HookManager(
        {
            "PreToolUse": [
                HookRule(
                    event="PreToolUse",
                    command=_cmd(
                        "import sys;print('测试阻止写文件',file=sys.stderr);sys.exit(2)"
                    ),
                    matcher=re.compile("write_file"),
                )
            ]
        }
    )


class _RecordingLLM(FakeLLM):
    """FakeLLM 基础上额外记录每次收到的全量消息（供断言拒绝理由回给了模型）。"""

    def __init__(self, script: list[dict]) -> None:
        super().__init__(script)
        self.seen: list[list[dict]] = []

    def complete(self, messages, tools=None):
        self.seen.append(list(messages))
        return super().complete(messages, tools)


def _run(script: list[dict], workspace: Path, hooks: HookManager,
         llm: FakeLLM | None = None):
    events: list[tuple[str, dict]] = []
    agent = Agent(
        llm=llm if llm is not None else FakeLLM(script),
        workspace=workspace,
        mode="auto",
        session=Session(cwd=workspace),
        hooks=hooks,
        on_event=lambda t, d: events.append((t, d)),
    )
    result = agent.run("测试任务")
    return agent, events, result


def test_pre_tool_matcher_blocks_write(workspace: Path) -> None:
    rec = _RecordingLLM(
        [
            {"content": None, "calls": [tool_call("c1", "write_file", {"path": "note.txt", "content": "x"})]},
            {"content": "明白了，不写了", "calls": None},
        ]
    )
    agent, events, result = _run(
        [],
        workspace,
        _block_write(),
        llm=rec,
    )

    assert result.final_content == "明白了，不写了"
    assert not (workspace / "note.txt").exists()  # hook 拒绝，fn 没机会执行

    # 广播里能看到 tool_result denied=True + PreToolUse hook 事件
    results = [d for t, d in events if t == "tool_result"]
    assert len(results) == 1 and results[0]["name"] == "write_file" and results[0]["denied"] is True
    hook_events = [d for t, d in events if t == "hook"]
    assert any(d["event"] == "PreToolUse" and "测试阻止写文件" in d["text"] for d in hook_events)

    # 拒绝理由确实回给了模型（第二轮模型收到的消息里有一条 tool 消息带"权限拒绝"）
    tool_msgs = [m for m in rec.seen[1] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "权限拒绝" in str(tool_msgs[0]["content"])
    assert "write_file" in str(tool_msgs[0]["content"])


def test_pre_tool_matcher_only_hits_target_tool(workspace: Path) -> None:
    """matcher=write_file 只拦写，read_file 不受影响。"""
    (workspace / "hello.txt").write_text("你好", encoding="utf-8")
    _, events, result = _run(
        [
            {"content": None, "calls": [tool_call("c1", "read_file", {"path": "hello.txt"})]},
            {"content": None, "calls": [tool_call("c2", "write_file", {"path": "note.txt", "content": "x"})]},
            {"content": "做完了", "calls": None},
        ],
        workspace,
        _block_write(),
    )

    assert result.final_content == "做完了"
    tool_results = [d for t, d in events if t == "tool_result"]
    # read_file 放行（denied False）、write_file 被拒（denied True）
    assert tool_results[0]["name"] == "read_file" and tool_results[0]["denied"] is False
    assert tool_results[1]["name"] == "write_file" and tool_results[1]["denied"] is True
    assert not (workspace / "note.txt").exists()


def test_post_tool_fires_on_success_with_tool_name(workspace: Path) -> None:
    """PostToolUse 在成功侧触发，stdin payload 带 tool_name/tool_response。"""
    (workspace / "hello.txt").write_text("内容", encoding="utf-8")
    hooks = HookManager(
        {
            "PostToolUse": [
                HookRule(
                    event="PostToolUse",
                    command=_cmd(
                        "import sys,json;d=json.load(sys.stdin);"
                        "print('saw='+d['tool_name']+':'+str(d['tool_response'])[:20])"
                    ),
                )
            ]
        }
    )
    _, events, result = _run(
        [
            {"content": None, "calls": [tool_call("c1", "read_file", {"path": "hello.txt"})]},
            {"content": "读完了", "calls": None},
        ],
        workspace,
        hooks,
    )
    assert result.final_content == "读完了"
    hook_events = [d for t, d in events if t == "hook"]
    assert any(
        d["event"] == "PostToolUse" and "saw=read_file:" in d["text"] for d in hook_events
    )


def test_post_tool_failure_on_raising_tool(workspace: Path) -> None:
    """工具抛 TypeError（漏参数）-> 触发 PostToolUseFailure 而非 PostToolUse。"""
    hooks = HookManager(
        {
            "PostToolUse": [
                HookRule(event="PostToolUse", command=_cmd("print('ok')"))
            ],
            "PostToolUseFailure": [
                HookRule(event="PostToolUseFailure", command=_cmd("print('failed')"))
            ],
        }
    )
    _, events, result = _run(
        [
            # write_file 缺 content 参数 -> fn(**arguments) 抛 TypeError
            {"content": None, "calls": [tool_call("c1", "write_file", {"path": "x.txt"})]},
            {"content": "我知道了", "calls": None},
        ],
        workspace,
        hooks,
    )
    assert result.final_content == "我知道了"
    hook_events = [d for t, d in events if t == "hook"]
    events_by = [d["event"] for d in hook_events]
    assert "PostToolUseFailure" in events_by
    assert "PostToolUse" not in events_by  # 失败侧不再当成功触发


def test_stop_fires_at_end_of_run(workspace: Path) -> None:
    hooks = HookManager(
        {
            "Stop": [
                HookRule(event="Stop", command=_cmd("import sys;print('stop:'+sys.stdin.read()[:40])"))
            ]
        }
    )
    _, events, result = _run(
        [{"content": "收工", "calls": None}],
        workspace,
        hooks,
    )
    assert result.final_content == "收工"
    hook_events = [d for t, d in events if t == "hook"]
    assert any(d["event"] == "Stop" and "stop:" in d["text"] for d in hook_events)


def test_no_hooks_is_noop(workspace: Path) -> None:
    """不传 hooks -> 全部触发点 no-op，事件流里没有 hook 事件。"""
    _, events, result = _run(
        [
            {"content": None, "calls": [tool_call("c1", "read_file", {"path": "hello.txt"})]},
            {"content": "好了", "calls": None},
        ],
        workspace,
        HookManager({}),
    )
    assert result.final_content == "好了"
    assert not any(t == "hook" for t, _ in events)
