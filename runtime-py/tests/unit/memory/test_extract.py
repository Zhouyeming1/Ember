"""记忆自动抽取（#10，extractMemories 单次调用版）单元测试。

覆盖 extract.py 的纯逻辑 + 编排：
- 门槛：auto_memory_enabled env 解析 / should_extract_turn 三类轮次判定；
- 档案折叠：patch_lines 动作+相对路径、tool_digest 名去重 + 报错片段、tool_trail；
- 严格 parse：合法数组 / 带废话仍抽中 / 非 JSON 不抛 / 坏条目单条丢；
- merge：同名覆盖（索引单行、created False）/ 新名新建 / 坏条目 drop /
  save 异常（如名字撞 MEMORY.md）不中断整批；
- run_memory_extraction：FakeLLM 合法 JSON → 落盘 + 索引行；空/散文 reply no-op。
"""
from __future__ import annotations

import json
from pathlib import Path

from emberpy.memory import MemoryStore
from emberpy.memory import extract as e
from emberpy.patches import Patch
from emberpy.testing import FakeLLM, tool_call


def make_store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "mem")


def _patch(seq: int, path: Path, before: str | None, after: str | None) -> Patch:
    return Patch(seq=seq, path=path, before=before, after=after)


# ---------------------------------------------------------------------------
# env 门控 / should_extract_turn
# ---------------------------------------------------------------------------


def test_auto_memory_enabled_parses_env(monkeypatch) -> None:
    for value in ("1", "true", "yes", "on", "TRUE", " On "):
        monkeypatch.setenv("EMBERPY_AUTO_MEMORY", value)
        assert e.auto_memory_enabled() is True
    for value in ("", "0", "false", "off", "no", "2"):
        monkeypatch.setenv("EMBERPY_AUTO_MEMORY", value)
        assert e.auto_memory_enabled() is False
    monkeypatch.delenv("EMBERPY_AUTO_MEMORY", raising=False)
    assert e.auto_memory_enabled() is False


def test_min_final_chars_env_override(monkeypatch) -> None:
    monkeypatch.delenv("EMBERPY_AUTO_MEMORY_MIN_FINAL_CHARS", raising=False)
    assert e.min_final_chars() == e.MIN_FINAL_CHARS == 80
    monkeypatch.setenv("EMBERPY_AUTO_MEMORY_MIN_FINAL_CHARS", "5")
    assert e.min_final_chars() == 5
    monkeypatch.setenv("EMBERPY_AUTO_MEMORY_MIN_FINAL_CHARS", "abc")
    assert e.min_final_chars() == e.MIN_FINAL_CHARS


def test_should_extract_turn_rules() -> None:
    # 琐碎寒暄：无 patch、无工具、答复空/过短 -> 跳过
    assert e.should_extract_turn(final_text="", patch_count=0, tool_count=0, min_chars=80) is False
    assert e.should_extract_turn(final_text="好的，没问题", patch_count=0, tool_count=0, min_chars=80) is False
    # 有文件改动 -> 一定抽（哪怕答复很短）
    assert e.should_extract_turn(final_text="改好了", patch_count=1, tool_count=0, min_chars=80) is True
    # 无工具但答复足够长 -> 抽
    long_final = "长" * 81
    assert e.should_extract_turn(final_text=long_final, patch_count=0, tool_count=0, min_chars=80) is True
    # 有工具调用（哪怕没产出最终文本/没改动）-> 抽
    assert e.should_extract_turn(final_text="", patch_count=0, tool_count=2, min_chars=80) is True


# ---------------------------------------------------------------------------
# 档案折叠
# ---------------------------------------------------------------------------


def test_patch_lines_actions_and_relpath(workspace: Path) -> None:
    patches = [
        _patch(1, workspace / "src" / "app.py", "old", "new"),      # 修改
        _patch(2, workspace / "NEW.md", None, "# hi"),              # 新建
        _patch(3, workspace / "gone.py", "x", None),                # 删除
    ]
    text = e.patch_lines(patches, workspace)
    assert "修改 src/app.py" in text
    assert "新建 NEW.md" in text
    assert "删除 gone.py" in text


def test_patch_lines_caps_and_off_workspace(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere" / "f.txt"
    patches = [_patch(i, workspace / f"f{i}.py", None, "x") for i in range(20)]
    text = e.patch_lines(patches, workspace)
    assert len(text.splitlines()) == e.PATCH_LINES_MAX + 1  # 封顶行 + 省略提示
    assert "另有 5 处改动" in text
    # 工作区外路径退化到文件名
    assert e.patch_lines([_patch(1, outside, None, "x")], workspace) == "新建 f.txt"


def test_tool_digest_names_dedup_and_errors(workspace: Path) -> None:
    assistant = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                tool_call("a", "write_file", {"path": "x"}),
                tool_call("b", "read_file", {"path": "y"}),
                tool_call("c", "write_file", {"path": "z"}),  # 与 a 同名 -> 去重
            ],
        },
    }
    ok_result = {"type": "tool", "message": {"role": "tool", "tool_call_id": "a", "content": "已写入 x"}}
    err_result = {"type": "tool", "message": {"role": "tool", "tool_call_id": "b", "content": "错误：没有权限读 y"}}
    names, errors = e.tool_digest([assistant, ok_result, err_result])
    assert names == ["write_file", "read_file"]  # 保序去重
    assert errors == ["错误：没有权限读 y"]


def test_tool_trail_formats() -> None:
    assert e.tool_trail([], []) == ""
    text = e.tool_trail(["write_file", "list_dir"], ["错误：no"])
    assert "用到的工具：write_file、list_dir" in text
    assert "报错片段：错误：no" in text


# ---------------------------------------------------------------------------
# 严格 parse
# ---------------------------------------------------------------------------


