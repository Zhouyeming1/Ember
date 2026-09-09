"""update_plan / AskUserQuestion 工具单元测试。"""

from pathlib import Path
from typing import Optional

from emberpy.patches import PatchStore
from emberpy.permission import PermissionGate, PermissionMode
from emberpy.tools import ToolEnv, default_registry


def _registry(workspace: Path, ask_value=None) -> ToolEnv:
    gate = PermissionGate(PermissionMode.AUTO, workspace)
    patches = PatchStore()
    env = ToolEnv(workspace=workspace, gate=gate, patches=patches, ask_value=ask_value)
    return env


class TestUpdatePlan:
    def test_registered_by_default(self, workspace: Path) -> None:
        reg = default_registry(_registry(workspace))
        assert "update_plan" in reg.names()

    def test_plan_object_list(self, workspace: Path) -> None:
        tool = default_registry(_registry(workspace)).get("update_plan")
        out = tool.fn(
            plan=[
                {"step": "先读配置", "status": "pending"},
                {"step": "改核心逻辑", "status": "pending"},
            ]
        )
        assert "2 步" in out
        assert "先读配置" in out and "改核心逻辑" in out

    def test_string_steps_tolerated(self, workspace: Path) -> None:
        tool = default_registry(_registry(workspace)).get("update_plan")
        assert "第一步" in tool.fn(plan=["第一步", "第二步"])

    def test_empty_plan_errors(self, workspace: Path) -> None:
        tool = default_registry(_registry(workspace)).get("update_plan")
        out = tool.fn(plan=[])
        assert "错误" in out
        out2 = tool.fn(plan=[{"detail": "没有 step 字段"}])
        assert "错误" in out2

    def test_summary_appended(self, workspace: Path) -> None:
        tool = default_registry(_registry(workspace)).get("update_plan")
        out = tool.fn(plan=[{"step": "x"}], summary="两步走")
        assert "两步走" in out


class TestAskUserQuestion:
    def test_absent_without_hook(self, workspace: Path) -> None:
        reg = default_registry(_registry(workspace))
        assert "AskUserQuestion" not in reg.names()

    def test_present_with_hook_and_passes_selection(self, workspace: Path) -> None:
        calls: list[tuple[str, list[str]]] = []

        def fake_ask(title: str, options: list[str]) -> Optional[str]:
            calls.append((title, list(options)))
            return "方案 B"

        tool = default_registry(_registry(workspace, ask_value=fake_ask)).get("AskUserQuestion")
        out = tool.fn(question="用哪个方案？", options=["方案 A", "方案 B"])
        assert calls == [("用哪个方案？", ["方案 A", "方案 B"])]
        assert "选择是：方案 B" in out

    def test_canceled_returns_control(self, workspace: Path) -> None:
        def fake_ask(title: str, options: list[str]) -> Optional[str]:
            return None

        tool = default_registry(_registry(workspace, ask_value=fake_ask)).get("AskUserQuestion")
        assert "取消" in tool.fn(question="继续吗？", options=["继续", "停下"])

    def test_too_few_options_errors_without_asking(self, workspace: Path) -> None:
        called = []

        def fake_ask(title: str, options: list[str]) -> Optional[str]:
            called.append(title)
            return None

        tool = default_registry(_registry(workspace, ask_value=fake_ask)).get("AskUserQuestion")
        out = tool.fn(question="确认？", options=["只有一项"])
        assert "错误" in out and called == []
