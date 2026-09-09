"""Feature B（2026-09）：shell 大输出全文落盘到 .ember/out + read_file 分段读回。

覆盖：
- 超 MAX_OUTPUT 时：返回文本含 .ember/out/shell-*.log 指针，中间段被截掉；
- 落盘文件 = 完整原文（含被截的中间段），可用 read_file offset/limit 按行读回；
- 未超限：纯截断/正常返回，不产生落盘文件；
- 落盘目录不可写（.ember 被占成文件）：退回纯截断、不抛、不破坏命令结果；
- 端到端：真实 run_shell 跑一条超限输出命令，指针 + 落盘文件真实存在。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from emberpy.patches import PatchStore
from emberpy.permission import PermissionGate, PermissionMode
from emberpy.tools import ToolEnv, default_registry
from emberpy.tools.shell import MAX_OUTPUT, _combine, run_shell

_SPILL_RE = re.compile(r"\.ember/out/(shell-[0-9a-f]{8}\.log)")


def _env(workspace: Path):
    gate = PermissionGate(PermissionMode.AUTO, workspace)
    patches = PatchStore()
    env = ToolEnv(workspace=workspace, gate=gate, patches=patches)
    return default_registry(env)


def _multiline_over_max() -> str:
    """构造 >MAX_OUTPUT 的多行文本，行 200 处放哨兵（落在"被截掉的中间段"）。"""
    lines: list[str] = []
    for i in range(500):
        lines.append(f"L{i:04d}-" + "y" * 294)  # 每行约 300 字符，总 ~150k > 80k
    lines[200] = f"L0200-SENTINEL_IN_MIDDLE-" + "y" * 280
    return "\n".join(lines)


class TestCombineSpill:
    def test_over_max_spills_full_file_and_points(self, tmp_path: Path) -> None:
        ws = tmp_path
        content = _multiline_over_max()
        out = _combine(content, "", "", spill_to=ws)
        # 中间哨兵被截掉，但返回里有落盘指针
        assert "SENTINEL_IN_MIDDLE" not in out
        assert ".ember/out/shell-" in out
        # 落盘文件 = 完整原文（含中间段）
        logs = list((ws / ".ember" / "out").glob("*.log"))
        assert len(logs) == 1
        assert logs[0].read_text(encoding="utf-8") == content
        # 指针给出的相对路径与真实文件一致（group(0) = 完整 ".ember/out/shell-x.log"）
        rel = _SPILL_RE.search(out).group(0)
        assert (ws / rel) == logs[0]

    def test_readback_via_read_file(self, tmp_path: Path) -> None:
        ws = tmp_path
        content = _multiline_over_max()
        out = _combine(content, "", "", spill_to=ws)
        rel = _SPILL_RE.search(out).group(0)
        reg = _env(ws)
        # 中间段（200 行处）在截断预览里没有，但 read_file 能按行读回
        assert "SENTINEL_IN_MIDDLE" not in out
        got = reg.get("read_file").fn(path=rel, offset=200, limit=2)
        assert "SENTINEL_IN_MIDDLE" in got
        assert "200:" in got or "201:" in got  # 带真实行号
        # 头部也能读回
        head = reg.get("read_file").fn(path=rel, offset=0, limit=1)
        assert "L0000-" in head

    def test_under_max_no_spill(self, tmp_path: Path) -> None:
        ws = tmp_path
        out = _combine("hello", "", "", spill_to=ws)
        assert out == "hello"
        assert not (ws / ".ember").exists()  # 没超限就不建目录

    def test_spill_failure_falls_back_to_plain_truncation(self, tmp_path: Path) -> None:
        ws = tmp_path
        (ws / ".ember").write_text("占位：让 .ember 不是目录", encoding="utf-8")
        content = _multiline_over_max()
        out = _combine(content, "", "", spill_to=ws)  # mkdir 失败 -> 不抛、无指针
        assert ".ember/out/" not in out
        assert "输出过大" in out


class TestRunCommandSpill:
    def test_real_command_spills(self, tmp_path: Path) -> None:
        ws = tmp_path
        # 用 venv python 输出 100k 字符（跨平台，不经 console 截断：走 pipe）
        cmd = f'"{sys.executable}" -c "import sys; sys.stdout.write(\'Z\' * 100000)"'
        out = run_shell(cmd, ws)
        assert ".ember/out/shell-" in out, out[-300:]
        logs = list((ws / ".ember" / "out").glob("*.log"))
        assert len(logs) == 1
        assert logs[0].stat().st_size == 100000
        # 指针相对路径模型可读：read_file 读回开头
        rel = _SPILL_RE.search(out).group(0)
        reg = _env(ws)
        assert "Z" * 10 in reg.get("read_file").fn(path=rel, offset=0, limit=1)
