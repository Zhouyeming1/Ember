"""会话 JSONL 落盘 / 续跑 / 统计 / 半截工具轮清理测试。"""

from pathlib import Path

from emberpy.session import Session


def _assistant(content: str = "好的", calls: list | None = None) -> dict:
    msg: dict = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = calls
    return msg


class TestMessages:
    def test_reconstruct_order(self, tmp_path: Path) -> None:
        with Session(path=tmp_path / "s.jsonl", cwd=tmp_path) as s:
            s.add_user("任务")
            s.add_assistant(_assistant(content=None, calls=[{"id": "c1"}]))
            s.add_tool_result("c1", "结果")

        messages = Session(path=tmp_path / "s.jsonl", cwd=tmp_path).messages()
        assert [m["role"] for m in messages] == ["user", "assistant", "tool"]
        assert messages[-1]["tool_call_id"] == "c1"

    def test_meta_not_in_messages(self, tmp_path: Path) -> None:
        with Session(path=tmp_path / "s.jsonl", cwd=tmp_path) as s:
            s.meta("model", "deepseek-chat")
            s.add_user("hi")
        assert len(Session(path=tmp_path / "s.jsonl", cwd=tmp_path).messages()) == 1


class TestResume:
    def test_drop_unfinished_tool_round(self, tmp_path: Path) -> None:
        with Session(path=tmp_path / "s.jsonl", cwd=tmp_path) as s:
            s.add_user("第一轮")
            s.add_assistant(_assistant("完成1"))          # 完整的一轮
            s.add_user("第二轮")
            s.add_assistant(_assistant(content=None, calls=[{"id": "c2"}]))  # 崩溃：没回填

        messages = Session(path=tmp_path / "s.jsonl", cwd=tmp_path).resume_messages()
        roles = [m["role"] for m in messages]
        assert roles == ["user", "assistant", "user"]  # 半截 assistant 及其后都被裁掉

    def test_keep_completed_tool_round(self, tmp_path: Path) -> None:
        with Session(path=tmp_path / "s.jsonl", cwd=tmp_path) as s:
            s.add_user("任务")
            s.add_assistant(_assistant(content=None, calls=[{"id": "c1"}]))
            s.add_tool_result("c1", "ok")

        messages = Session(path=tmp_path / "s.jsonl", cwd=tmp_path).resume_messages()
        assert len(messages) == 3

    def test_drop_stray_tool_result(self, tmp_path: Path) -> None:
        with Session(path=tmp_path / "s.jsonl", cwd=tmp_path) as s:
            s.add_user("任务")
            s.add_assistant(_assistant("完成"))
            s.add_tool_result("no-such-id", "孤立结果")  # 没有对应 assistant

        messages = Session(path=tmp_path / "s.jsonl", cwd=tmp_path).resume_messages()
        assert all(m["role"] != "tool" for m in messages)


class TestStats:
    def test_counts(self, tmp_path: Path) -> None:
        with Session(path=tmp_path / "s.jsonl", cwd=tmp_path) as s:
            s.add_user("a")
            s.add_assistant(_assistant(content=None, calls=[{"id": "c1"}, {"id": "c2"}]))
            s.add_tool_result("c1", "r1")
            s.add_tool_result("c2", "r2")
            s.add_assistant(_assistant("done"))

        stats = Session(path=tmp_path / "s.jsonl", cwd=tmp_path).stats()
        assert stats.user_messages == 1
        assert stats.assistant_messages == 2
        assert stats.tool_calls == 2
        assert stats.tool_results == 2
        assert stats.total_messages == 5


class TestPersistence:
    def test_memory_only_no_file(self, tmp_path: Path) -> None:
        s = Session(cwd=tmp_path)
        s.add_user("内存里")
        assert s.path is None
        s.close()
        # 不落盘：目录里没有任何 jsonl

    def test_append_only_keeps_old(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        with Session(path=path, cwd=tmp_path) as s:
            s.add_user("旧")
        with Session(path=path, cwd=tmp_path) as s:  # 第二次会话续写
            s.add_user("新")
        messages = Session(path=path, cwd=tmp_path).messages()
        assert [m["content"] for m in messages] == ["旧", "新"]
