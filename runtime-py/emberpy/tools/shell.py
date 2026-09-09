"""shell 命令工具。

- 执行前必须通过权限门（危险命令拦截在 permissions.py）
- 超时后尝试杀整棵进程树，避免留下孤儿进程
- 输出有上限并保留头尾，防止几百 MB 输出灌爆上下文
- 超限（>MAX_OUTPUT）时把**全文**落盘项目级 ``<cwd>/.ember/out/shell-*.log``
  并在返回里附工作区相对路径：模型需要中间被截掉的内容时，可用
  read_file 按 offset/limit 分段读回（读 spill 只受 deny 约束，read_file 在工作区内即可读）。
  写盘失败退回纯截断。
- v1 用字符串命令 + 系统默认 shell（Windows 为 cmd，其它为 sh）
"""
from __future__ import annotations

import os
import signal
import subprocess
import uuid
from pathlib import Path
from typing import Any

from ..permission import PermissionGate
from .registry import Tool, ToolCategory, ToolEnv
from ..paths import project_dot

MAX_OUTPUT = 80_000          # 字符上限
DEFAULT_TIMEOUT = 120        # 秒


def _spawn_kwargs() -> dict[str, Any]:
    """尽量让子进程独立成组，便于超时后整组杀掉。"""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _terminate_tree(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


def run_shell(command: str, cwd: Path, timeout: float = DEFAULT_TIMEOUT) -> str:
    """执行命令并返回（截断后的）stdout+stderr。"""
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_spawn_kwargs(),
        )
    except OSError as exc:
        return f"错误：无法启动命令：{exc}"

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            stdout, stderr = "", ""
        return _combine(
            stdout or "",
            stderr or "",
            f"\n[命令超过 {timeout}s，已终止]",
            spill_to=cwd,
        )

    code = proc.returncode
    note = "" if code == 0 else f"\n[退出码 {code}]"
    body = stdout or "" if code == 0 else (stdout or "") + (stderr or "")
    if not body.strip() and code == 0:
        body = "(无输出)"
    return _combine(stdout or "", stderr or "", note, spill_to=cwd)


def _spill(text: str, cwd: Path) -> str | None:
    """全文超限时把 text 写到项目级 spill 目录（.ember/out），返回相对路径。

    放引擎自有目录的原因：该目录在工作区内 -> 模型的 read_file 能读回（read_file 只查
    deny、不查敏感集合，.ember 默认在 deny/敏感之外）；又属引擎自有目录，
    不会被 agent 误写。写盘失败（磁盘/权限）返回 None，调用方退回纯截断，
    绝不因落盘失败弄坏命令结果。
    """
    out_dir = project_dot(cwd, "out")
    name = f"shell-{uuid.uuid4().hex[:8]}.log"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / name).write_text(text, encoding="utf-8", newline="\n")
    except OSError:
        return None
    return os.path.relpath(out_dir / name, cwd).replace("\\", "/")


def _combine(
    stdout: str,
    stderr: str,
    note: str,
    spill_to: Path | None = None,
) -> str:
    parts: list[str] = []
    if stdout.strip():
        parts.append(stdout.rstrip())
    if stderr.strip():
        parts.append("[stderr]\n" + stderr.rstrip())
    text = "\n".join(parts) + note
    if len(text) > MAX_OUTPUT:
        full = text  # 落盘要的是全文（含被截掉的中间段），不是截断后的预览
        head = full[: MAX_OUTPUT // 2]
        tail = full[-(MAX_OUTPUT // 2):]
        omit = len(full) - MAX_OUTPUT
        text = f"{head}\n……[输出过大，中间省略 {omit} 字符]……\n{tail}"
        if spill_to is not None:
            rel = _spill(full, spill_to)
            if rel is not None:
                text = (
                    f"{text}\n\n[全文已落盘：{rel}（{len(full)} 字符）；"
                    f"需要时用 read_file 读 {rel}，offset/limit 按行续读]"
                )
    return text


def build_shell_tool(env: ToolEnv) -> Tool:
    gate: PermissionGate = env.gate

    def run_command(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
        # Denied 不在这里吞：向上抛给 agent._execute 统一转成"权限拒绝"工具结果并
        # 触发 PermissionDenied hook（R-G③），否则 denied 标志恒 False、hook 不触发。
        gate.authorize_command(command, env.confirm)
        try:
            return run_shell(command, env.workspace, timeout=float(timeout))
        except Exception as exc:  # 兜底：任何异常都变成文本回给模型
            return f"执行出错：{exc}"

    return Tool(
        name="run_command",
        description=(
            "在工作区里执行一条 shell 命令（git 常用命令、运行测试/构建等）。"
            "命令输出最多保留前 40KB 与后 40KB；超过时全文会落盘 .ember/out/shell-*.log "
            "并在输出末尾附路径，需要被截掉的中间内容时用 read_file 按行读该文件。"
            "破坏性命令会被权限系统拦截或要求确认。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "timeout": {
                    "type": "integer",
                    "description": "超时秒数",
                    "default": DEFAULT_TIMEOUT,
                },
            },
            "required": ["command"],
        },
        category=ToolCategory.SHELL,
        fn=run_command,
    )
