"""记忆库（memdir）：文件式跨会话记忆，复刻 claude memdir 的行为设计。

目录布局：<home>/projects/<sanitized-工作区>/memory/
  - 每条记忆一个 .md（frontmatter: name / description / type），正文为记忆内容；
  - MEMORY.md 是纯索引（无 frontmatter），每行一条 ``- [Title](name.md) — 一句提示``，
    上限 200 行 / 25KB，超限截断并附 WARNING；
  - type 闭集：user | feedback | project | reference。

谁在调用：模型通过 memory_save / memory_read / memory_list 工具读写（本模块只做文件
模型，不含任何工具/循环逻辑）。写入一律原子（先写临时文件再 rename），索引每次保存后
由 store 重建 —— 名字重复不会产生重复行，改名/删除天然同步。

设计要点（对齐 claude 行为、代码自写）：
- 目录懒建：list/read 在目录不存在时返回空，绝不 mkdir；只有第一次 save 才建目录。
- 索引两态：空索引时提示"还没有记忆"，非空则截断后整段进上下文。
- 目录必须存在且可写由 save 保证；路径一律 resolve，杜绝穿越。
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..errors import FileToolError

ENTRYPOINT_NAME = "MEMORY.md"
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_000
MEMORY_TYPES = ("user", "feedback", "project", "reference")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")
# 相关召回参数：索引条目多于这个数才按 task 挑"最相关全文"；太多条时把全部
# 记忆都塞进上下文会稀释注意力，只挑 top 若干读全文。默认 0 task / 少条目回退
# 纯索引（既有行为）。
REL_TASK_THRESHOLD = 60
REL_MAX_READS = 3
REL_READ_CHAR_LIMIT = 2_000
_REL_HEADING = "## 与本任务最相关的记忆"
_CJK_CHAR_RE = re.compile(r"[一-鿿]")


def _sanitize(path: Path) -> str:
    """把绝对工作区路径折成文件系统安全的目录名（过长则截断 + 取路径哈希尾部）。

    对齐 claude 的 ``projects/<sanitized-cwd>/memory`` 布局：不同项目记忆互不干扰，
    同一项目（含换机器）路径稳定。
    """
    slug = _SANITIZE_RE.sub("-", str(path.resolve())).strip("-._")
    if len(slug) <= 80:
        return slug
    digest = __import__("hashlib").sha1(str(path.resolve()).encode("utf-8")).hexdigest()
    return f"{slug[:40]}-{digest[:10]}"


def resolve_memory_dir(workspace: Path) -> Path:
    """记忆根目录：EMBER_HOME/projects/<sanitize-cwd>/memory；无 EMBER_HOME 退 ~/.ember。

    EMBERPY_MEMORY_DIR 环境变量可整体覆盖（测试/嵌入用）。与
    ``session.resolve_sessions_dir`` 共用同一 home 语义。
    """
    override = os.environ.get("EMBERPY_MEMORY_DIR")
    if override:
        return Path(override).expanduser().resolve()
    base = os.environ.get("EMBER_HOME")
    home = Path(base).expanduser() if base else Path.home() / ".ember"
    return (home / "projects" / _sanitize(workspace) / "memory").resolve()


@dataclass(frozen=True)
class MemoryMeta:
    """一条记忆的清单项（list() 返回，供 manifest / prompt 用）。"""

    name: str
    description: str
    type_: Optional[str]
    path: Path
    mtime_ms: float


@dataclass(frozen=True)
class SaveResult:
    path: str
    created: bool  # True=新建，False=覆盖旧条目


# ---------------------------------------------------------------------------
# frontmatter 读写（本 store 自己写的文件，严格往返即可）
# ---------------------------------------------------------------------------


def _serialize(name: str, description: str, type_: str, content: str) -> str:
    """组装一条记忆文件文本。description 压成单行（索引只读它当 hook）。"""
    desc = " ".join((description or "").split())
    body = content.strip()
    parts = ["---", f"name: {name}", f"description: {desc}", f"type: {type_}", "---"]
    if body:
        parts.append("")
        parts.append(body)
    return "\n".join(parts) + "\n"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解出 frontmatter 字典 + 正文。容忍手改/非严格格式：缺字段补空串。"""
    meta: dict[str, str] = {"name": "", "description": "", "type": ""}
    if not text.startswith("---"):
        return meta, text.strip()
    lines = text.split("\n", 6)
    # lines[0]=---；frontmatter 结束行可能是第二段 "---"，否则视为无正文
    if len(lines) < 3:
        return meta, ""
    if not lines[1].startswith("---"):
        rest = text.split("---", 2)
        head = rest[1] if len(rest) > 1 else ""
        body = rest[2].strip() if len(rest) > 2 else ""
        for line in head.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip().lower()] = value.strip()
        return meta, body
    head = lines[1]
    body = "\n".join(lines[2:]).strip() if len(lines) > 2 else ""
    for line in head.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip()
    return meta, body


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @property
    def index_path(self) -> Path:
        return self.root / ENTRYPOINT_NAME

    # -- 读 ----------------------------------------------------------------
    def list(self) -> list[MemoryMeta]:
        """列目录里 .md 的清单（最新在前，cap 200）。目录不存在返回空。"""
        if not self.root.is_dir():
            return []
        metas: list[MemoryMeta] = []
        for path in sorted(self.root.glob("*.md")):
            if path.name == ENTRYPOINT_NAME:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta, _body = _parse_frontmatter(text)
            metas.append(
                MemoryMeta(
                    name=meta.get("name") or path.stem,
                    description=meta.get("description") or "",
                    type_=(meta.get("type") or None) if meta.get("type") in MEMORY_TYPES else None,
                    path=path,
                    mtime_ms=path.stat().st_mtime * 1000.0,
                )
            )
        metas.sort(key=lambda m: m.mtime_ms, reverse=True)
        return metas[:200]

    def read(self, name: str) -> str:
        """读一条记忆全文（frontmatter + 正文）。名字不存在抛 FileToolError。"""
        safe = self._validate_name(name)
        path = self.root / f"{safe}.md"
        if not path.is_file():
            raise FileToolError(f"没有这条记忆：{safe}。可用 memory_list 查看全部。")
        return path.read_text(encoding="utf-8", errors="replace")

    # -- 写 ----------------------------------------------------------------
    def save(self, name: str, description: str, type_: str, content: str) -> SaveResult:
        """保存一条记忆（新建/覆盖）。自动两步：写文件 + 重建 MEMORY.md 索引。"""
        safe = self._validate_name(name)
        if type_ not in MEMORY_TYPES:
            raise FileToolError(f"type 必须是 {', '.join(MEMORY_TYPES)} 之一，收到：{type_!r}")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{safe}.md"
        created = not path.exists()
        _atomic_write(path, _serialize(safe, description, type_, content))
        # 索引里新条目加在最后即可；重建比逐行 append 更不容易留重复/错位
        _atomic_write(self.index_path, self._build_index_text(self.list()))
        return SaveResult(path=str(path), created=created)

    def index_text(self) -> str:
        """MEMORY.md 当前文本（供注入上下文），超限截断并附 WARNING。"""
        raw = self._read_raw_index()
        if not raw.strip():
            return ""
        lines = raw.strip().split("\n")
        truncated = len(lines) > MAX_INDEX_LINES or len(raw.strip()) > MAX_INDEX_BYTES
        if len(lines) > MAX_INDEX_LINES:
            lines = lines[:MAX_INDEX_LINES]
        text = "\n".join(lines)
        if len(text) > MAX_INDEX_BYTES:
            cut = text.rfind("\n", 0, MAX_INDEX_BYTES)
            text = text[: cut if cut > 0 else MAX_INDEX_BYTES]
            truncated = True
        if truncated:
            text += (
                f"\n\n> WARNING: {ENTRYPOINT_NAME} 超限，只加载了部分。索引每条保持单行 ~150 字符，"
                "详情移进各主题文件。"
            )
        return text

    def _build_index_text(self, metas: list[MemoryMeta]) -> str:
        if not metas:
            return ""
        lines = []
        for m in metas:
            hook = " ".join(m.description.split())
            if len(hook) > 150:
                hook = hook[:147] + "..."
            title = m.name if m.name else m.path.stem
            lines.append(f"- [{title}]({m.path.name}) — {hook}" if hook else f"- [{title}]({m.path.name})")
        return "\n".join(lines) + "\n"

    def _read_raw_index(self) -> str:
        try:
            return self.index_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    # -- 校验 --------------------------------------------------------------
    def _validate_name(self, name: str) -> str:
        """名字即文件名主干：只收安全字符，防目录穿越 / 覆盖 MEMORY.md。"""
        if not isinstance(name, str) or not name.strip():
            raise FileToolError("记忆名字不能为空。")
        safe = name.strip()
        if not _NAME_RE.match(safe) or safe == "MEMORY" or safe in (".", ".."):
            raise FileToolError(
                "记忆名字只能由字母/数字/._-组成且不能以 . 开头（如 user_preferences），"
                f"收到：{name!r}"
            )
        return safe


