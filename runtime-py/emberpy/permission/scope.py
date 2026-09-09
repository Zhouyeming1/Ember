"""路径作用域：把任意给定路径解析到工作区之内。

杜绝目录穿越与符号链接逃逸；fs / shell 工具共用这套规则。
"""
from __future__ import annotations

from pathlib import Path


def resolve_within(root: Path, given: str) -> Path:
    """把用户/模型给的路径解析到 root 之下。

    - 相对路径基于 root 展开
    - 绝对路径必须落在 root 内，否则抛 ValueError（杜绝穿越）
    - 用 resolve() 展开符号链接，防止通过软链逃出 root
    """
    candidate = Path(given).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    root_resolved = root.resolve()
    if resolved == root_resolved or root_resolved in resolved.parents:
        return resolved
    raise ValueError(f"路径越界（不在工作区内）：{given}")


def is_within(child: Path, parent: Path) -> bool:
    """child 是否在 parent 之内或等于 parent。"""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
