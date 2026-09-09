"""command hook 执行器单元测试：决策语义（退出码/JSON）、stdin payload、超时。

用 ``python -c`` 当假 hook（真实 subprocess、跨平台路径经 quote_command 保证），
覆盖：成功回灌文本、退出码 2 阻塞、stdout JSON 的 decision:block / continue:false、
modifiedPrompt、非阻塞错误码、超时、空命令、可执行文件缺失。
"""
from __future__ import annotations

import json
import sys

from emberpy.hooks.runner import quote_command, run_command


def _cmd(code: str) -> str:
    return quote_command(sys.executable, "-c", code)


def _run(code: str, *, timeout: float = 30.0):
    return run_command(_cmd(code), event="PreToolUse", payload={"tool_name": "x"}, timeout=timeout)


def test_exit0_stdout_is_visible_text() -> None:
    r = _run("print('一切正常')")
    assert r.exit_code == 0
    assert r.blocked_reason is None
    assert r.visible_text == "一切正常"
    assert r.modified_prompt is None


def test_stdin_payload_received_as_json() -> None:
    code = (
        "import sys, json; d = json.load(sys.stdin); "
        "print('got=' + d['tool_name'] + '/' + str(d['event_is_ok']))"
    )
    payload = {"tool_name": "write_file", "event_is_ok": 1}
    r = run_command(_cmd(code), event="PostToolUse", payload=payload, timeout=30.0)
    assert r.exit_code == 0
    assert r.stdout == "got=write_file/1"


def test_exit_code_2_blocks_with_stderr() -> None:
    code = "import sys; print('禁止写文件', file=sys.stderr); sys.exit(2)"
    r = _run(code)
    assert r.exit_code == 2
    assert r.blocked_reason == "禁止写文件"
    assert "阻塞" in r.note


def test_stdout_json_decision_block() -> None:
    code = "import json; print(json.dumps({'decision': 'block', 'reason': '用户要求先确认'}))"
    r = _run(code)
    assert r.exit_code == 0
    assert r.blocked_reason == "用户要求先确认"


def test_stdout_json_continue_false_blocks() -> None:
    code = "import json; print(json.dumps({'continue': False, 'reason': '不允许'}))"
    r = _run(code)
    assert r.blocked_reason == "不允许"


def test_stdout_json_modified_prompt() -> None:
    code = "import json; print(json.dumps({'modifiedPrompt': '改写后的任务'}))"
    r = _run(code)
    assert r.exit_code == 0
    assert r.blocked_reason is None
    assert r.modified_prompt == "改写后的任务"


def test_other_exit_code_is_non_blocking_error() -> None:
    code = "import sys; print('boom'); sys.exit(1)"
    r = _run(code)
    assert r.exit_code == 1
    assert r.blocked_reason is None
    assert "非阻塞" in r.note
    assert r.stdout == "boom"


def test_timeout_marks_and_kills() -> None:
    code = "import time; time.sleep(5)"
    r = _run(code, timeout=0.3)
    assert r.timed_out is True
    assert r.blocked_reason is None
    assert "超时" in r.note


def test_empty_and_missing_executable() -> None:
    empty = run_command("", event="Stop", payload={}, timeout=30.0)
    assert empty.blocked_reason is not None and "空命令" in empty.note

    missing = run_command("definitely-not-a-real-exe-xyz123 --flag", event="Stop", payload={}, timeout=30.0)
    assert missing.exit_code == -1
    assert missing.blocked_reason is not None
    assert "找不到" in missing.note