def test_parse_valid_array() -> None:
    payload = json.dumps(
        [
            {"name": "user_prefs", "type": "user", "content": "喜欢中文", "description": "沟通语言"},
            {"name": "proj_goal", "type": "project", "content": "用 Python 实现 agent"},
        ],
        ensure_ascii=False,
    )
    valid, issues = e.parse_memory_payload(payload)
    assert issues == []
    assert [v["name"] for v in valid] == ["user_prefs", "proj_goal"]
    assert valid[0]["type"] == "user"


def test_parse_extracts_array_out_of_prose() -> None:
    # 模型回话夹了 markdown 围栏/前后废话仍能抽中数组
    text = "好的，以下是记忆：\n```json\n" + json.dumps(
        [{"name": "a", "type": "user", "content": "正文"}], ensure_ascii=False
    ) + "\n```\n以上。"
    valid, issues = e.parse_memory_payload(text)
    assert valid and valid[0]["name"] == "a"


def test_parse_non_json_returns_empty_not_raise() -> None:
    valid, issues = e.parse_memory_payload("没有任何值得留档的内容。")
    assert valid == [] and issues
    valid, issues = e.parse_memory_payload("")
    assert valid == [] and issues


def test_parse_drops_bad_entries_keeps_good() -> None:
    text = json.dumps(
        [
            {"name": "good", "type": "feedback", "content": "先给结论", "description": "x"},
            {"name": "有空格 名字", "type": "user", "content": "非法名字"},
            {"name": "bad_type", "type": "wat", "content": "类型不在闭集"},
            {"name": "empty", "type": "project", "content": ""},
            {"type": "user", "content": "缺 name"},
            "not a dict",
        ],
        ensure_ascii=False,
    )
    valid, issues = e.parse_memory_payload(text)
    assert [v["name"] for v in valid] == ["good"]
    assert len(issues) == 5  # 每个坏条目一条 issue，整体不失败


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def _candidate(name: str, content: str, type_: str = "project", desc: str = "d") -> dict[str, str]:
    return {"name": name, "description": desc, "type": type_, "content": content}


def test_merge_same_name_overwrites(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save("x", "旧描述", "project", "旧正文")
    report = e.merge_memories(store, [_candidate("x", "新正文", desc="新描述")])
    assert report.created == 0 and report.updated == 1 and report.dropped == 0
    assert report.saved == 1
    # 覆盖：正文/描述变新，索引仍单行
    assert "新正文" in store.read("x")
    assert "旧正文" not in store.read("x")
    index_lines = store.index_text().strip().splitlines()
    assert len(index_lines) == 1
    assert "新描述" in index_lines[0]


def test_merge_new_name_creates(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    report = e.merge_memories(store, [_candidate("brand_new", "正文")])
    assert report.created == 1 and report.updated == 0
    assert store.read("brand_new").startswith("---")


def test_merge_same_name_candidates_last_wins(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    report = e.merge_memories(store, [_candidate("x", "第一版"), _candidate("x", "第二版")])
    assert report.saved == 1  # 同名去重，不产生双条
    assert "第二版" in store.read("x")


def test_merge_drops_bad_and_survives_save_error(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    # 名字合法但撞保留名 MEMORY.md -> store.save 抛错；"MEMORY" 过 regex 但被 store 拒
    report = e.merge_memories(
        store,
        [
            _candidate("ok_one", "正常"),
            _candidate("MEMORY", "撞索引保留名，save 会抛"),
            _candidate("empty_skip", ""),
            _candidate("bad_type", "类型非法", type_="wat"),
        ],
    )
    assert report.created == 1 and report.updated == 0 and report.dropped == 3
    assert store.read("ok_one").startswith("---")  # 前面的成功不受后续坏条目影响


# ---------------------------------------------------------------------------
# build_extract_messages / run_memory_extraction
# ---------------------------------------------------------------------------


def test_build_extract_messages_shape(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save("pref", "旧描述", "user", "用户偏好")
    messages = e.build_extract_messages(
        task="帮我改代码", final="改完了", patch_digest="修改 a.py", tool_digest="工具", index_block=store.index_text()
    )
    assert [m["role"] for m in messages] == ["system", "user"]
    assert e.EXTRACT_SYSTEM_PROMPT in messages[0]["content"]
    user = messages[1]["content"]
    for fragment in ("帮我改代码", "改完了", "修改 a.py", "工具", "[pref](pref.md)"):
        assert fragment in user


def test_run_extraction_saves_from_json(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save("existing", "旧", "project", "旧正文")
    payload = json.dumps(
        [
            {"name": "existing", "type": "project", "content": "被覆盖的新正文", "description": "更新"},
            {"name": "fresh", "type": "feedback", "content": "先结论后展开", "description": "新条目"},
            {"name": "bad name", "type": "user", "content": "被丢"},
        ],
        ensure_ascii=False,
    )
    fake = FakeLLM([{"content": payload}])
    report = e.run_memory_extraction(fake, store, task="任务", final="答复", patch_digest="", tool_digest="")
    assert report.created == 1 and report.updated == 1 and report.dropped == 0
    names = [m.name for m in store.list()]
    assert "existing" in names and "fresh" in names and "bad name" not in names
    assert "被覆盖的新正文" in store.read("existing")
    index = store.index_text()
    assert index.count("[existing]") == 1  # 覆盖后索引仍单行


def test_run_extraction_prose_or_empty_reply_noop(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    for reply in ("没有值得留档的内容。", ""):
        fake = FakeLLM([{"content": reply or None}])
        report = e.run_memory_extraction(fake, store, task="t", final="f", patch_digest="", tool_digest="")
        assert report.saved == 0 and report.dropped == 0
    assert store.list() == []  # 什么都没落盘
