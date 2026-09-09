"""command hook 执行器：subprocess 喂 JSON、收集输出、解释决策。

行为对齐 claude hook 执行契约（借鉴语义，代码自写）：
- 命令用 shlex 切词成 argv，subprocess **不经 shell** 执行（安全：命令串按字面
  拆，不给 shell 注入机会）；
- stdin 喂该事件的 JSON payload；env 注入事件名与上下文路径（由调用方经 extra_env
  补全 HOOK_EVENT/CWD/PERMISSION_MODE/TRANSCRIPT_PATH 等）；
- 决策解释：
  - 退出码 2            -> 阻塞（blocking error），理由优先取 stdout JSON 的
    ``reason``，再取 stderr，最后取 stdout 文本；
  - stdout 是 JSON 对象 -> ``decision: "block"`` 或 ``continue: false`` 也算阻塞
    （reason 同优先级）；``modifiedPrompt`` 记为改写后的 prompt（UserPromptSubmit）；
  - 退出码 0             -> 成功，stdout 当"回灌文本"保留给上层展示；
  - 其它退出码（1/3/…）  -> 非阻塞错误，仅可见/记账，不拦流程。
- 超时：默认 60s（可被单条 rule 的 timeout 字段或调用方覆盖）；超时按阻塞级错误
  记（stderr 提示超时），subprocess.run 会杀掉子进程后抛 TimeoutExpired。
- stdout/stderr 截断保留（默认各 10k），防一条 hook 把整轮上下文打爆。
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional

MAX_KEEP = 10_000  # stdout/stderr 各自保留的字符上限
DEFAULT_HOOK_TIMEOUT = 60.0


def _is_windows() -> bool:
    return os.name == "nt"


def sys_encoding() -> str:
    """系统码页（解码非 UTF-8 子进程输出的回退口径）。"""
    return "mbcs" if _is_windows() else "utf-8"


def _split_windows(command: str) -> list[str]:
    """Windows 命令行分词：空格分隔、双引号分组、``\"`` 转义引号、反斜杠原样保留。

    shlex（POSIX 模式）在 Windows 上会把 ``C:\\dir`` 里的反斜杠当转义符吃掉，
    这里用接近 cmd 规则的自写分词代替：只认空白与成对双引号，路径反斜杠不动。
    """
    tokens: list[str] = []
    cur: list[str] = []
    in_quote = False
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and i + 1 < n and command[i + 1] == '"':
            cur.append('"')
            i += 2
            continue
        if ch == '"':
            in_quote = not in_quote
            i += 1
            continue
        if not in_quote and ch in " \t":
            if cur:
                tokens.append("".join(cur))
                cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    if cur:
        tokens.append("".join(cur))
    return tokens


def split_command(command: str) -> list[str]:
    """把一条 hook 命令切成 argv（不经 shell 解释）。

    POSIX 用 shlex 规则；Windows 用自写规则（保路径反斜杠、双引号分组）。
    """
    if _is_windows():
        return _split_windows(command)
    return shlex.split(command)


def quote_command(*argv: str) -> str:
    """把 argv 组回一条 hook 命令串（与 split_command 语义互逆；测试/文档用）。

    POSIX 交给 shlex.join；Windows 用双引号 + ``\\``/``\\"`` 转义补齐。
    """
    if not _is_windows():
        return shlex.join(argv)
    parts: list[str] = []
    for token in argv:
        # 只把双引号转义成 \"；反斜杠原样保留（_split_windows 也按字面读反斜杠，
        # 只有 \" 是转义对）。Windows 路径里的 \U、\p 等不会被吞。
        if not token or any(c in token for c in ' \t"'):
            parts.append('"' + token.replace('"', '\\"') + '"')
        else:
            parts.append(token)
    return " ".join(parts)


def _truncate(text: str, limit: int = MAX_KEEP) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(hook 输出过长，已截断)"


def _decode(raw: bytes) -> str:
    """字节流解码：优先 UTF-8（子进程继承 PYTHONUTF8 时是 UTF-8），失败退回系统码页。"""
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return raw.decode(sys_encoding(), errors="replace").strip()


def _stdout_json(stdout: str) -> Optional[dict[str, Any]]:
    """把 stdout 当 JSON 对象解释：整段或首行，解析出 dict 才返回。

    对齐 claude 的 sync response：hook 常把 ``{"decision": ...}`` 之类打成 JSON。
    解析不出来就按普通文本对待（不回灌结构化字段）。
    """
    text = stdout.strip()
    for candidate in (text, text.splitlines()[0].strip() if text else ""):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


@dataclass
class HookCommandResult:
    """单条 command hook 的执行与解释结果。"""

    command: str            # 原命令串（展示用）
    exit_code: int          # 子进程退出码；-1 = 没能启动 / 被超时杀掉
    stdout: str             # 截断后的 stdout（同时是"成功回灌文本"）
    stderr: str             # 截断后的 stderr
    timed_out: bool = False
    json: Optional[dict[str, Any]] = None   # 解释出的 stdout JSON（有才非 None）
    blocked_reason: Optional[str] = None    # 非 None => 本 hook 阻塞
    modified_prompt: Optional[str] = None   # stdout JSON 里的 modifiedPrompt
    decision: Optional[str] = None          # stdout JSON decision: allow/deny/ask（PermissionRequest）
    note: str = ""                          # 供上层展示/记账的一句话

    @property
    def visible_text(self) -> str:
        """给界面/转录展示的正文（成功回灌 stdout；阻塞回 reason）。"""
        if self.blocked_reason:
            return self.blocked_reason
        return self.stdout


@dataclass
class HookRunOutcome:
    """一次事件里全部匹配 command hook 的聚合结果。"""

    event: str
    results: list[HookCommandResult] = field(default_factory=list)

    @property
    def any_run(self) -> bool:
        return bool(self.results)

    @property
    def blocked_reason(self) -> Optional[str]:
        for result in self.results:
            if result.blocked_reason:
                return result.blocked_reason
        return None

    @property
    def modified_prompt(self) -> Optional[str]:
        for result in self.results:
            if result.modified_prompt:
                return result.modified_prompt
        return None

    @property
    def texts(self) -> list[str]:
        """所有 hook 的可见文本（逐条，供展示/打点）。"""
        return [r.visible_text for r in self.results if r.visible_text]

    @property
    def notes(self) -> list[str]:
        return [r.note for r in self.results if r.note]

    @property
    def decision(self) -> Optional[str]:
        """聚合 decision：第一条非空的 allow/deny/ask（PermissionRequest 裁决用）。"""
        for result in self.results:
            if result.decision in ("allow", "deny", "ask"):
                return result.decision
        return None


def _reason_from(json_obj: Optional[dict[str, Any]], stdout: str, stderr: str) -> str:
    """阻塞理由优先级：stdout JSON 的 reason > stderr > stdout 文本。"""
    if json_obj and isinstance(json_obj.get("reason"), str) and json_obj["reason"].strip():
        return json_obj["reason"].strip()
    if stderr:
        return _truncate(stderr)
    return _truncate(stdout) if stdout else "hook 未给出理由"


def run_command(
    command: str,
    *,
    event: str,
    payload: dict[str, Any],
    cwd: Optional[str] = None,
    extra_env: Optional[dict[str, str]] = None,
    timeout: float = DEFAULT_HOOK_TIMEOUT,
) -> HookCommandResult:
    """执行一条 command hook 并解释结果（不抛错：启动/超时都折叠进结果）。

    extra_env 里的键会覆盖进程继承到的同名环境变量；event 恒以 HOOK_EVENT 注入。
    """
    result = HookCommandResult(command=command, exit_code=-1, stdout="", stderr="", note="")
    try:
        argv = split_command(command)
    except ValueError as exc:
        result.stderr = f"命令切词失败（不是合法 argv）：{exc}"
        result.blocked_reason = result.stderr
        result.note = f"[hook {event}] 命令无法解析，已阻塞"
        return result
    if not argv:
        result.stderr = "空命令：rule 没有 command"
        result.blocked_reason = result.stderr
        result.note = f"[hook {event}] 空命令，已阻塞"
        return result

    env = os.environ.copy()
    env["HOOK_EVENT"] = event
    # 让 python 子进程的 stdin/stdout 统一走 UTF-8（Windows 上 locale 码页是 GBK 时，
    # 子进程 json.load(sys.stdin) / print 才不会因字节编码猜错而崩）。
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra_env:
        env.update({k: v for k, v in extra_env.items() if v is not None})
    stdin_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    try:
        proc = subprocess.run(
            argv,
            input=stdin_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            timeout=max(0.1, timeout),
        )
    except FileNotFoundError:
        # hook 本体跑不起来（缺可执行文件）：PreToolUse 等闸门语义下应 fail-closed，
        # 视作阻塞级配置错误，交给上层决定拦不拦。
        result.stderr = f"找不到要执行的可执行文件（命令切词后首词不可用）：{argv[0]}"
        result.blocked_reason = result.stderr
        result.note = f"[hook {event}] 找不到可执行文件：{argv[0]}"
        return result
    except subprocess.TimeoutExpired:
        result.stderr = f"hook 超过 {timeout:g}s 未结束，已终止"
        result.timed_out = True
        result.note = f"[hook {event}] 超时"
        return result
    except OSError as exc:
        result.stderr = f"执行 hook 失败：{exc}"
        result.note = f"[hook {event}] 启动失败"
        return result

    stdout = _decode(proc.stdout)
    stderr = _decode(proc.stderr)
    result.exit_code = proc.returncode
    result.stdout = stdout
    result.stderr = stderr
    result.json = _stdout_json(stdout)
    json_obj = result.json

    # stdout JSON 的结构化字段（对任意事件都先抽出来）
    modified = json_obj.get("modifiedPrompt") if json_obj else None
    if isinstance(modified, str) and modified.strip():
        result.modified_prompt = modified.strip()

    # PermissionRequest 的裁决字段：decision: allow/deny/ask（允许 hook 代答）
    raw_decision = json_obj.get("decision") if json_obj else None
    if isinstance(raw_decision, str) and raw_decision in ("allow", "deny", "ask"):
        result.decision = raw_decision

    decision_block = bool(
        json_obj
        and (json_obj.get("decision") == "block" or json_obj.get("continue") is False)
    )
    if proc.returncode == 2 or decision_block:
        result.blocked_reason = _reason_from(json_obj, stdout, stderr)
        result.note = f"[hook {event}] 阻塞：{result.blocked_reason[:120]}"
    elif proc.returncode == 0:
        result.note = f"[hook {event}] 成功"
    else:
        result.note = f"[hook {event}] 非阻塞错误：退出码 {proc.returncode}"
    return result
