"""read_file 对 .ipynb 的 markdown 化读取（R-H）。

.ipynb 原始是 JSON，模型几乎无法使用；read_file 应渲染成按 cell 排列的
视图（cell 源码 + 文本输出），解析失败/结构不符落回原文本。
"""
from __future__ import annotations

import json
from pathlib import Path

from emberpy.testing import make_agent, tool_call


def _nb_text() -> str:
    nb = {
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": ["# 标题\n", "说明"]},
            {
                "cell_type": "code",
                "metadata": {},
                "source": ["print(1 + 1)\n"],
                "outputs": [{"output_type": "stream", "name": "stdout", "text": ["2\n"]}],
            },
            {
                "cell_type": "code",
                "metadata": {},
                "source": [],
                "outputs": [
                    {"output_type": "execute_result", "data": {"text/plain": ["'ok'"]}, "metadata": {}}
                ],
            },
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    return json.dumps(nb, ensure_ascii=False)


def test_read_ipynb_renders_cells(workspace: Path) -> None:
    (workspace / "demo.ipynb").write_text(_nb_text(), encoding="utf-8")
    agent = make_agent(
        [
            {"content": None, "calls": [tool_call("c1", "read_file", {"path": "demo.ipynb"})]},
            {"content": "看到 notebook", "calls": None},
        ],
        workspace,
        mode="auto",
    )
    result = agent.run("读 demo.ipynb")
    assert result.steps == 1
    tool_payload = agent.session.messages()[-2]["content"]
    assert "Cell 1" in tool_payload and "# 标题" in tool_payload
    assert "Cell 2" in tool_payload and "print(1 + 1)" in tool_payload
    assert "[stdout]" in tool_payload and "2" in tool_payload
    # 原始 JSON 结构不应原样出现
    assert '"nbformat"' not in tool_payload


def test_bad_ipynb_falls_back_to_text(workspace: Path) -> None:
    (workspace / "broken.ipynb").write_text("这不是 JSON", encoding="utf-8")
    agent = make_agent(
        [
            {"content": None, "calls": [tool_call("c1", "read_file", {"path": "broken.ipynb"})]},
            {"content": "看到了", "calls": None},
        ],
        workspace,
        mode="auto",
    )
    agent.run("读 broken.ipynb")
    assert "这不是 JSON" in agent.session.messages()[-2]["content"]
