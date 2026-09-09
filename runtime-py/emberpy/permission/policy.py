"""权限策略：deny 规则 + 内置敏感文件/目录集合（对齐 claude 权限语义）。

对齐点（claude utils/permissions/filesystem.ts）：
- 敏感集合是**内置**的、不可被配置绕开：路径任一段命中危险目录（.git/.vscode/
  .idea/.claude/.ember，大小写不敏感）或末段命中危险文件名单 => 在 full/auto 下
  也不能**静默**写——需要一次人工批准（无交互=拒绝，fail closed）。
- deny 规则是**无条件**拒绝：压过 confirm、压过任何权限模式（含 full），对齐
  claude "deny 免疫一切（连人工批准都救不回）"。

deny 来源两级（镜像 hooks 的信任分层，见 load_permission_policy）：
1. env ``EMBERPY_DENY``（JSON 数组）——恒读，CLI/测试注入，不碰磁盘；
2. 项目 ``<workspace>/.ember/permissions.json``（claude 兼容外壳
   ``{"permissions":{"deny":[...]}}``）——仅 ``EMBERPY_PROJECT_PERMISSIONS=1`` 并入。

匹配器极简：pattern 与"工作区相对 posix 小写路径"比对；``/`` 开头=锚定工作区根，
否则额外匹配末段文件名（方便写 ``.env`` / ``settings.json`` 这种"任意层级同名"规则）。
不实现 settings 多源 / gitignore 的多 root 分辨率（可裁，见 GAP-ANALYSIS R-B）。

工具级 allow/deny（permission/settings.py 落地，2026-09）：
- 项目 ``<workspace>/.claude/settings.json`` 的 ``permissions.allow/deny`` 在
  ``EMBERPY_PROJECT_SETTINGS=1`` 时并入 ``_tool_allow`` / ``_tool_deny``，与上面的
  路径 deny（``_deny``，只匹配文件路径）互不干扰、分别裁决。
- 规则文法 ``"Tool"`` / ``"Tool(pattern)"``；Tool 可写引擎名或 claude CamelCase，
  别名见 ``_TOOL_ALIAS_GROUPS``。deny 无条件（压过 full/confirm），allow 只供 gate
  豁免"普通询问"——敏感集合（.env/.git…）与 deny 都不可被 allow 绕过。
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path
from typing import Optional

from .settings import load_settings_rules
from ..paths import project_dot

# 危险目录：路径任一段命中（含路径本身就是这个目录）。大小写不敏感防
# Windows/macOS 反绕过。.ember = 引擎自配置目录（hooks.json / permissions.json）。
_DANGEROUS_DIRS = {".git", ".vscode", ".idea", ".claude", ".ember"}
# 危险文件名单：末段命中（对齐 claude DANGEROUS_FILES）。
# .env 单独在列：引擎内部 config 读取走它自己的通道（不经 gate），但 agent 经
# write_file 写 .env（凭据）不能静默——auto/full 下也要一次人工批准。读不受限
# （authorize_read 只查 deny，不查敏感集合）。.env.example 不含（脚手架常写）。
_DANGEROUS_FILES = {
    ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc",
    ".zprofile", ".profile", ".ripgreprc", ".mcp.json", ".claude.json", ".env",
}

# -- 工具级规则（settings.json 的 allow/deny） ----------------------------------
# 规则里 Tool 名 → 允许命中的工具集合。可写引擎名或 claude CamelCase；不在表内的
# 工具只认引擎名自身。写工具把 write_file/file_edit 归一组（claude 的 Write/Edit
# 同属"写文件"权限类别），规则写 Write(...) 或 Edit(...) 都会拦到两种引擎写工具。
_TOOL_ALIAS_GROUPS: dict[str, frozenset[str]] = {
    "run_command": frozenset({"run_command", "Bash"}),
    "read_file": frozenset({"read_file", "Read"}),
    "write_file": frozenset({"write_file", "file_edit", "Write", "Edit"}),
    "file_edit": frozenset({"write_file", "file_edit", "Write", "Edit"}),
    "web_search": frozenset({"web_search", "WebSearch"}),
    "fetch_content": frozenset({"fetch_content", "WebFetch"}),
}
# name -> 所属别名组（查表用；不在表内的名字 -> 单例 {name}）
_TOOL_NAME_LOOKUP: dict[str, frozenset[str]] = {}
for _names in _TOOL_ALIAS_GROUPS.values():
    for _n in _names:
        _TOOL_NAME_LOOKUP.setdefault(_n, _names)
# 规则里合法 Tool token：字母/下划线开头，后接字母数字 _ . -（MCP 工具名常有 . 与 -）
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")


def _tool_matches(rule_tool: str, call_tool: str) -> bool:
    """一条规则写的工具名 vs 一次实际调用的工具名：是否同一权限类别。"""
    rg = _TOOL_NAME_LOOKUP.get(rule_tool)
    cg = _TOOL_NAME_LOOKUP.get(call_tool)
    if rg is not None or cg is not None:
        return rg is not None and cg is not None and rg == cg
    return rule_tool == call_tool


def _parse_tool_rule(raw: object) -> Optional[tuple[str, Optional[str]]]:
    """把一条 settings 条目解析成 (tool, pattern)；pattern=None = 任意输入。

    文法：``Tool`` 或 ``Tool(pattern)``。坏格式（无闭合括号、工具名非法、
    空串）一律返回 None 丢弃——单条坏不连累整表，也不抛。
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if "(" in s:
        if not s.endswith(")"):
            return None
        open_i = s.find("(")
        tool = s[:open_i].strip()
        pat = s[open_i + 1:-1]
    else:
        if ")" in s:
            return None
        tool = s
        pat = None
    if not _NAME_RE.fullmatch(tool):
        return None
    return tool, pat


