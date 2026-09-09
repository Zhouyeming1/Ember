"""技能发现：扫目录、找 SKILL.md / 单文件命令 md、按真实路径去重。

复刻 claude skills 发现规则的子集（对齐 ``.agents/skills`` ``.claude/skills``
两种根、目录式 ``<name>/SKILL.md`` 为主、兼容单 ``<name>.md``）：
- 目录式技能：目录下任意深度的 ``SKILL.md``，名字 = 相对根的目录路径用 ``:``
  拼（``a/b/SKILL.md`` -> ``a:b``）；该目录里的其它 .md 不算技能本体（正文里的
  相对链接由模型按 base_dir 解析）。
- 单文件技能：根目录下直接放的 ``<name>.md``（legacy /commands 兼容），
  名字 = 文件名去 .md。
- 一个真实文件（realpath 解析符号链接）只加载一次，多根重复按根顺序第一个生效。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .frontmatter import fm_bool, fm_first_line, fm_str, fm_str_list, parse_frontmatter
from .models import Skill

# 项目级根：workspace（及其下层 home 之内的祖先）下这些子目录会被当作技能根。
# user 级根由 resolve_skill_roots 在 home 下追加（.ember/skills/.agents/skills/.claude/skills）。
PROJECT_SKILL_SUBDIRS = (".agents/skills", ".claude/skills")


def _read_utf8(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _skill_from_file(path: Path, name: str, source: str) -> Optional[Skill]:
    text = _read_utf8(path)
    if text is None or not text.strip():
        return None
    data, body = parse_frontmatter(text)
    if not body.strip():
        return None
    description = fm_str(data, "description") or fm_first_line(body, name)
    return Skill(
        name=name,
        description=description,
        body=body,
        path=path.resolve(),
        base_dir=path.parent.resolve(),
        source=source,
        when_to_use=fm_str(data, "when_to_use"),
        allowed_tools=fm_str_list(data, "allowed-tools"),
        argument_names=fm_str_list(data, "arguments"),
        argument_hint=fm_str(data, "argument-hint"),
        version=fm_str(data, "version"),
        user_invocable=fm_bool(data, "user-invocable", default=True),
        disable_model_invocation=fm_bool(data, "disable-model-invocation"),
        # context: fork -> 隔离执行技能（对齐 claude frontmatter 的 executionContext）
        fork=fm_str(data, "context").lower() == "fork",
        paths=fm_str_list(data, "paths"),
    )


def scan_root(root: Path, source: str) -> list[Skill]:
    """扫一个技能根目录，返回其下全部技能（目录式 + 根级单文件）。"""
    root = root.resolve()
    if not root.is_dir():
        return []
    found: list[Skill] = []

    # 目录式：任意深度 SKILL.md，名字 = 相对根的目录路径用 : 拼
    for dirpath, _dirnames, filenames in os.walk(root):
        if "SKILL.md" in filenames:
            rel = Path(dirpath).relative_to(root)
            if not rel.parts:
                continue  # 根目录自身不放 SKILL.md（没有名字）
            name = ":".join(rel.parts)
            skill = _skill_from_file(Path(dirpath) / "SKILL.md", name, source)
            if skill is not None:
                found.append(skill)

    # 单文件：只认根目录顶层 *.md（不递归，避免把技能目录里附带的 md 当命令）
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        entries = []
    for child in entries:
        if not child.is_file():
            continue
        if child.name == "SKILL.md" or not child.name.endswith(".md"):
            continue
        skill = _skill_from_file(child, child.name[: -len(".md")], source)
        if skill is not None:
            found.append(skill)
    return found


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


def resolve_skill_roots(workspace: Path, home_base: Optional[Path] = None) -> list[Path]:
    """给出要扫描的技能根目录（已存在、去重、项目>user 优先级）。

    - ``EMBERPY_SKILLS_DIR`` 整体覆盖（测试/嵌入隔离用）；
    - 否则 project 根 = workspace 以及（home 之内的）每个祖先目录下的
      PROJECT_SKILL_SUBDIRS，就近优先；user 根 = home_base 下
      .ember/skills、.agents/skills、.claude/skills（最后扫，同文件让位给项目级）；
    - home_base 缺省 = ``EMBER_HOME``（设了就当作 home，测试隔离的关键）否则 ``~``。
    """
    override = os.environ.get("EMBERPY_SKILLS_DIR")
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
    # workspace 在 home 之外时上面的 while 不跑（不扫 home 之外的祖先），只留 workspace

    probes: list[Path] = []
    for p in project_dirs:
        for sub in PROJECT_SKILL_SUBDIRS:
            probes.append(p / sub)
    for sub in (".ember/skills", ".agents/skills", ".claude/skills"):
        probes.append(home_base / sub)
    return _existing(probes)


def load_skill_roots(roots: list[Path]) -> list[Skill]:
    """按根顺序扫并去重（同一真实文件只留第一个）。

    去重按 Skill.path（已 resolve），因此符号链接/重复父目录只加载一次；
    根顺序就是优先级（项目级在 user 前，靠前优先）。"""
    skills: list[Skill] = []
    seen: set[str] = set()
    for root in roots:
        for skill in scan_root(root, _source_label(root)):
            if skill.path in seen:
                continue
            seen.add(skill.path)
            skills.append(skill)
    return skills


def _source_label(root: Path) -> str:
    """尽量贴近语义的标签：home 目录（或 EMBER_HOME）下的算 user，否则 project。"""
    env_home = os.environ.get("EMBER_HOME")
    home = (Path(env_home).expanduser() if env_home else Path.home()).resolve()
    root = root.resolve()
    if root == home or home in root.parents:
        return "user"
    return "project"


def build_skill_store(workspace: Path) -> "SkillStore":
    """一次性助手：按默认规则找到技能目录并建 store（import 放函数内避免循环依赖）。"""
    from .store import SkillStore

    return SkillStore(resolve_skill_roots(workspace))