def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件再 rename，避免半截文件被读到（崩溃/并发安全）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# prompt（注入 system prompt 的记忆区块，对齐 claude buildMemoryPrompt 职责）
# ---------------------------------------------------------------------------


def _rel_features(text: str) -> set[str]:
    """把文本折成确定性匹配特征集：ASCII 小写词 + 中文连续串的双字组。

    中文没有空格分词：整句按非字母字符切会粘成一整坨，与任务里的中文串对不上。
    中文段取相邻两字（bigram），能稳定命中重叠片段（任务「修复登录」、描述带
    「登录页」时共享 登录 一个 bigram）。ASCII 的 name/description 用单词匹配足够。
    """
    out: set[str] = set()
    s = text.lower()
    out.update(re.findall(r"[a-z0-9]+", s))
    for seg in re.findall(r"[一-鿿]+", text):  # 中文原样无大小写，直接找连续段
        if len(seg) >= 2:
            for i in range(len(seg) - 1):
                out.add(seg[i : i + 2])
        else:
            out.add(seg)
    return out


def _clip_rel(body: str) -> str:
    body = body.strip()
    if len(body) <= REL_READ_CHAR_LIMIT:
        return body
    return body[:REL_READ_CHAR_LIMIT] + "\n…(该条记忆过长，已截断)"