def _rule_hit(
    rules: list[tuple[str, Optional[str]]],
    tool_name: Optional[str],
    spec: Optional[str],
) -> bool:
    """给定已解析规则集 + 一次调用 (tool_name, spec)，是否命中任一条。

    pattern=None（裸 ``Tool``）= 任意输入都命中；否则 fnmatch 全匹配，或
    （pattern 无通配时）spec 以 pattern 开头——claude 的前缀规则语义
    （``Bash(python -m pytest`` 能放行 ``python -m pytest tests/``）。
    """
    if not rules or not tool_name:
        return False
    for rule_tool, pattern in rules:
        if not _tool_matches(rule_tool, tool_name):
            continue
        if pattern is None:
            return True
        if spec is None:
            continue
        if fnmatch.fnmatchcase(spec, pattern):
            return True
        if "*" not in pattern and "?" not in pattern and "[" not in pattern:
            if spec.startswith(pattern):
                return True
    return False


class PermissionPolicy:
    """一次 agent run 的权限策略：敏感集合常开 + 路径 deny + 工具级 allow/deny。

    policy 对象由 worker 装配一次，跨 agent / 子 agent 共享（deny 不能被子 agent
    绕过）。测试/纯库不传 -> gate 内部给默认空 policy（deny 为空，敏感集合仍生效）。
    """

    def __init__(
        self,
        root: Path,
        deny_patterns: Optional[list[str]] = None,
        *,
        tool_deny: Optional[list[object]] = None,
        tool_allow: Optional[list[object]] = None,
    ) -> None:
        self.root = Path(root).resolve()
        self._deny: list[str] = []
        for pat in deny_patterns or []:
            if not isinstance(pat, str):
                continue
            cleaned = pat.strip().replace("\\", "/").rstrip("/")
            if cleaned:  # "/" 单独等价空 -> 跳过（锚定根的语义保留在 leading /）
                self._deny.append(cleaned.lower())
        # 工具级规则：构造时解析，坏条目静默丢弃；不解析的原始串不进策略
        self._tool_deny: list[tuple[str, Optional[str]]] = [
            r for r in (_parse_tool_rule(x) for x in (tool_deny or [])) if r is not None
        ]
        self._tool_allow: list[tuple[str, Optional[str]]] = [
            r for r in (_parse_tool_rule(x) for x in (tool_allow or [])) if r is not None
        ]

    # -- deny（无条件拒绝） ------------------------------------------------
    def denies(self, path: Path) -> bool:
        """给定路径是否命中任意 deny 规则。只对工作区内路径生效。"""
        if not self._deny:
            return False
        rel = self._relative(path)
        if rel is None:
            return False
        name = rel.rsplit("/", 1)[-1]
        for raw in self._deny:
            if self._match(rel, name, raw):
                return True
        return False

    @staticmethod
    def _match(rel: str, name: str, raw: str) -> bool:
        anchored = raw.startswith("/")
        pat = raw.lstrip("/").replace("**", "*")
        # 具体路径名（无通配）命中目录时覆盖其整棵子树：deny "/build/" 应拦 build 内所有
        def _path(relpath: str) -> bool:
            if fnmatch.fnmatchcase(relpath, pat):
                return True
            if "*" not in pat and "?" not in pat and "[" not in pat:
                return relpath.startswith(pat + "/")
            return False
        if anchored:
            return _path(rel)
        # 未锚定：整条相对路径 + 末段文件名都试（".env" 对任意层级的 .env 生效）
        return _path(rel) or bool(fnmatch.fnmatchcase(name, pat))

    def _relative(self, path: Path) -> Optional[str]:
        """把路径折成工作区相对 posix 小写；越界返回 None。"""
        try:
            p = Path(path)
            if not p.is_absolute():
                p = self.root / p
            rel = os.path.relpath(p, self.root)
        except (ValueError, OSError):
            return None
        rel = rel.replace("\\", "/")
        if rel == ".." or rel.startswith("../"):
            return None
        return rel.lower()

    # -- 工具级规则（settings.json 的 allow/deny） -----------------------------
    def path_spec(self, path: Path) -> Optional[str]:
        """把路径折成工具规则匹配用的 spec（工作区相对 posix 小写；越界 None）。"""
        return self._relative(path)

    def denies_tool(self, tool_name: Optional[str], spec: Optional[str]) -> bool:
        """工具级 deny：无条件拒绝（gate 在任何模式之前调，压过 full/confirm）。"""
        return _rule_hit(self._tool_deny, tool_name, spec)

    def allows_tool(self, tool_name: Optional[str], spec: Optional[str]) -> bool:
        """工具级 allow：gate 只在"普通询问"分支前调，豁免询问但不豁免敏感/deny。"""
        return _rule_hit(self._tool_allow, tool_name, spec)

    # -- 敏感集合（full/auto 下仍要人工批准） --------------------------------
    def sensitive_reason(self, path: Path) -> Optional[str]:
        """路径落在内置敏感集合 => 返回一句原因（调用方转 ask）；否则 None。"""
        try:
            parts = [str(p) for p in Path(path).parts if p not in ("", "/", "\\")]
        except Exception:
            return None
        lowered = [p.lower() for p in parts]
        for idx, seg in enumerate(lowered):
            if seg in _DANGEROUS_DIRS and idx < len(lowered) - 1:
                # 目录段命中（文件本身是目录的话，idx 为最后一段也拦）
                return f"落在受保护目录 {parts[idx]}/"
        # 末段文件名命中危险名单
        if lowered and lowered[-1] in _DANGEROUS_FILES:
            return f"受保护文件 {parts[-1]}"
        # 路径本身就是一个危险目录（写进该目录？目录不是文件，写文件不会以它结尾，
        # 但把目录当目标出现在 rename 等场景仍要拦）
        if lowered and lowered[-1] in _DANGEROUS_DIRS:
            return f"受保护目录 {parts[-1]}/"
        return None


