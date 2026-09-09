"""MCP 配置读取：mcpServers JSON → stdio server 列表；坏配置降级不崩。"""
from __future__ import annotations

import json
from pathlib import Path

from emberpy.mcp.config import McpServerConfig, MCP_CONFIG_ENV, manager_from_env, read_config


def _write(tmp_path: Path, data) -> str:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_read_basic(tmp_path: Path) -> None:
    path = _write(tmp_path, {"mcpServers": {"notes": {"command": "python", "args": ["a.py"], "env": {"X": "1"}}}})
    servers, warns = read_config(path)
    assert warns == []
    assert len(servers) == 1
    s = servers[0]
    assert s == McpServerConfig(name="notes", command="python", args=["a.py"], env={"X": "1"})


def test_missing_file_gives_warning(tmp_path: Path) -> None:
    servers, warns = read_config(str(tmp_path / "nope.json"))
    assert servers == []
    assert warns and "不存在" in warns[0]


def test_bad_json_gives_warning(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text("{ 不是 json", encoding="utf-8")
    servers, warns = read_config(str(path))
    assert servers == [] and len(warns) == 1


def test_missing_mcp_servers_key(tmp_path: Path) -> None:
    servers, warns = read_config(_write(tmp_path, {"foo": 1}))
    assert servers == [] and warns and "mcpServers" in warns[0]


def test_invalid_server_name_skipped(tmp_path: Path) -> None:
    data = {"mcpServers": {"bad name.with.dot": {"command": "python"}, "ok": {"command": "python"}}}
    servers, warns = read_config(_write(tmp_path, data))
    assert [s.name for s in servers] == ["ok"]
    assert any("不合法" in w for w in warns)


def test_remote_url_entry_skipped(tmp_path: Path) -> None:
    # 远程 url 型没有 command：最小客户端只做 stdio，跳过（有警告但不拦其它）
    data = {"mcpServers": {"remote": {"url": "https://x/mcp"}, "local": {"command": "python"}}}
    servers, warns = read_config(_write(tmp_path, data))
    assert [s.name for s in servers] == ["local"]
    assert any("非 stdio" in w for w in warns)


def test_env_expansion(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MCP_PY", "python3")
    data = {"mcpServers": {"a": {"command": "${MCP_PY}", "args": ["${MISSING_VAR:-fallback}.py"]}}}
    servers, _ = read_config(_write(tmp_path, data))
    assert servers[0].command == "python3"
    assert servers[0].args == ["fallback.py"]


def test_manager_from_env_absent_returns_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(MCP_CONFIG_ENV, raising=False)
    manager, warns = manager_from_env(str(tmp_path))
    assert manager is None and warns == []


def test_manager_from_env_bad_file_is_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(MCP_CONFIG_ENV, str(tmp_path / "missing.json"))
    manager, warns = manager_from_env(str(tmp_path))
    assert manager is None and len(warns) == 1
