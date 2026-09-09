"""worker 回合末记忆自动抽取（#10，extractMemories 单次调用版）桥接集成测试。

驱动整条链路：RPC prompt -> agent 跑轮（写文件）-> _execute_prompt 收尾时
_auto_extract_memories 用同一 FakeLLM 多消耗一条 JSON 回复 -> parse/merge 落盘。

场景：
① env=1 + 一轮 write_file -> 记忆 .md + MEMORY.md 索引行（抽取调用计入 FakeLLM 总调用）；
② 不开 env（其余 env 就位，memory store 非 None）-> 无记忆文件、主轮之外无多余调用；
③ 琐碎寒暄轮 -> 不再发第二次 complete（抽取被 should_extract_turn 拦住）；
④ 两轮同名记忆 -> 单 .md、索引单行、正文为第二轮；
⑤ 抽取回复非 JSON / 抽取那次调用抛异常 -> 主轮 success、不落盘、可再发下一轮。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from emberpy.rpc import Worker, _Out
from emberpy.testing import FakeLLM, tool_call


def make_worker(workspace: Path, script: list[dict[str, Any]], **kwargs) -> tuple[Worker, FakeLLM, list[str]]:
    sink: list[str] = []
    llm = FakeLLM(script)
    worker = Worker(workspace=workspace, llm=llm, out=_Out(sink.append), **kwargs)
    return worker, llm, sink


def send(worker: Worker, method: str, rid: str, **params: Any) -> None:
    worker.handle_line(json.dumps({"type": method, "id": rid, **params}, ensure_ascii=False))


def run_prompt(worker: Worker, sink: list[str], message: str, rid: str = "p1") -> None:
    send(worker, "prompt", rid, message=message)
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if worker._run_thread is None or not worker._run_thread.is_alive():
            break
        time.sleep(0.02)
    for line in reversed(sink):
        obj = json.loads(line)
        if obj.get("type") == "response" and obj.get("id") == rid:
            assert obj.get("success") is True
            return
    raise AssertionError("没有 prompt 响应。")


def _mem_json(name: str, content: str, type_: str = "project", desc: str = "d") -> str:
    return json.dumps([{"name": name, "type": type_, "content": content, "description": desc}], ensure_ascii=False)


def _write_tool(path: str) -> dict[str, Any]:
    return tool_call("c1", "write_file", {"path": path, "content": "内容" + path + "\n"})


def enable_persist(tmp_path: Path, monkeypatch) -> Path:
    """桌面场景：开 EMBER_HOME + 记忆目录覆盖；返回 mem_dir。"""
    mem_dir = tmp_path / "mem"
    monkeypatch.setenv("EMBER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EMBERPY_MEMORY_DIR", str(mem_dir))
    monkeypatch.setenv("EMBERPY_AUTO_MEMORY", "1")
    return mem_dir


# ---------------------------------------------------------------------------
# ① 开启时：write_file 轮 -> 自动落记忆
# ---------------------------------------------------------------------------


def test_auto_memory_saves_on_file_change(tmp_path: Path, monkeypatch) -> None:
    mem_dir = enable_persist(tmp_path, monkeypatch)
    # 主轮 2 条（工具调用 + 最终答复）+ 预留 1 条抽取 JSON 回复
    script = [
        {"content": None, "calls": [_write_tool("note.txt")]},
        {"content": "已写入 note.txt。"},
        {"content": _mem_json("proj_note", "工作区已创建 note.txt 记录说明。", desc="note.txt")},
    ]
    worker, llm, sink = make_worker(tmp_path / "proj", script)
    assert worker.memory is not None
    run_prompt(worker, sink, "写一个 note.txt")

    assert (mem_dir / "proj_note.md").is_file()
    index = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "[proj_note](proj_note.md)" in index
    # 抽取确实多消耗了一次模型调用（agent 2 次 + 抽取 1 次）
    assert len(llm.complete_calls) == 3
    # 写文件本身成功
    assert (tmp_path / "proj" / "note.txt").is_file()


# ---------------------------------------------------------------------------
# ② 不开 env：记忆目录就位但默认关 -> 零抽取、零落盘
# ---------------------------------------------------------------------------


def test_auto_memory_off_by_default(tmp_path: Path, monkeypatch) -> None:
    mem_dir = tmp_path / "mem"
    monkeypatch.setenv("EMBER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EMBERPY_MEMORY_DIR", str(mem_dir))
    # 注意：不设 EMBERPY_AUTO_MEMORY
    script = [{"content": "好的，随时效劳。"}]
    worker, llm, sink = make_worker(tmp_path / "proj", script)
    assert worker.memory is not None  # store 就位
    run_prompt(worker, sink, "你好")

    assert llm.complete_calls and len(llm.complete_calls) == 1  # 主轮只调一次，无抽取
    assert not mem_dir.exists() or list(mem_dir.glob("*.md")) == []  # 没有记忆落盘


# ---------------------------------------------------------------------------
# ③ 琐碎寒暄轮：应跳过，不再发第二次 complete
# ---------------------------------------------------------------------------


def test_trivial_turn_skips_extraction(tmp_path: Path, monkeypatch) -> None:
    enable_persist(tmp_path, monkeypatch)
    script = [{"content": "好的。"}]  # 短答复、无工具、无改动
    worker, llm, sink = make_worker(tmp_path / "proj", script)
    run_prompt(worker, sink, "你好")
    assert len(llm.complete_calls) == 1


# ---------------------------------------------------------------------------
# ④ 两轮同名记忆：单文件、索引单行、正文为第二轮
# ---------------------------------------------------------------------------


def test_same_name_overwrites_across_turns(tmp_path: Path, monkeypatch) -> None:
    mem_dir = enable_persist(tmp_path, monkeypatch)
    script = [
        # 第一轮：写 a.txt + 抽取 JSON（同名 proj_state，v1）
        {"content": None, "calls": [_write_tool("a.txt")]},
        {"content": "第一轮完成。"},
        {"content": _mem_json("proj_state", "第一轮：新建 a.txt。", desc="v1")},
        # 第二轮：写 b.txt + 抽取 JSON（同名 proj_state，v2 -> 覆盖）
        {"content": None, "calls": [_write_tool("b.txt")]},
        {"content": "第二轮完成。"},
        {"content": _mem_json("proj_state", "第二轮：又新建 b.txt。", desc="v2")},
    ]
    worker, _llm, sink = make_worker(tmp_path / "proj", script)
    run_prompt(worker, sink, "第一轮，写 a.txt", rid="t1")
    run_prompt(worker, sink, "第二轮，写 b.txt", rid="t2")

    md_files = [p.name for p in mem_dir.glob("*.md") if p.name != "MEMORY.md"]
    assert md_files == ["proj_state.md"]  # 单文件
    text = (mem_dir / "proj_state.md").read_text(encoding="utf-8")
    assert "第二轮：又新建 b.txt。" in text
    assert "第一轮：新建 a.txt。" not in text  # 已被覆盖
    index_lines = [l for l in (mem_dir / "MEMORY.md").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert sum("[proj_state]" in l for l in index_lines) == 1  # 索引单行


# ---------------------------------------------------------------------------
# ⑤ 抽取异常/非 JSON：主轮照常成功，不落盘
# ---------------------------------------------------------------------------


def test_extraction_non_json_and_throw_do_not_break_turn(tmp_path: Path, monkeypatch) -> None:
    mem_dir = enable_persist(tmp_path, monkeypatch)
    # 第一轮：抽取回散文（非 JSON）-> no-op
    script_a = [
        {"content": None, "calls": [_write_tool("a.txt")]},
        {"content": "写完 a.txt。"},
        {"content": "本轮没有需要长期留档的内容。"},
    ]
    worker, llm, sink = make_worker(tmp_path / "proj", script_a)
    run_prompt(worker, sink, "写 a.txt", rid="t1")
    assert len(llm.complete_calls) == 3  # 抽取那次调用发生了，只是没产出可存 JSON
    assert not mem_dir.exists() or list(mem_dir.glob("*.md")) == []

    # 第二轮：抽取那次模型调用直接抛异常 -> 仍 success、可继续下一轮
    script_b = [
        {"content": None, "calls": [_write_tool("b.txt")]},
        {"content": "写完 b.txt。"},
        # 下面没有了：抽取那次 complete 抛
    ]

    class BoomLLM(FakeLLM):
        def complete(self, messages, tools=None):
            self.boom_calls = getattr(self, "boom_calls", 0) + 1
            if self.boom_calls == 3:  # 第 3 次 = 抽取调用
                raise RuntimeError("抽取模型挂了")
            return super().complete(messages, tools)

    sink2: list[str] = []
    boom = BoomLLM(script_b)
    worker2 = Worker(workspace=tmp_path / "proj", llm=boom, out=_Out(sink2.append))
    run_prompt(worker2, sink2, "写 b.txt", rid="t2")  # 不抛、response success
    assert getattr(boom, "boom_calls", 0) == 3
    assert list(mem_dir.glob("*.md")) == []  # 没被半截写入
    assert (tmp_path / "proj" / "b.txt").is_file()  # 主流程文件改动照常
