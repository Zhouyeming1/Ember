"""agentic 循环端到端测试：假模型脚本驱动真实工具执行。"""

from emberpy.testing import FakeLLM, make_agent, tool_call


class TestEndToEnd:
    def test_read_write_final(self, workspace) -> None:
        agent = make_agent(
            [
                {"content": None, "calls": [tool_call("c1", "read_file", {"path": "hello.txt"})]},
                {"content": None, "calls": [tool_call("c2", "write_file", {"path": "note.txt", "content": "你好，引擎"})]},
                {"content": "全部完成", "calls": None},
            ],
            workspace,
            mode="auto",
        )
        result = agent.run("测试任务")

        assert result.final_content == "全部完成"
        assert result.steps == 2
        assert (workspace / "note.txt").read_text(encoding="utf-8") == "你好，引擎"
        assert result.stats.tool_calls == 2
        assert result.stats.patches == 1

        # 模型每次收到的消息数应单调增长（工具结果确实回填进了上下文）
        sizes = [n for n, _ in agent.llm.complete_calls]
        assert sizes == sorted(sizes) and len(sizes) == 3

    def test_plan_mode_blocks_write_but_keeps_going(self, workspace) -> None:
        """plan 模式下 write_file 被权限门拒绝，但循环不崩，把拒绝理由回给模型。"""
        agent = make_agent(
            [
                {"content": None, "calls": [tool_call("c1", "write_file", {"path": "x.txt", "content": "x"})]},
                {"content": "明白了，我只读", "calls": None},
            ],
            workspace,
            mode="plan",
        )
        result = agent.run("请只读分析")

        assert not (workspace / "x.txt").exists()
        assert result.steps == 1
        assert result.final_content == "明白了，我只读"
        # 权限拒绝被当作工具结果回传，模型上下文里应能看到
        last_messages = agent.session.messages()
        tool_payload = last_messages[-2]["content"]
        assert "拒绝" in tool_payload

    def test_unknown_tool_name_becomes_result(self, workspace) -> None:
        agent = make_agent(
            [
                {"content": None, "calls": [tool_call("c1", "no_such_tool", {})]},
                {"content": "我错了", "calls": None},
            ],
            workspace,
            mode="auto",
        )
        result = agent.run("任务")
        assert result.steps == 1
        assert "未知工具" in agent.session.messages()[-2]["content"]

    def test_stops_when_no_tool_calls(self, workspace) -> None:
        agent = make_agent([{"content": "直接回答", "calls": None}], workspace, mode="auto")
        result = agent.run("只需要文字")
        assert result.steps == 0
        assert result.final_content == "直接回答"


class TestUndo:
    def test_undo_restores_file(self, workspace) -> None:
        (workspace / "config.txt").write_text("旧配置\n", encoding="utf-8", newline="\n")
        agent = make_agent(
            [
                {"content": None, "calls": [tool_call("c0", "read_file", {"path": "config.txt"})]},
                {"content": None, "calls": [tool_call("c1", "write_file", {"path": "config.txt", "content": "新配置\n"})]},
                {"content": "改好了", "calls": None},
            ],
            workspace,
            mode="auto",
        )
        agent.run("改配置文件")
        assert (workspace / "config.txt").read_text(encoding="utf-8") == "新配置\n"

        msg = agent.undo_last_change()
        assert "已回滚" in msg
        assert (workspace / "config.txt").read_text(encoding="utf-8") == "旧配置\n"
        assert agent.undo_last_change() == "没有可回滚的改动。"

    def test_max_steps_bounds(self, workspace) -> None:
        # 模型一直要调用工具 -> 应被 max_steps 截停
        agent = make_agent(
            [{"content": None, "calls": [tool_call(f"c{i}", "list_dir", {"path": "."})]} for i in range(50)],
            workspace,
            mode="auto",
        )
        agent.max_steps = 3
        result = agent.run("任务")
        assert result.steps == 3
        assert result.stopped_by_limit
