"""权限域：模式换算 + 命令分类规则 + 路径作用域 + 权限门。

对齐 claude-code-analysis 里的权限设计思路（plan/ask/auto/full + 危险拦截），
但这里是 Python 侧独立实现。错误统一在 errors.py，这里重导出 Denied 以便
``from emberpy.permission import Denied`` 使用。
"""
from ..errors import Denied
from .gate import Confirm, PermissionGate
from .modes import CommandKind, PermissionMode, effective_mode
from .policy import PermissionPolicy, load_permission_policy
from .rules import classify_command
from .scope import is_within, resolve_within

__all__ = [
    "Denied",
    "Confirm",
    "PermissionGate",
    "PermissionMode",
    "CommandKind",
    "effective_mode",
    "PermissionPolicy",
    "load_permission_policy",
    "classify_command",
    "is_within",
    "resolve_within",
]
