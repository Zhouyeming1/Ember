#!/usr/bin/env python3
"""样例数仓演示：一次跑通三个 SQL 工具。

    cd runtime-py && python3 examples/dw_demo/demo.py

不需要启动桌面端，也不调模型——直接构造工具环境、调工具函数、打印模型会看到的原文。
用来验证实现、录演示、或面试时当场跑。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # runtime-py
sys.path.insert(0, str(ROOT))

from emberpy.patches import PatchStore  # noqa: E402
from emberpy.permission import PermissionGate, PermissionMode  # noqa: E402
from emberpy.sql import SQLGLOT_AVAILABLE, get_index  # noqa: E402
from emberpy.tools import ToolEnv, default_registry  # noqa: E402

WORKSPACE = Path(__file__).resolve().parent


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    if not SQLGLOT_AVAILABLE:
        print("未安装 sqlglot，先跑：pip install sqlglot")
        return 1

    env = ToolEnv(
        workspace=WORKSPACE,
        gate=PermissionGate(PermissionMode("auto"), WORKSPACE),
        patches=PatchStore(),
    )
    reg = default_registry(env)
    index = get_index(WORKSPACE)
    print(f"样例数仓：{WORKSPACE}")
    print(f"  扫描到 {len(index.files)} 个 SQL 文件、{len(index.catalog)} 张表的建表语句")

    banner("① 反向影响面（表级）：改 dwd.dwd_order_detail 会波及谁")
    print(reg.get("sql_impact").fn(table="dwd.dwd_order_detail"))

    banner("② 反向影响面（字段级）：改 ods.ods_order_detail.amount 会一路波及到哪")
    print(reg.get("sql_impact").fn(table="ods.ods_order_detail.amount"))

    banner("③ 正向血缘：一个文件读了谁、写了谁、字段从哪来")
    print(reg.get("sql_lineage").fn(path="jobs/dws_order_summary_d.sql", level="column"))

    banner("④ 规范检查：干净的生产脚本")
    print(reg.get("sql_lint").fn(path="jobs/dws_user_gmv_d.sql"))

    banner("⑤ 规范检查：每个埋了一类问题的脚本")
    for path in sorted(WORKSPACE.joinpath("problems").glob("*.sql")):
        rel = path.relative_to(WORKSPACE).as_posix()
        print()
        print(reg.get("sql_lint").fn(path=rel))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
