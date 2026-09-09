"""update_plan 工具：让模型把"计划/进度"写成结构化步骤。

对齐 claude 计划模式的"写计划文件/提交计划"职责，但落点不同：
Ember 渲染端（InspectPanel）不读磁盘计划文件，而是从助手消息里的工具
调用解析待办——凡名字匹配 /plan/i、且参数里带 ``plan`` 数组的工具调用，
会被收集成进度列表；当权限模式为 plan 且有空闲待办时展示"批准/细化"区。

因此本工具把模型提交的计划原样带在 ``args.plan`` 里（每步 {step, status}），
工具本身不写任何文件、不需权限；返回给模型的只是简短回显，让模型知道计划
已记录、后续如何更新状态。执行阶段模型再次调用即可把进度推进到 UI。

安全：category=READ，纯上下文记账，不在任何模式受限（plan 模式也可用）。
"""
from __future__ import annotations

from typing import Any

from .registry import Tool, ToolCategory

# 状态词集：UI 把 completed/complete/done 视为完成；in_progress 高亮为进行中
_STATUSES = {"pending", "in_progress", "completed", "complete", "done"}


def _step_text(item: Any) -> str | None:
    """从一个计划项里抽出步骤文本（兼容对象或字符串两种写法）。"""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("step", "content", "text", "title"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _status_of(item: Any, default: str = "pending") -> str:
    status = item.get("status") if isinstance(item, dict) else None
    done = item.get("done") if isinstance(item, dict) else None
    if status in _STATUSES:
        return status
    if done is True:
        return "completed"
    return default


def build_update_plan_tool() -> Tool:
    def run(plan: list[Any] | None = None, summary: str = "") -> str:
        steps: list[str] = []
        if isinstance(plan, list):
            for item in plan:
                text = _step_text(item)
                if text:
                    steps.append(text)
        if not steps:
            return (
                "错误：plan 为空。请给出一组步骤，如 [{\"step\": \"第 1 步…\", \"status\": \"pending\"}, …]。"
            )
        lines = []
        for idx, text in enumerate(steps, 1):
            lines.append(f"{idx}. {text}")
        note = ""
        if summary:
            note = f"\n概述：{summary}"
        return (
            f"已记录计划（{len(steps)} 步）：\n" + "\n".join(lines) + note
            + "\n执行时每完成一步就再次调用本工具，把对应步骤的 status 改为 in_progress/completed，"
            "让用户看到实时进度。"
        )

    return Tool(
        name="update_plan",
        description=(
            "记录或更新分步计划与执行进度。用于计划模式：调研完先想清楚怎么做，把计划逐条列出；"
            "计划获批准后执行时，每完成一步都调用它把状态推进（in_progress/completed）。"
            "plan 是步骤数组，每步是一个对象：step=步骤说明，status 取值 pending（待做）/"
            "in_progress（进行中）/completed（已完成）。不要只口头说计划，请结构化列出来。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "plan": {
                    "type": "array",
                    "description": "分步计划：每步 {step: 步骤说明, status: pending|in_progress|completed}",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step": {"type": "string", "description": "这一步做什么"},
                            "status": {
                                "type": "string",
                                "description": "状态：pending/in_progress/completed",
                                "default": "pending",
                            },
                        },
                        "required": ["step"],
                    },
                },
                "summary": {
                    "type": "string",
                    "description": "可选：一到两句话概括整个计划",
                },
            },
            "required": ["plan"],
        },
        category=ToolCategory.READ,
        fn=run,
    )
