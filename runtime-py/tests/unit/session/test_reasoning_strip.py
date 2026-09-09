"""会话里推理文本的取舍：落盘保留，回发模型剥离。"""
from __future__ import annotations

from emberpy.session import Session


def _asst(content: str, reasoning: str) -> dict:
    return {"role": "assistant", "content": content, "reasoning_content": reasoning}


class TestResumeStripsReasoning:
    def test_disk_keeps_but_replay_strips(self, tmp_path) -> None:
        path = tmp_path / "s.jsonl"
        with Session(path=path, cwd=tmp_path) as s:
            s.add_user("任务")
            s.add_assistant(_asst("完成", "内部思考"))

        # 落盘/事件仍保留推理（供展示与审计）
        events = Session(path=path, cwd=tmp_path).events()
        assert events[-1]["message"]["reasoning_content"] == "内部思考"

        # 回发模型前剥离
        msgs = Session(path=path, cwd=tmp_path).resume_messages()
        asst = [m for m in msgs if m["role"] == "assistant"][0]
        assert asst["content"] == "完成"
        assert "reasoning_content" not in asst

    def test_in_memory_session_events_not_mutated(self, tmp_path) -> None:
        s = Session(cwd=tmp_path)
        s.add_user("hi")
        s.add_assistant(_asst("完成", "思考"))
        msgs = s.resume_messages()
        assert all("reasoning_content" not in m for m in msgs if m.get("role") == "assistant")
        # 事件原样未动
        asst = [e["message"] for e in s.events() if e["type"] == "assistant"][0]
        assert asst["reasoning_content"] == "思考"

    def test_user_messages_untouched(self, tmp_path) -> None:
        s = Session(cwd=tmp_path)
        s.add_user("任务")
        s.add_assistant({"role": "assistant", "content": "好"})
        msgs = s.resume_messages()
        assert msgs[0] == {"role": "user", "content": "任务"}