def _relevant_reads(store: MemoryStore, task: str) -> list[tuple[str, str, str]]:
    """按 task 与各记忆 name+description 的字符重叠打分，回 top ≤3 条全文。

    前置条件已在调用方把关：task 非空、索引条目 > 阈值。任何一条都打不上分时
    返回空（宁可不注入，也不把无关记忆全文塞进上下文）。排序确定性：得分降序、
    旧到新稳定（list 本身按 mtime 新在前，再以名字兜底）。
    """
    task = (task or "").strip()
    task_feats = _rel_features(task)
    if not task_feats:
        return []
    scored: list[tuple[int, float, str, MemoryMeta]] = []
    for meta in store.list():
        mem_feats = _rel_features(f"{meta.name} {meta.description}")
        score = len(task_feats & mem_feats)
        if score:
            scored.append((-score, -meta.mtime_ms, meta.name, meta))
    scored.sort()
    reads: list[tuple[str, str, str]] = []
    for _neg, _mtime, _name, meta in scored[:REL_MAX_READS]:
        try:
            body = _clip_rel(store.read(meta.name))
        except (FileToolError, OSError):
            continue
        reads.append((meta.name, meta.description, body))
    return reads


def build_memory_prompt(store: MemoryStore, task: str = "") -> str:
    """给 Agent 的记忆行为指南 + 当前索引（两级：索引常驻 + 相关记忆全文）。

    task 非空且索引条目超过阈值时，额外按相关度挑 top ≤3 条记忆全文附在尾部
    （见 _relevant_reads）；否则行为与之前完全一致，既有调用/测试不受影响。
    目录不需要此刻存在（由 save 建）。
    """
    index = store.index_text()
    lines = [
        "# 跨会话记忆（memory）",
        "",
        f"你有一个持久、基于文件的记忆系统，目录：`{store.root}`。"
        "它能跨会话生效——之前会话存下的记忆会在新会话自动出现在本区块，供你调用。",
        "",
        "## 何时保存",
        "用户明确说「记住/以后都这样」时立即保存；或你了解到**跨会话才有用**的偏好、纠正、"
        "项目上下文、外部资源位置时主动保存。判断是否值得存：这条对**以后别的会话**有用吗？"
        "只用得到当前这轮的不要存（该用 plan / task 或只放在对话里）。",
        "",
        "## 保存方式",
        "用 memory_save 工具：给一个语义化名字（user_preferences、feedback_terse）、一行描述、"
        "类型与正文。memory_save 会自动写文件并把一行指针加进 MEMORY.md（MEMORY.md 是纯索引，"
        "不是记忆本体，永远不要手动往里面写正文）。更新比新建好：内容相近先 memory_read 旧条目"
        "再覆盖，别写重复条目；记错/过时的记忆要覆盖掉。",
        "",
        "## 四种类型",
        "- user：用户的身份/角色/技能/沟通偏好（这条最重要，帮助按他的水平与偏好协作）",
        "- feedback：用户纠正过你「别这么做」或确认过「这样对」的做事方式（存原因 why）",
        "- project：项目里正在推进的工作/目标/约束/背景（用户说的、代码和 git 推不出来的）",
        "- reference：外部系统的指针（bug 在哪个 Linear 项目、监控面板地址…）",
        "",
        "## 不要存什么",
        "- 代码结构/架构/约定/文件路径——读一遍当前项目就能得到；",
        "- git 历史、改了什么——git log 更权威；",
        "- 修 bug 的解法——修完就在代码里；",
        "- CLAUDE.md / README 已写的；临时任务细节、当前对话进度。",
        "",
        "## 何时读",
        "接到任务先扫一眼上面 MEMORY.md 索引；命中相关描述就 memory_read 读全文再采用。"
        "**记忆可能已过时**：用它前对照当前项目现状核实，不确定就向用户确认，别盲信。",
        "",
        "## MEMORY.md",
        "",
    ]
    if index:
        lines.append(index)
    else:
        lines.append("（还没有任何记忆。等你在任务中学到值得跨会话保留的信息，用 memory_save 存下来，它会出现在这里。）")
    text = "\n".join(lines)

    task = (task or "").strip()
    if task and len(store.list()) > REL_TASK_THRESHOLD:
        reads = _relevant_reads(store, task)
        if reads:
            count = len(store.list())
            parts = [
                "",
                _REL_HEADING,
                f"索引条目较多（{count} 条），下面按相关度挑了 top {len(reads)} 条记忆的全文，"
                "直接对照采用。相关度按「当前任务 × name/description 字符重叠」粗估，仅供参考。",
            ]
            for name, desc, body in reads:
                parts.append("")
                parts.append(f"### {name}")
                if desc:
                    parts.append(desc)
                parts.append("")
                parts.append(body)
            text += "\n" + "\n".join(parts)
    return text
