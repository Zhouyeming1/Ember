"""SkillStore：技能注册表 + 触发时把技能正文渲染成要注入的文本。

渲染对齐 claude ``getPromptForCommand`` 的要点：
- 文件型技能正文前加一行 ``Base directory for this skill: <目录>``（模型据此
  解析技能里相对路径的引用，例如 ``[tokens.md](tokens.md)``）；
- 参数插值：``$ARGUMENTS`` / ``${ARGUMENTS}`` -> 整段参数；声明了
  ``arguments:`` 名字列表时，``$名字`` -> 按空格切分后的第 N 个词；
  ``${CLAUDE_SKILL_DIR}`` / ``$CLAUDE_SKILL_DIR`` -> 技能目录（正斜杠）。
- 不做 inline shell（``!``…````` 那种）执行——本引擎不信任技能正文里暗藏的
  命令，需要跑命令时由模型走 shell 工具（含权限门），比 prompt 阶段静默执行安全。

命令面输出（commands()）对齐桌面端 ``get_commands`` -> parseSkillCommands 契约：
``name: "skill:<name>"``、``source: "skill"``、``sourceInfo: {path, baseDir}``。
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Iterable, Optional

from .loader import build_skill_store, load_skill_roots, resolve_skill_roots
from .models import Skill

_ARG_BLOCK = re.compile(r"\$\{ARGUMENTS\}")
_ARG_PLAIN = re.compile(r"\$ARGUMENTS\b")
_DIR_BLOCK = re.compile(r"\$\{CLAUDE_SKILL_DIR\}")
_DIR_PLAIN = re.compile(r"\$CLAUDE_SKILL_DIR\b")
_LISTING_LINE_MAX = 240
_LISTING_MAX_TOTAL = 6000


def _path_matches(pattern: str, target: Path, workspace: Optional[Path]) -> bool:
    """一条 paths 规则是否命中触碰文件（gitignore 式启发，够用即可）。

    说明：fnmatch 的 ``*``/``**`` 都能跨 ``/`` 匹配，因此整串匹配天然覆盖子树
    （``src/**`` 命中 ``src/a/b.py``）。按三条规则归一：
    - ``**/`` 前缀 = 任意深度（``**/*.py`` 命中树下任意 .py；也允许零层目录）；
    - 规则含 ``/`` 时按"工作区相对 + 绝对"两串 fnmatch；不含 ``/`` 时只比文件名
      （``test_*.py`` 命中任意目录下的同名文件）；
    - ``~``/``$HOME`` 前缀当绝对主目录规则。
    """
    pat = pattern.strip()
    if not pat:
        return False
    # frontmatter 列表项可能带成对引号（frontmatter 解析对列表项不剥引号），剥掉
    if len(pat) >= 2 and pat[0] == pat[-1] and pat[0] in ("'", '"'):
        pat = pat[1:-1]
    pat = pat.replace("\\", "/").rstrip("/")
    if not pat:
        return False
    if pat.startswith("~/") or pat == "~":
        pat = str(Path.home()) + "/" + (pat[2:].lstrip("/"))
    posix = str(target.resolve()).replace("\\", "/")
    candidates = {posix}
    if workspace is not None:
        try:
            rel = target.resolve().relative_to(workspace.resolve()).as_posix()
            candidates.add(rel)
        except ValueError:
            pass  # 工作区外文件：只按绝对路径/文件名匹配
    if "/" not in pat:
        # 无目录约束 -> 文件名级匹配
        return fnmatch.fnmatch(posix.rsplit("/", 1)[-1], pat)
    for cand in candidates:
        if fnmatch.fnmatch(cand, pat):
            return True
        # **/ 允许零层目录：去前缀后再比一次
        if pat.startswith("**/") and fnmatch.fnmatch(cand, pat[3:]):
            return True
    return False


