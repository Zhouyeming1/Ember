"""计划模式的 system prompt 附加指引 + set_mode 单元测试。"""

from emberpy.permission import PermissionMode
from emberpy.testing import make_agent


def test_plan_mode_appends_plan_guide(workspace) -> None:
    agent = make_agent([{"content": "ok", "calls": None}], workspace, mode="plan")
    prompt = agent._system_prompt()["content"]
    assert "计划模式" in prompt
    assert "update_plan" in prompt
    assert "不要调用 write_file / file_edit / run_command" in prompt


def test_auto_mode_has_no_guide(workspace) -> None:
    agent = make_agent([{"content": "ok", "calls": None}], workspace, mode="auto")
    prompt = agent._system_prompt()["content"]
    assert "计划模式" not in prompt


def test_set_mode_toggles_guide_live(workspace) -> None:
    agent = make_agent([{"content": "ok", "calls": None}], workspace, mode="auto")
    assert "计划模式" not in agent._system_prompt()["content"]
    agent.set_mode("plan")
    assert agent.gate.mode is PermissionMode.PLAN
    assert "计划模式" in agent._system_prompt()["content"]
    agent.set_mode(PermissionMode.AUTO)
    assert agent.gate.mode is PermissionMode.AUTO
    assert "计划模式" not in agent._system_prompt()["content"]
