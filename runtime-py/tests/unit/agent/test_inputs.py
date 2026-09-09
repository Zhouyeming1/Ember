"""运行时外部输入：stop 请求与 steer 注入（RPC 桥接的前置能力）。"""
from __future__ import annotations

from pathlib import Path

from emberpy.agent import Agent, RuntimeInput, run_once
from emberpy.permission import PermissionMode
from emberpy.session import Session
from emberpy.testing import FakeLLM
from emberpy.tools import ToolEnv


def _agent(script, workspace: Path, **kwargs):
    env = ToolEnv(workspace=workspace, gate=None, patches=None, confirm=None)
    return Agent(
        llm=FakeLLM(script),
        workspace=workspace,
        mode=kwargs.pop("mode", "auto"),
        env=env,
        session=Session(cwd=workspace),
        **kwargs,
    )


def test_stop_requested_halts_between_rounds(workspace: Path) -> None:
    # 脚本：第一轮要写文件（一个工具轮），第二轮本来要给最终答复；stop 在第二轮前生效
    script = [
        {"content": "我先写个文件", "calls": [{"id": "c1", "type": "function",
                                               "function": {"name": "write_file", "arguments": '{"path": "a.txt", "content": "x"}'}}]},
        {"content": "完成了", "calls": None},
    ]
    stopped = {"flag": False}

    def should_stop() -> bool:
        return stopped["flag"]

    agent = _agent(script, workspace, runtime_input=RuntimeInput(stop_requested=should_stop))
    result = agent.run("请写文件")
    assert result.steps >= 1
    assert not result.interrupted  # 还没人叫停

    stopped["flag"] = True
    result2 = agent.run("再来一轮")
    assert result2.interrupted is True
    assert result2.final_content is None


def test_steer_injected_as_user_turn(workspace: Path) -> None:
    events: list[tuple[str, dict]] = []
    injected: list[str] = []
    steers: list[str] = ["改成内容 B"]

    def drain() -> list[str]:
        out, steers[:] = steers[:], []
        return out

    agent = _agent(
        [
            # 第一轮：读文件，故意给个会继续的答复（带工具），让 steer 有机会插入
            {"content": None, "calls": [{"id": "c1", "type": "function",
                                         "function": {"name": "read_file", "arguments": '{"path": "hello.txt"}'}}]},
            # 之后模型应当看到 steer 作为 user 消息出现
            {"content": "按你的新指示处理", "calls": None},
        ],
        workspace,
        runtime_input=RuntimeInput(drain_steers=drain, on_user_injected=injected.append),
        on_event=lambda t, d: events.append((t, d)),
    )
    result = agent.run("先读文件")
    assert result.final_content == "按你的新指示处理"
    assert injected == ["改成内容 B"]
    assert any(t == "user_injected" for t, _ in events)
    # steer 进过 session：下一条 user 内容应包含 steer 文本
    text = "".join(str(m.get("content")) for m in agent.session.resume_messages() if m.get("role") == "user")
    assert "改成内容 B" in text


def test_run_once_signature_unaffected(workspace: Path) -> None:
    # run_once 仍可用（无 runtime_input），确保便捷入口没坏
    result = run_once(task="列出目录", llm=FakeLLM([{"content": "目录如下", "calls": None}]), workspace=workspace, mode="plan")
    assert result.final_content == "目录如下"
