"""自定义 agent 定义文件（R-E）：.claude/agents/*.md -> 可派生子 agent 的专用角色。

对齐 claude-code-analysis ``tools/AgentTool/loadAgentsDir.ts`` 的接口意图：
每个定义文件 = frontmatter（名字/描述/工具过滤/权限模式/步数上限/初始 prompt）+
正文（当作该子 agent 的 system prompt）。引擎在默认 general/explore 之上，把
项目里 .claude/agents/（及 .ember/agents/）下的定义都扫进来，``run_agent`` 的
agent_type 除了 general/explore，还能填任意已发现的 agent 名。

有意裁剪（与 claude 差异，记账在 GAP-ANALYSIS R-E 行）：model 覆盖、skills 集合、
memory scope、worktree 隔离、effort、hooks 子集、背景压缩等字段本轮不做——引擎
子 agent 固定共享父会话的模型与补丁账本，模式/工具过滤/步数/初始 prompt 是可落地
且有价值的最小集。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..skills.frontmatter import fm_first_line, fm_str, fm_str_list, parse_frontmatter

# 项目级根下的 agent 子目录；user 级根 = home 下同名子目录（最后扫，同文件让位项目）。
AGENTS_PROJECT_SUBDIRS = (".claude/agents", ".ember/agents")
AGENTS_USER_SUBDIRS = (".claude/agents", ".ember/agents")


@dataclass(frozen=True)
class AgentDef:
    name: str                 # run_agent 的 agent_type 可填这个名字
    description: str          # 一句话（用于工具描述/界面）
    body: str                 # frontmatter 之后的正文 -> 子 agent 的 system prompt
    path: Path
    source: str               # project / user
    tools: tuple[str, ...] = ()           # 白名单：只给这些引擎工具名（空=不限）
    disallowed_tools: tuple[str, ...] = ()  # 黑名单：从池里拿掉
    model: str = ""            # 解析但本轮不覆盖模型（记账：模型覆盖=裁）
    max_turns: int = 0         # 0 = 继承 Agent 默认 max_steps
    permission_mode: str = ""  # 空 = 继承父权限模式；plan/ask/auto/full 可覆盖
    initial_prompt: str = ""   # 每次派生时前置在 run_agent prompt 之前的固定指令

    @property
    def filters_tools(self) -> bool:
        return bool(self.tools or self.disallowed_tools)


@dataclass
class AgentStore:
    """agent 定义注册表：按根顺序扫、同名去重、可 refresh 动态发现。"""

    roots: list[Path]
    _defs: list[AgentDef] = field(default_factory=list)
    _by_name: dict[str, AgentDef] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.roots = list(self.roots)
        self._defs = load_agent_roots(self.roots)
        self._rebuild()

    def _rebuild(self) -> None:
        self._by_name = {}
        for adef in self._defs:
            if adef.name not in self._by_name:
                self._by_name[adef.name] = adef

    def refresh(self) -> None:
        """重扫根，让磁盘上新增/删除的 agent 定义即时可见（Agent.run 每轮开头调）。"""
        self._defs = load_agent_roots(self.roots)
        self._rebuild()

    @classmethod
    def from_workspace(cls, workspace: Path) -> "AgentStore":
        return cls(resolve_agent_roots(workspace))

    # -- 读 ----------------------------------------------------------------
    def all(self) -> list[AgentDef]:
        return list(self._defs)

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def get(self, name: str) -> AgentDef | None:
        if not name:
            return None
        exact = self._by_name.get(name)
        if exact is not None:
            return exact
        # 容错：只给尾段（不带 : 命名空间）且唯一时也认
        matches = [d for d in self._defs if d.name.rsplit(":", 1)[-1] == name]
        uniq = {d.name: d for d in matches}
        if len(uniq) == 1:
            return next(iter(uniq.values()))
        return None

    def listing(self) -> str:
        """给 run_agent 描述用的清单：``- name: desc``（限长）。"""
        lines: list[str] = []
        for adef in self._defs:
            desc = " ".join(adef.description.split())
            if len(desc) > 160:
                desc = desc[:157] + "..."
            lines.append(f"- {adef.name}: {desc}")
        return "\n".join(lines) if lines else "（没有自定义 agent）"


def _read_utf8(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _def_from_file(path: Path, source: str) -> Optional[AgentDef]:
    text = _read_utf8(path)
    if text is None or not text.strip():
        return None
    data, body = parse_frontmatter(text)
    if not body.strip():
        return None
    stem = path.name[: -len(".md")] if path.name.endswith(".md") else path.name
    name = fm_str(data, "name") or stem
    description = fm_str(data, "description") or fm_first_line(body, name)
    max_turns_raw = fm_str(data, "maxturns")  # frontmatter 键一律小写（disallowedTools -> disallowedtools）
    try:
        max_turns = int(max_turns_raw) if max_turns_raw else 0
    except ValueError:
        max_turns = 0
    return AgentDef(
        name=name,
        description=description,
        body=body,
        path=path.resolve(),
        source=source,
        tools=fm_str_list(data, "tools"),
        disallowed_tools=fm_str_list(data, "disallowedtools"),
        model=fm_str(data, "model"),
        max_turns=max_turns if max_turns > 0 else 0,
        permission_mode=fm_str(data, "permissionmode").strip().lower(),
        initial_prompt=fm_str(data, "initialprompt"),
    )


def scan_root(root: Path, source: str) -> list[AgentDef]:
    """扫一个 agent 根目录：顶层每个 *.md 是一个 agent 定义（不递归）。"""
    root = root.resolve()
    if not root.is_dir():
        return []
    defs: list[AgentDef] = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        entries = []
    for child in entries:
        if not child.is_file() or not child.name.endswith(".md"):
            continue
        adef = _def_from_file(child, source)
        if adef is not None:
            defs.append(adef)
    return defs


def _under(p: Path, base: Path) -> bool:
    return p == base or base in p.parents


def _existing(dirs: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for d in dirs:
        rp = str(d.resolve())
        if d.is_dir() and rp not in seen:
            seen.add(rp)
            out.append(d)
    return out


def resolve_agent_roots(workspace: Path, home_base: Optional[Path] = None) -> list[Path]:
    """给出要扫描的 agent 根目录（已存在、去重、项目>user 优先级）。

    与 skills 的 resolve_skill_roots 同构：``EMBERPY_AGENTS_DIR`` 整体覆盖；
    否则 project 根 = workspace 及其（home 之内）每个祖先下的 AGENTS_PROJECT_SUBDIRS，
    就近优先；user 根 = home_base 下 .claude/agents / .ember/agents（最后扫）。
    home_base 缺省 = EMBER_HOME（设了就当作 home，测试隔离的关键）否则 ~。
    """
    override = os.environ.get("EMBERPY_AGENTS_DIR")
    if override:
        p = Path(override).expanduser().resolve()
        return [p] if p.is_dir() else []
    if home_base is None:
        env_home = os.environ.get("EMBER_HOME")
        home_base = (Path(env_home).expanduser() if env_home else Path.home()).resolve()

    ws = workspace.resolve()
    project_dirs: list[Path] = [ws]
    d = ws.parent
    while d != home_base and d.parent != d and _under(d, home_base):
        project_dirs.append(d)
        d = d.parent

    probes: list[Path] = []
    for p in project_dirs:
        for sub in AGENTS_PROJECT_SUBDIRS:
            probes.append(p / sub)
    for sub in AGENTS_USER_SUBDIRS:
        probes.append(home_base / sub)
    return _existing(probes)


def load_agent_roots(roots: list[Path]) -> list[AgentDef]:
    """按根顺序扫并去重（同一真实文件只留第一个；根顺序即优先级）。"""
    defs: list[AgentDef] = []
    seen: set[str] = set()
    for root in roots:
        for adef in scan_root(root, _source_label(root)):
            if adef.path in seen:
                continue
            seen.add(adef.path)
            defs.append(adef)
    return defs


def _source_label(root: Path) -> str:
    env_home = os.environ.get("EMBER_HOME")
    home = (Path(env_home).expanduser() if env_home else Path.home()).resolve()
    root = root.resolve()
    if root == home or home in root.parents:
        return "user"
    return "project"


def build_agent_store(workspace: Path) -> AgentStore:
    """一次性助手：按默认规则找到 agent 目录并建 store。"""
    return AgentStore(resolve_agent_roots(workspace))
