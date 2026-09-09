"""agent 域：编排内核（loop）、运行时输入、系统提示词。
"""
from .core import Agent, EventCallback, RunResult, run_once
from .inputs import RuntimeInput
from .prompts import SYSTEM_TEMPLATE

__all__ = [
    "Agent",
    "RuntimeInput",
    "RunResult",
    "EventCallback",
    "SYSTEM_TEMPLATE",
    "run_once",
]
