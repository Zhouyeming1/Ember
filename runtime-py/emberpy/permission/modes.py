"""权限枚举与"界面模式 -> 引擎模式"换算。

PermissionMode 这四级与 Ember 前端下拉完全一致（plan|ask|auto|full）；
sandbox 只是纵深防御（read-only / workspace-write / danger-full-access）。
effective_mode() 就是 Worker 把界面给的 (permission, sandbox) 换算成实际模式的规则。
"""
from __future__ import annotations

from enum import Enum


class PermissionMode(str, Enum):
    PLAN = "plan"  # 只读：可以读工作区、跑只读诊断命令
    ASK = "ask"    # 写文件 / 命令 / 越界访问前逐个询问
    AUTO = "auto"  # 工作区内常规操作自动放行，越界或危险时询问
    FULL = "full"  # 完全访问（仅用于用户显式信任的项目）


class CommandKind(str, Enum):
    READ_ONLY = "read_only"  # 只读诊断命令（git status/ls 之类）
    SAFE = "safe"            # 常规命令
    DANGEROUS = "dangerous"  # 有破坏性，auto 模式下也要询问
    BLOCKED = "blocked"      # 无论什么模式都拒绝


def effective_mode(permission: str, sandbox: str) -> PermissionMode:
    """把桌面端给的 permission + sandbox 换成引擎实际运行的权限模式。

    界面在"空任务/新目录"会用 read-only 沙箱（此时即便 permission=auto 也只读）；
    danger-full-access / full 才给到 FULL。
    """
    if permission == "plan" or sandbox == "read-only":
        return PermissionMode.PLAN
    if permission == "full" or sandbox == "danger-full-access":
        return PermissionMode.FULL
    if permission == "ask":
        return PermissionMode.ASK
    return PermissionMode.AUTO
