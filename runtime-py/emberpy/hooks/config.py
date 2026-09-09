"""hooks 配置加载与分发。

配置来源（信任分层，对齐 claude"项目文件默认不被信任"的取向）：
- 用户级 ``<home>/.ember/hooks.json``：恒读（home = ``EMBER_HOME`` 优先，否则 ``~``，
  与 skills/web_search 的 home 解析一致）；
- 项目级 ``<workspace>/.ember/hooks.json``：仅在 ``EMBERPY_PROJECT_HOOKS=1`` 时并入。

文件形状：claude 现代式 ``{"hooks": {"PreToolUse": [{"matcher": "...", "hooks":
[{"type": "command", "command": "..."}]}]}}``；也接受事件名直接放顶层
（值是命令数组或带 matcher 的命令）的同款简写。

事件名用 claude 规范名（SessionStart/PreToolUse/PostToolUse/Stop…）。只实现
``type: command`` 一种 hook（prompt/agent/http 等其它类型本期不做，读到即记账
忽略并告警）。

解析宽容：文件不存在/坏 JSON/坏字段一律跳过并记进 warnings，不让一条坏配置
拖垮整个 worker。没有匹配 rule 时 run() 是纯 no-op（不 spawn 任何进程）。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .runner import DEFAULT_HOOK_TIMEOUT, HookCommandResult, HookRunOutcome, run_command
from ..paths import project_dot

# 本引擎实际会触发的规范事件名（配错名字 = 静默不触发，告警提示）
# R-G 增量：StopFailure（模型出错停止）、PermissionRequest（询问前可代答
# allow/deny/ask）、PermissionDenied（权限被拒后打点）。
_KNOWN_EVENTS = frozenset(
    {
        "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
        "PostToolUseFailure", "SubagentStart", "SubagentStop",
        "PreCompact", "PostCompact", "Stop", "StopFailure",
        "PermissionRequest", "PermissionDenied",
    }
)


def _ember_home() -> Path:
    env_home = os.environ.get("EMBER_HOME")
    return (Path(env_home).expanduser() if env_home else Path.home()).resolve()


def _normalize_event(name: Any) -> Optional[str]:
    """事件名规范化：去空白；非字符串/空 -> None。"""
    if not isinstance(name, str):
        return None
    key = name.strip()
    if not key:
        return None
    return key


@dataclass
class HookRule:
    """一条可执行的命令 hook（matcher 决定它对哪个工具名生效）。"""

    event: str
    command: str
    matcher: Optional[re.Pattern] = None   # None => 该事件下所有触发（工具事件=所有工具）
    timeout: float = DEFAULT_HOOK_TIMEOUT

    def matches(self, target: Optional[str]) -> bool:
        if self.matcher is None:
            return True
        if target is None:
            return False  # 有 matcher 但没给匹配目标（非工具事件）=> 不触发
        return self.matcher.search(str(target)) is not None


def _compile_matcher(raw: Any, event: str, warnings: list[str]) -> Optional[re.Pattern]:
    """把 matcher 编译成正则；无效正则会退化成字面量（不静默全匹配）。"""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return re.compile(raw)
    except re.error:
        warnings.append(f"事件 {event} 的 matcher {raw!r} 不是合法正则，按字面量匹配")
        return re.compile(re.escape(raw))


def _extract_command_def(entry: Any, event: str, warnings: list[str]) -> Optional[HookRule]:
    """从一条命令定义 dict 里抽 rule（只认 type: command；其余类型忽略并记账）。"""
    if not isinstance(entry, dict):
        return None
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    kind = entry.get("type")
    if isinstance(kind, str) and kind.strip() and kind.strip() != "command":
        warnings.append(f"事件 {event} 有非 command 类型的 hook（{kind.strip()}），本期不支持已忽略")
        return None
    timeout = DEFAULT_HOOK_TIMEOUT
    raw_timeout = entry.get("timeout")
    if isinstance(raw_timeout, (int, float)) and raw_timeout > 0:
        timeout = float(raw_timeout)
    return HookRule(event=event, command=command.strip(), timeout=timeout)


def _parse_file(path: Path) -> tuple[dict[str, list[HookRule]], list[str]]:
    """解析单个 hooks.json -> (event -> rules, warnings)。坏文件/空返回空。"""
    warnings: list[str] = []
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, warnings
    except (OSError, ValueError) as exc:
        warnings.append(f"读取 hooks 配置失败 {path}: {exc}")
        return {}, warnings
    if not isinstance(obj, dict):
        warnings.append(f"hooks 配置 {path} 顶层不是对象，已忽略")
        return {}, warnings

    # 支持 {"hooks": {...}} 现代外壳；否则直接当事件表
    if isinstance(obj.get("hooks"), dict):
        obj = obj["hooks"]
    if not isinstance(obj, dict):
        warnings.append(f"hooks 配置 {path} 事件表不是对象，已忽略")
        return {}, warnings

    rules: dict[str, list[HookRule]] = {}
    for raw_event, value in obj.items():
        event = _normalize_event(raw_event)
        if event is None:
            continue
        if event not in _KNOWN_EVENTS:
            warnings.append(f"hooks 配置 {path} 的事件 {raw_event!r} 不是已知事件，已忽略")
            continue
        # value 归一成一组组 {matcher?, hooks?/command}；三种形状都收
        groups: list[Any] = value if isinstance(value, list) else [value]
        for group in groups:
            matcher: Optional[re.Pattern] = None
            if isinstance(group, dict):
                matcher = _compile_matcher(group.get("matcher"), event, warnings)
                entries: list[Any]
                if isinstance(group.get("hooks"), list):
                    entries = group["hooks"]
                else:
                    entries = [group]  # 直接把 {type,command} 当一条命令定义
            else:
                entries = [group]
            for entry in entries:
                rule = _extract_command_def(entry, event, warnings)
                if rule is None:
                    continue
                rule.matcher = matcher
                rules.setdefault(event, []).append(rule)
    return rules, warnings


class HookManager:
    """按事件注册 command hook 并执行（无匹配规则 = 纯 no-op）。"""

    def __init__(self, rules: dict[str, list[HookRule]], warnings: Optional[list[str]] = None) -> None:
        self._rules = rules
        self.warnings = list(warnings or [])

    @classmethod
    def load(cls, workspace: Path, *, user_path: Optional[Path] = None,
             project_path: Optional[Path] = None,
             project_enabled: Optional[bool] = None) -> "HookManager":
        """从用户级（恒读）与项目级（需开关）hooks.json 装配。

        供测试直传路径；worker 场景用 load_hook_manager 读默认路径。
        """
        enabled = _project_hook_enabled() if project_enabled is None else project_enabled
        paths: list[tuple[Path, bool]] = []
        # 用户级恒读
        user_file = user_path if user_path is not None else _ember_home() / ".ember" / "hooks.json"
        paths.append((user_file, True))
        # 项目级：仅开关开启才并入
        if enabled:
            proj_file = project_path if project_path is not None else project_dot(Path(workspace).resolve(), "hooks.json")
            paths.append((proj_file, True))

        merged: dict[str, list[HookRule]] = {}
        warnings: list[str] = []
        for file_path, _always in paths:
            file_rules, file_warnings = _parse_file(file_path)
            warnings.extend(file_warnings)
            for event, event_rules in file_rules.items():
                merged.setdefault(event, []).extend(event_rules)
        return cls(merged, warnings)

    # -- 查询 ------------------------------------------------------------
    def has(self, event: str) -> bool:
        norm = _normalize_event(event)
        return bool(norm and self._rules.get(norm))

    def rules_for(self, event: str) -> list[HookRule]:
        norm = _normalize_event(event)
        return list(self._rules.get(norm, [])) if norm else []

    # -- 执行 ------------------------------------------------------------
    def run(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        matcher_target: Optional[str] = None,
        cwd: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
    ) -> HookRunOutcome:
        """跑该事件下全部匹配的 hook；首个阻塞即短路停止。

        没有匹配 rule（或事件没配）直接返回空 outcome——调用方先查 ``has`` 更省，
        这里兜底也不 spawn 进程。
        """
        outcome = HookRunOutcome(event=event)
        for rule in self.rules_for(event):
            if not rule.matches(matcher_target):
                continue
            result: HookCommandResult = run_command(
                rule.command,
                event=event,
                payload=payload,
                cwd=cwd,
                extra_env=extra_env,
                timeout=rule.timeout,
            )
            outcome.results.append(result)
            if result.blocked_reason:
                break
        return outcome


def _project_hook_enabled() -> bool:
    """项目级 hooks 默认关；只有显式 EMBERPY_PROJECT_HOOKS=1 才读。"""
    return os.environ.get("EMBERPY_PROJECT_HOOKS") == "1"


def load_hook_manager(workspace: Path) -> HookManager:
    """worker 装配入口：按默认路径 + env 门控读配置。"""
    return HookManager.load(workspace)
