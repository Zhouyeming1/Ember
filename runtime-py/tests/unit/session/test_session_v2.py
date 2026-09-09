"""会话 JSONL 落盘 v2（ember-core 索引器可识别格式）测试。

覆盖：session 头只写一次、assistant parts（thinking/text/toolCall）往返、
reasoning 在事件保留 / resume_messages 回发剥离、checkpoint 存改前全文、
v1 旧文件向后兼容（无头、续写不插头）、路径助手 env 解析。

现有 test_session.py / test_reasoning_strip.py 是内存语义不变的回归锁，这里
只验证"文件边界"的 v2 行为，与旧文件同时存在时不冲突。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from emberpy.patches import Patch
from emberpy.session import Session, allocate_session_path, resolve_sessions_dir
from emberpy.testing import tool_call


def _lines(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# v2 头与文件结构
# ---------------------------------------------------------------------------


def test_new_file_writes_session_header_once(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    with Session(path=path, cwd=tmp_path) as s:
        s.add_user("一")
    with Session(path=path, cwd=tmp_path) as s:  # 重开续写，头不能重复
        s.add_user("二")

    rows = _lines(path)
    heads = [r for r in rows if r.get("type") == "session"]
    assert len(heads) == 1, rows
    head = heads[0]
    assert head["version"] == 3
    assert head["cwd"] == str(tmp_path)  # 绝对路径，索引器靠它定位工作区
    # 之后是普通 message 行
    assert rows[1]["type"] == "message"
    messages = Session(path=path, cwd=tmp_path).messages()
    assert [m["content"] for m in messages] == ["一", "二"]


def test_memory_session_writes_nothing(tmp_path: Path) -> None:
    s = Session(cwd=tmp_path)
    s.add_user("内存")
    assert s.path is None
    assert list(tmp_path.glob("*.jsonl")) == []


# ---------------------------------------------------------------------------
# assistant：thinking / text / toolCall parts 往返
# ---------------------------------------------------------------------------


def test_assistant_parts_roundtrip_thinking_and_tool_call(tmp_path: Path) -> None:
    raw = {
        "role": "assistant",
        "content": "我来写文件",
        "reasoning_content": "先想一下",
        "tool_calls": [tool_call("c1", "write_file", {"path": "a.txt", "content": "x"})],
    }
    path = tmp_path / "s.jsonl"
    with Session(path=path, cwd=tmp_path) as s:
        s.add_assistant(raw)

    # 落盘：assistant 消息 content 是 parts 数组（索引器用）
    row = next(r for r in _lines(path) if r.get("type") == "message" and r["message"]["role"] == "assistant")
    parts = row["message"]["content"]
    assert parts[0] == {"type": "thinking", "thinking": "先想一下"}
    assert parts[1] == {"type": "text", "text": "我来写文件"}
    tc = next(p for p in parts if p["type"] == "toolCall")
    assert tc["name"] == "write_file"
    assert tc["id"] == "c1"
    # arguments 保留原始 JSON 串，加载才能精确还原 function.arguments
    assert json.loads(tc["arguments"]) == {"path": "a.txt", "content": "x"}

    # 重载：还原成与写入完全一致的原始 assistant 消息 dict
    loaded = Session(path=path, cwd=tmp_path).events()[-1]
    assert loaded["type"] == "assistant"
    assert loaded["message"] == raw


def test_reasoning_kept_in_events_stripped_for_resume(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    with Session(path=path, cwd=tmp_path) as s:
        s.add_user("任务")
        s.add_assistant({"role": "assistant", "content": "回答", "reasoning_content": "推理过程"})

    # 事件里保留 reasoning（展示/审计用）
    reloaded = Session(path=path, cwd=tmp_path)
    assert reloaded.events()[-1]["message"]["reasoning_content"] == "推理过程"
    assert reloaded.messages()[-1].get("reasoning_content") == "推理过程"
    # resume_messages 回发前剥离推理（reasoner 多轮规范）
    resumed = reloaded.resume_messages()[-1]
    assert resumed["content"] == "回答"
    assert "reasoning_content" not in resumed


def test_checkpoint_stores_full_before_after(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    with Session(path=path, cwd=tmp_path) as s:
        s.add_patch(Patch(seq=1, path=tmp_path / "a.txt", before="旧文", after="新文"))

    event = Session(path=path, cwd=tmp_path).events()[-1]
    assert event["type"] == "patch"
    # 全文 before/after（不是旧版 before_len/after_len），跨重启 /undo 可还原
    assert event["patch"] == {"seq": 1, "path": str(tmp_path / "a.txt"), "before": "旧文", "after": "新文"}


# ---------------------------------------------------------------------------
# v1 向后兼容：无头旧文件原样读、续写不插头
# ---------------------------------------------------------------------------


def test_v1_legacy_file_reads_and_appends_without_header(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    # v1 旧格式：无头，每行一个原始事件、行尾带换行（与旧实现写出的一致）
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "meta", "key": "model", "value": "deepseek-chat"}, ensure_ascii=False),
                json.dumps({"type": "user", "message": {"role": "user", "content": "旧 v1 消息"}}, ensure_ascii=False),
                json.dumps(
                    {"type": "assistant", "message": {"role": "assistant", "content": "旧回答", "reasoning_content": "旧推理"}},
                    ensure_ascii=False,
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )

    s = Session(path=path, cwd=tmp_path)
    assert [e["type"] for e in s.events()] == ["meta", "user", "assistant"]
    # v1 事件里保留原始 message dict
    assert s.events()[-1]["message"]["reasoning_content"] == "旧推理"
    s.add_user("续写")  # 续写保持 v1 行
    s.close()

    rows = _lines(path)
    assert rows[0]["type"] == "meta"  # 没有插入 session 头
    assert rows[-1] == {"type": "user", "message": {"role": "user", "content": "续写"}}

    reloaded = Session(path=path, cwd=tmp_path)
    assert [e["type"] for e in reloaded.events()] == ["meta", "user", "assistant", "user"]
    assert [m["content"] for m in reloaded.messages()] == ["旧 v1 消息", "旧回答", "续写"]


# ---------------------------------------------------------------------------
# 路径助手
# ---------------------------------------------------------------------------


def test_resolve_sessions_dir_env_precedence(tmp_path: Path, monkeypatch) -> None:
    desktop = tmp_path / "desktop-sessions"
    monkeypatch.setenv("EMBER_SESSIONS_DIR", str(desktop))
    assert resolve_sessions_dir() == desktop.resolve()

    # EMBER_SESSIONS_DIR 优先；缺省才回落 EMBER_HOME/sessions
    monkeypatch.delenv("EMBER_SESSIONS_DIR")
    home = tmp_path / "emberhome"
    monkeypatch.setenv("EMBER_HOME", str(home))
    assert resolve_sessions_dir() == (home / "sessions").resolve()

    # 都没有 -> ~/.ember/sessions（只解析路径，不创建目录）
    monkeypatch.delenv("EMBER_HOME")
    assert resolve_sessions_dir() == (Path.home() / ".ember" / "sessions").resolve()


def test_allocate_session_path_unique_and_windows_safe(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EMBER_SESSIONS_DIR", str(tmp_path / "custom"))
    p1 = allocate_session_path()
    p2 = allocate_session_path()
    assert p1 != p2
    assert p1.parent == (tmp_path / "custom").resolve()
    assert p1.parent.is_dir()  # mkdir 已建
    assert p1.suffix == ".jsonl"
    # 文件名不含 Windows 非法字符（: . 斜杠等），可安全当普通文件名
    assert re.fullmatch(r"[A-Za-z0-9._-]+", p1.name)
