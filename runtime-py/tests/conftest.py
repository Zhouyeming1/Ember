"""pytest 共享夹具。

FakeLLM / tool_call / make_agent 这类"测试替身"放在 emberpy.testing 里
（随包分发，深层目录也好 import），这里只留与 pytest 强绑定的夹具。
"""
from pathlib import Path

import pytest


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """带两个示例文件的临时工作区。"""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "hello.txt").write_text("你好，世界\n", encoding="utf-8", newline="\n")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text(
        "def greet():\n    return 'hi'\n\ndef main():\n    print(greet())\n",
        encoding="utf-8",
        newline="\n",
    )
    return root
