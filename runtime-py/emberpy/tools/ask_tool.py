"""AskUserQuestion 工具：模型向用户提出单选澄清问题，按答案分支。

复刻 claude AskUserQuestion 的职责：计划前/需求不明时，用结构化选项问
用户，而不是自己瞎猜。行为设计要点（从 claude 源码只取语义、代码自写）：
- 一次问一个问题（本引擎/渲染面只支持单选卡片）；问题 ≤2~4 个选项；
- 选项 label 要短、彼此唯一（界面按 label 渲染按钮、回传的就是 label）；
- 用户取消时把控制权还给模型（让它换更小的确认或按最合理假设继续）。

渲染落点：经 env.ask_value（宿主注入）发 ``extension_ui_request``
method=``select`` 的卡片；Ember 渲染端 ApprovalCard 把它画成选项按钮，
点击回执 {value: <label>}。AskValue 阻塞等待并返回 label；None 表示取消。
只有 env.ask_value 非 None（桌面/交互宿主）才挂载本工具；无头场景不出现，
避免模型对着一个永远没答案的工具空转。
"""
from __future__ import annotations

from typing import Any

from .registry import AskValue, Tool, ToolCategory

_MIN_OPTIONS = 2
_MAX_OPTIONS = 4


def build_ask_user_question_tool(ask_value: AskValue) -> Tool:
    def run(question: str, options: list[Any] | None = None, header: str = "") -> str:
        labels = [str(o) for o in (options or []) if str(o).strip()]
        labels = labels[:_MAX_OPTIONS]
        if not question or not question.strip():
            return "错误：question 不能为空。"
        if len(labels) < _MIN_OPTIONS:
            return (
                "错误：options 至少要 2 个可选项。想清楚再问，或不要问、基于最合理假设继续。"
            )
        heading = question.strip()
        if header and header.strip():
            heading = f"{header.strip()}：{heading}"
        chosen = ask_value(heading, labels)
        if chosen is None:
            return (
                "用户取消了提问。若你仍不确定关键取舍，请选一个最合理假设继续执行，"
                "并在最终回复里说明这个假设；或改用更小、更明确的问题再问一次。"
            )
        return f"用户对“{question.strip()}”的选择是：{chosen}。请据此继续，不要再次重复提问。"

    return Tool(
        name="AskUserQuestion",
        description=(
            "向用户提出一个单选澄清问题（带 2~4 个互斥选项），用户点选后按答案继续。"
            "用于需求有歧义、几个做法都说得通、或改动影响用户偏好时——先问清楚再动手，"
            "胜过猜错返工。用法：question=问句，options=[选项A, 选项B, …]。"
            "选项要短、含义彼此不同；一次只问一个问题；能自己合理推断的事不要问。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "问句（简体中文）"},
                "options": {
                    "type": "array",
                    "description": "2~4 个互斥选项，每个是用户会看到的按钮文案，短且唯一",
                    "items": {"type": "string"},
                    "minItems": _MIN_OPTIONS,
                    "maxItems": _MAX_OPTIONS,
                },
                "header": {
                    "type": "string",
                    "description": "可选：给问题起个 ≤12 字的短标题（界面小标签）",
                },
            },
            "required": ["question", "options"],
        },
        category=ToolCategory.READ,
        fn=run,
    )
