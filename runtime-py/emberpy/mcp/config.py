"""MCP server 配置：从 JSON 文件读 stdio server 定义。

配置文件形如 claude 的 ``.mcp.json``（沿用其 ``mcpServers`` 字段形状，方便
直接复用已有配置）：

.. code-block:: json

    {
      "mcpServers": {
        "notes": {
          "command": "python",
          "args": ["server.py"],
          "env": {"FOO": "bar"}
        }
      }
    }

只认 stdio 形态（command 非空）；带 ``url`` 的远程条目在这里直接跳过
（emberpy 最小客户端只做本地子进程）。server 名限制与 claude /mcp add
一致：``^[a-zA-Z0-9_-]+$``，非法名该条忽略并记 warning，不让坏配置打断启动。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
MCP_CONFIG_ENV = "EMBERPY_MCP_CONFIG"  # 指向 JSON 配置文件；不设 = MCP 整体关闭


@dataclass(frozen=True)
class McpServerConfig:
    """一条 stdio MCP server 的定义。"""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def _expand_env(value: str) -> str:
    """展开 ${VAR} / ${VAR:-default}；缺变量保留原文（警告由上层给）。"""
    def _sub(match: re.Match[str]) -> str:
        name, _, default = match.group(1).partition(":-")
        if name in os.environ:
            return os.environ[name]
        return default if match.group(0).find(":-") >= 0 else match.group(0)

    return re.sub(r"\$\{([^}]+)\}", _sub, value)


def read_config(path: str | os.PathLike[str]) -> tuple[list[McpServerConfig], list[str]]:
    """读配置文件 → (可用 server 列表, 警告列表)。任何坏条目不抛异常，只记警告。

    文件缺失/JSON 非法 → (空, [致命警告])，调用方可据此决定整体降级为不用 MCP。
    """
    warnings: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return [], [f"MCP 配置文件不存在：{path}"]
    except (OSError, ValueError) as exc:
        return [], [f"MCP 配置文件解析失败：{exc}"]

    if not isinstance(data, dict) or not isinstance(data.get("mcpServers"), dict):
        return [], [f"MCP 配置缺少 mcpServers 对象：{path}"]

    servers: list[McpServerConfig] = []
    for name, spec in data["mcpServers"].items():
        if not _NAME_RE.match(str(name)):
            warnings.append(f"MCP server 名 {name!r} 不合法（只允许字母数字 _ -），已忽略")
            continue
        if not isinstance(spec, dict):
            warnings.append(f"MCP server {name!r} 配置不是对象，已忽略")
            continue
        command = spec.get("command")
        if not isinstance(command, str) or not command.strip():
            # 远程 url 型 / 缺 command 的条目：emberpy 只做 stdio，跳过不报错
            warnings.append(f"MCP server {name!r} 无 command（非 stdio，忽略）")
            continue
        args = spec.get("args") if isinstance(spec.get("args"), list) else []
        args = [str(a) for a in args if isinstance(a, str)]
        env_map = spec.get("env") if isinstance(spec.get("env"), dict) else {}
        env = {str(k): str(v) for k, v in env_map.items() if isinstance(v, str)}
        servers.append(
            McpServerConfig(
                name=str(name),
                command=_expand_env(command),
                args=[_expand_env(a) for a in args],
                env={k: _expand_env(v) for k, v in env.items()},
            )
        )
    return servers, warnings


def manager_from_env(cwd: str) -> tuple[Optional["McpManager"], list[str]]:
    """按 EMBERPY_MCP_CONFIG 环境变量建 McpManager；未配置 → (None, [])。

    返回值放 (manager, warnings)：调用方把 warnings 打到日志即可，无配置时
    整体返回 None（不建对象、不 spawn 任何进程），保证默认行为零变化。
    """
    from .manager import McpManager  # 延迟导入避免循环

    cfg = os.environ.get(MCP_CONFIG_ENV)
    if not cfg:
        return None, []
    servers, warnings = read_config(cfg)
    if not servers:
        return None, warnings
    return McpManager(servers=servers, cwd=cwd), warnings
