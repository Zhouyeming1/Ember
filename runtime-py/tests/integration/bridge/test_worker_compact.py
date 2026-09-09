"""worker 上下文压缩集成测试（claude autoCompact / /compact 契约对齐）。

场景：
① 手动 compact：跑了几轮后压缩——返回 tokensBefore、转录变成「摘要气泡 + 保留的
   最后一个 user 整轮」、会话事件被重写、后续 prompt 能正常续跑（摘要留在历史里）；
② 对话太短手动 compact -> success:false + 错误文本含 nothing to compact
   （renderer 正则 /nothing to compact|session too small/i 转 compactTooShort toast）；
③ set_auto_compaction 开 + 小上下文窗 -> 下一轮 prompt 开始前先自动压缩（消耗一条
   摘要模型调用），关掉开关后不再压缩；
④ 自动压缩连续失败熔断（连败 3 次停手，会话仍能正常跑）。

阈值推导（见 compact.auto_compact_threshold）：
    threshold = contextWindow − 8192 − 13000 = window − 21192
本文件把 window 设 21392 -> threshold = 200 tokens。回话估算口径是每消息 字符数÷3：
_big() 约 411 字符 -> ~137 token。故 1 轮历史(~138) < 200 不触发，
2 轮历史(~276) >= 200 触发——足够把「1 轮/2 轮」分开又不依赖精确 token 数。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from emberpy.rpc import Worker, _Out
from emberpy.testing import FakeLLM

# 使自动压缩触发阈值=200（见模块 docstring）
AUTO_WINDOW = 21392


@pytest.fixture(autouse=True)
def _tiny_min_tokens(monkeypatch) -> None:
    # 压测不用造几千 token 的长文本：MIN 下限缩到 1
    monkeypatch.setenv("EMBERPY_COMPACT_MIN_TOKENS", "1")


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
                return obj
        time.sleep(0.02)
    raise AssertionError(f"没有 {rid} 的响应。")


def run_prompt(worker: Worker, sink: list[str], message: str, rid: str) -> None:
    send(worker, "prompt", rid, message=message)
    wait_run_done(worker)
    wait_response(sink, rid)


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


def message_texts(records: list[dict[str, Any]]) -> list[str]:
    """get_messages 返回的记录 -> 每条的可读文本（兼容 str / parts 数组）。"""
    out: list[str] = []
    for record in records:
        content = record.get("content")
        if isinstance(content, str):
            out.append(content)
            continue
        parts = content if isinstance(content, list) else []
        out.append("".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"))
    return out


def _big(seed: str) -> str:
    return "助手已处理需求。要点：" + seed * 400  # ≈411 字符 -> ÷3 ≈137 token


def _summary_item(content: str = "（摘要）旧对话归纳完成：前面的需求都已确认。") -> dict[str, Any]:
    return {"content": content, "calls": None}


# ---------------------------------------------------------------------------
# ① 手动 compact 完整周期
# ---------------------------------------------------------------------------


def test_manual_compact_rewrites_history_and_continues(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    script = [
        {"content": _big("一"), "calls": None},
        {"content": _big("二"), "calls": None},
        {"content": _big("三"), "calls": None},
        _summary_item(),
        {"content": "第四条答复", "calls": None},
    ]
    worker, sink = make_worker(workspace, script)
    for i in range(3):
        run_prompt(worker, sink, f"任务{i}", f"p{i}")

    # 压缩前：6 条消息事件（3 user + 3 assistant）
    assert len(worker.session.events()) == 6

    send(worker, "compact", "c1")
    resp = wait_response(sink, "c1")
    assert resp["success"] is True, resp.get("error")
    data = resp["data"]
    assert data["tokensBefore"] > 0
    assert data["messagesSummarized"] >= 3  # 最后一个 user 前的多轮被归纳
    assert "摘要" in data["summary"]

    # 会话事件被重写：摘要气泡 + 保留的最后一轮（user2 + asst2）
    events = worker.session.events()
    assert events[0]["type"] == "assistant"
    assert events[0]["message"]["content"].startswith("＞ 已压缩")
    kept = [e["message"]["content"] for e in events[1:]]
    assert kept == ["任务2", script[2]["content"]]

    # 转录视角：get_messages 反映压缩后状态（摘要 + 最后 user + 助手答复）
    msgs = worker.transcript.get_messages()
    assert [m["role"] for m in msgs] == ["assistant", "user", "assistant"]
    assert any(t.startswith("＞ 已压缩") for t in message_texts(msgs))

    # 压缩后继续对话：摘要留在历史开头、能正常跑、事件继续增长
    run_prompt(worker, sink, "再跑一轮", "p4")
    assert any("第四条答复" in t for t in assistant_texts(sink))
    ev2 = worker.session.events()
    assert ev2[0]["message"]["content"].startswith("＞ 已压缩")  # 摘要仍是最前
    assert len(ev2) == 3 + 2  # 摘要 + 保留轮 + 新增 user/assistant


# ---------------------------------------------------------------------------
# ② 太短 -> nothing to compact
# ---------------------------------------------------------------------------


def test_manual_compact_too_short_errors(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    script = [{"content": "只有一句", "calls": None}]
    worker, sink = make_worker(workspace, script)
    run_prompt(worker, sink, "任务0", "p0")

    send(worker, "compact", "c1")
    resp = wait_response(sink, "c1")
    assert resp["success"] is False
    assert "nothing to compact" in resp["error"]  # renderer 正则匹配 -> compactTooShort
    # 太短不该改任何东西
    assert len(worker.session.events()) == 2


# ---------------------------------------------------------------------------
# ③ set_auto_compaction + 小窗：下一轮 prompt 自动先压缩
# ---------------------------------------------------------------------------


def test_auto_compact_fires_before_prompt_then_toggles_off(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    monkeypatch.setenv("EMBERPY_CONTEXT_WINDOW", str(AUTO_WINDOW))
    script = [
        {"content": _big("一"), "calls": None},
        {"content": _big("二"), "calls": None},
        _summary_item("（自动摘要）已确认两轮需求。"),
        {"content": "第三条答复", "calls": None},
        {"content": "第四条答复（auto 已关）", "calls": None},
    ]
    worker, sink = make_worker(workspace, script)

    # 桌面端 startAgent 时都会发 set_auto_compaction {enabled:true}
    send(worker, "set_auto_compaction", "a1", enabled=True)
    assert worker.auto_compact_enabled is True

    # 前两轮：1 轮历史低于阈值，不触发自动压缩
    run_prompt(worker, sink, "任务0", "p0")
    run_prompt(worker, sink, "任务1", "p1")
    assert len(worker.injected_llm._script) == 3  # 只消耗了 2 条 asst，摘要还没用
    assert worker.session.events()[0]["message"]["content"] == "任务0"  # 历史原样

    # 第三轮：2 轮历史超阈值 -> prompt 开始前先自动压缩（消耗摘要项）再答复
    run_prompt(worker, sink, "任务2", "p2")
    assert len(worker.injected_llm._script) == 1  # 摘要 + 第三条答复被消耗
    events = worker.session.events()
    assert events[0]["type"] == "assistant"
    assert events[0]["message"]["content"].startswith("＞ 已压缩")
    assert any("第三条答复" in t for t in assistant_texts(sink))
    assert worker._compact_failures == 0

    # 关掉自动压缩：下一轮不再消耗摘要项
    send(worker, "set_auto_compaction", "a2", enabled=False)
    assert worker.auto_compact_enabled is False
    run_prompt(worker, sink, "任务3", "p3")
    assert len(worker.injected_llm._script) == 0  # 只消耗了第四条答复
    assert any("第四条答复（auto 已关）" in t for t in assistant_texts(sink))


# ---------------------------------------------------------------------------
# ④ 自动压缩连续失败熔断
# ---------------------------------------------------------------------------


def test_auto_compact_circuit_breaker_after_failures(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    monkeypatch.setenv("EMBERPY_CONTEXT_WINDOW", str(AUTO_WINDOW))
    script = [
        {"content": _big("一"), "calls": None},  # 0: p0 答复
        {"content": _big("二"), "calls": None},  # 1: p1 答复
        {"content": "", "calls": None},          # 2: p2 自动摘要（空 -> 失败 #1）
        {"content": "答复-第二轮", "calls": None},  # 3: p2 答复
        {"content": "", "calls": None},          # 4: p3 自动摘要（空 -> 失败 #2）
        {"content": "答复-第三轮", "calls": None},  # 5: p3 答复
        {"content": "", "calls": None},          # 6: p4 自动摘要（空 -> 失败 #3，熔断）
        {"content": "答复-第四轮", "calls": None},  # 7: p4 答复
        {"content": "答复-熔断后", "calls": None},   # 8: p5 答复（熔断后不再自动压）
    ]
    worker, sink = make_worker(workspace, script)
    send(worker, "set_auto_compaction", "a1", enabled=True)

    run_prompt(worker, sink, "任务0", "p0")
    run_prompt(worker, sink, "任务1", "p1")

    # 每轮起手都自动压缩一次：摘要为空 -> 累计失败次数 +1，会话照常答复
    run_prompt(worker, sink, "任务2", "p2")
    assert worker._compact_failures == 1
    assert any("答复-第二轮" in t for t in assistant_texts(sink))

    run_prompt(worker, sink, "任务3", "p3")
    assert worker._compact_failures == 2
    assert any("答复-第三轮" in t for t in assistant_texts(sink))

    # 第 3 次失败 -> 熔断线；本轮仍正常答复
    run_prompt(worker, sink, "任务4", "p4")
    assert worker._compact_failures == 3
    assert any("答复-第四轮" in t for t in assistant_texts(sink))

    # 熔断后：_auto_compact_needed 直接返回 False，不再烧摘要调用
    run_prompt(worker, sink, "任务5", "p5")
    assert worker._compact_failures == 3
    assert any("答复-熔断后" in t for t in assistant_texts(sink))
    assert len(worker.injected_llm._script) == 0
