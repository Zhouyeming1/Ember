"""技能系统：跨会话/按需注入专业工作流（skills）。

对齐 claude skills 的 ``<name>/SKILL.md`` 目录式发现 + frontmatter 子集 +
"正文注入成上下文让模型照做"的执行语义（代码自写）。模型驱动走 ``Skill``
工具；用户驱动走 ``/skill:名称`` 前缀（worker 展开成技能正文当任务）。
"""
from .loader import (
    PROJECT_SKILL_SUBDIRS,
    build_skill_store,
    load_skill_roots,
    resolve_skill_roots,
    scan_root,
)
from .models import Skill
from .store import SkillStore

__all__ = [
    "PROJECT_SKILL_SUBDIRS",
    "Skill",
    "SkillStore",
    "build_skill_store",
    "load_skill_roots",
    "resolve_skill_roots",
    "scan_root",
]