class SkillStore:
    def __init__(self, roots: list[Path], workspace: Optional[Path] = None) -> None:
        self.roots = list(roots)
        self.workspace = workspace.resolve() if workspace is not None else None
        self._skills = load_skill_roots(self.roots)
        self._activated: set[str] = set()
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        """按技能名建索引；同名多文件按根优先级第一个生效。"""
        self._by_name: dict[str, Skill] = {}
        for skill in self._skills:
            if skill.name not in self._by_name:
                self._by_name[skill.name] = skill
        # refresh 后磁盘可能已删/改成普通技能：清理失效的激活名
        self._activated = {
            n for n in self._activated if n in self._by_name and self._by_name[n].paths
        }

    def refresh(self) -> None:
        """重扫技能根，让磁盘上新增/删除的技能对同一 store 实例即时可见。

        动态发现：Agent.run 每轮开头调一次（同一会话续跑之间新写的 SKILL.md
        能被 Skill 工具感知）。原地更新自身状态，引用本 store 的工具/命令面对象
        不必重建——Skill 工具 fn 每次调用都经 store.get，看到的就是最新集合。
        """
        self._skills = load_skill_roots(self.roots)
        self._rebuild_index()

    @classmethod
    def from_workspace(cls, workspace: Path) -> "SkillStore":
        return cls(resolve_skill_roots(workspace), workspace=workspace)

    # -- 读 ----------------------------------------------------------------
    def all(self) -> list[Skill]:
        return list(self._skills)

    def available(self) -> list[Skill]:
        """模型/命令面可见的技能：无条件技能（无 paths）+ 已激活的条件技能。

        paths 条件技能在命中触碰路径前不进可用清单（R-F：防技能库变大后向模型
        泄出一堆不相关的工作流）。激活状态跨 refresh 保留（磁盘删了才清）。
        """
        return [s for s in self._skills if not s.paths or s.name in self._activated]

    def conditional(self) -> list[Skill]:
        """所有带 paths 的条件技能（含未激活的，供调试/测试断言）。"""
        return [s for s in self._skills if s.paths]

    def is_active(self, name: str) -> bool:
        skill = self._by_name.get(name)
        if skill is None or not skill.paths:
            return False
        return name in self._activated

    def activate_for_paths(self, touched: Iterable[Path]) -> None:
        """触碰一组文件路径后，把 paths 命中的条件技能激活（幂等）。

        由 fs 的 read_file/write_file/file_edit 成功回调调用；被激活技能随即进入
        下一轮 schema 的 Skill 清单（agent.run 每步重算 schemas -> 热更生效）。
        """
        touched = list(touched)
        if not touched:
            return
        for skill in self._skills:
            if not skill.paths or skill.name in self._activated:
                continue
            if any(_path_matches(pat, Path(t), self.workspace) for pat in skill.paths for t in touched):
                self._activated.add(skill.name)

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def get(self, name: str) -> Skill | None:
        if not name:
            return None
        exact = self._by_name.get(name)
        if exact is not None:
            return exact
        # 容错：只给尾段（不带命名空间）且唯一时也认，如 "review:lint" 输 "lint"
        matches = [s for s in self._skills if s.name.rsplit(":", 1)[-1] == name]
        uniq = {s.name: s for s in matches}
        if len(uniq) == 1:
            return next(iter(uniq.values()))
        return None

    def user_invocable(self) -> list[Skill]:
        """出现在 /skill: 命令面/UI 列表的技能（user-invocable 且 available）。"""
        return [s for s in self.available() if s.user_invocable]

    # -- 渲染 --------------------------------------------------------------
    def render(self, skill: Skill, args: str = "") -> str:
        text = skill.body
        if skill.base_dir is not None:
            base = Path(skill.base_dir).resolve().as_posix()
            header = f"Base directory for this skill: {base}\n\n"
        else:
            header = ""
        args = args or ""
        if skill.argument_names:
            tokens = args.split() if args.strip() else []
            for idx, arg_name in enumerate(skill.argument_names):
                value = tokens[idx] if idx < len(tokens) else ""
                text = re.sub(rf"\${{{arg_name}}}", value, text)
                text = re.sub(rf"\${arg_name}\b", value, text)
        text = _ARG_BLOCK.sub(args, text)
        text = _ARG_PLAIN.sub(args, text)
        if skill.base_dir is not None:
            skill_dir = Path(skill.base_dir).resolve().as_posix()
            text = _DIR_BLOCK.sub(skill_dir, text)
            text = _DIR_PLAIN.sub(skill_dir, text)
        return header + text

    # -- 面向模型/命令面 ----------------------------------------------------
    def listing(self) -> str:
        """给 Skill 工具的 description 用：一行一条 ``- name — desc``（限长限总量）。"""
        lines: list[str] = []
        used = 0
        for skill in self.available():
            desc = " ".join(skill.description.split())
            if len(desc) > 200:
                desc = desc[:197] + "..."
            line = f"- {skill.name}: {desc}"
            if skill.when_to_use:
                room = _LISTING_LINE_MAX - len(line)
                if room > 20:
                    line += f" — {skill.when_to_use[:room - 2]}"
            if len(line) > _LISTING_LINE_MAX:
                line = line[:_LISTING_LINE_MAX - 3] + "..."
            used += len(line) + 1
            if used > _LISTING_MAX_TOTAL:
                lines.append("- ...(技能太多，其余略)")
                break
            lines.append(line)
        return "\n".join(lines) if lines else "（没有可用技能）"

    def commands(self) -> list[dict[str, object]]:
        """get_commands 返回体：仅 user-invocable，名字带 skill: 前缀。"""
        out: list[dict[str, object]] = []
        for skill in self.user_invocable():
            out.append(
                {
                    "name": f"skill:{skill.name}",
                    "description": skill.description,
                    "source": "skill",
                    "sourceInfo": {
                        "path": str(skill.path),
                        "baseDir": str(skill.base_dir) if skill.base_dir else None,
                    },
                }
            )
        return out