def _project_permissions_enabled() -> bool:
    """项目级 permissions.json 默认关；只有显式 EMBERPY_PROJECT_PERMISSIONS=1 才读。"""
    return os.environ.get("EMBERPY_PROJECT_PERMISSIONS") == "1"


def load_permission_policy(workspace: Path) -> PermissionPolicy:
    """worker 装配入口：读 env deny（恒读）+ 项目权限文件（env 门控）。

    - 路径 deny：env ``EMBERPY_DENY``（恒读）+ ``<workspace>/.ember/permissions.json``
      （``EMBERPY_PROJECT_PERMISSIONS=1``）。
    - 工具级规则：``<workspace>/.claude/settings.json`` 的 permissions.allow/deny
      （``EMBERPY_PROJECT_SETTINGS=1``，见 permission/settings.py）。

    敏感集合由 PermissionPolicy 常开，这里只收集模式/规则。坏 JSON / 文件不存在
    都静默 -> 只有 env deny 或空（不因一条坏配置炸掉 worker）。
    """
    patterns: list[str] = []
    env_deny = os.environ.get("EMBERPY_DENY")
    if env_deny:
        try:
            data = json.loads(env_deny)
            if isinstance(data, list):
                patterns.extend(str(x) for x in data)
        except ValueError:
            pass  # 坏 env JSON 忽略
    if _project_permissions_enabled():
        proj = project_dot(Path(workspace).resolve(), "permissions.json")
        try:
            obj = json.loads(proj.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            obj = None
        if isinstance(obj, dict):
            perms = obj.get("permissions")
            if isinstance(perms, dict) and isinstance(perms.get("deny"), list):
                patterns.extend(str(x) for x in perms["deny"])
            elif isinstance(obj.get("deny"), list):  # 宽容旧形状
                patterns.extend(str(x) for x in obj["deny"])
    tool_deny, tool_allow = load_settings_rules(workspace)
    return PermissionPolicy(
        Path(workspace),
        patterns,
        tool_deny=tool_deny,
        tool_allow=tool_allow,
    )
