"""项目级数据目录解析：统一落在 ``.ember``。"""

from __future__ import annotations

from pathlib import Path


def project_dot(workspace: Path, rel: str) -> Path:
    """返回项目级路径 ``<workspace>/.ember/<rel>``。

    hooks/permissions/out 等项目内配置统一放在 ``.ember`` 下。调用方负责
    mkdir / exists 等检查。
    """
    return workspace.resolve() / ".ember" / rel
