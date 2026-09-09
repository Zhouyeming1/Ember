"""文件系统工具：列目录 / 读文件 / 通配列文件 / 文本搜索 / 写文件 / 精确编辑。

- 所有路径都先 resolve 到工作区内，杜绝目录穿越与符号链接逃逸
- 权限检查在工具内部执行（gate.authorize_read / authorize_write）
- 写文件统一走 PatchStore，天然留下可 undo 的 checkpoint
- 返回文本刻意保持精简，避免把大文件内容原样塞回上下文烧 token
- 严格写前必读（对齐 claude FileEdit/Write）：已存在的文件，write_file/file_edit
  前必须先 read_file 过（登记在 env.read_state），且文件未被外部改动（mtime/size
  与读过时一致），否则拒绝——防止基于陈旧内容覆盖用户的文件。新建文件不受限。
  写成功后也会登记（自己刚建/刚写的文件再编辑不必重复读）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..errors import FileToolError, NotTextFileError
from ..permission import PermissionGate, resolve_within
from .registry import Tool, ToolCategory, ToolEnv

MAX_READ_BYTES = 200_000        # 单次读文件上限（非分页），防止灌爆上下文
MAX_PAGED_LINES = 500           # offset/limit 分页读取时，缺省 limit
MAX_GREP_HITS = 80              # 搜索命中最多条（content 模式）
MAX_GLOB_HITS = 200             # glob 最多返回文件数
SCAN_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", "target", ".pytest_cache",
    ".ember",  # 引擎项目级数据目录（shell 落盘等），不参与模型文件浏览
}


def _err(exc: Exception) -> str:
    """把工具错误转成给模型的文本（不抛异常中断循环）。"""
    return f"错误：{exc}"


def _render_ipynb(text: str) -> str:
    """把 .ipynb 的 JSON 折成 markdown 化视图（cell 源码 + 文本输出）。

    对齐 claude FileRead 对 notebook 的"分格阅读"：模型直接读原始 JSON 几乎无法
    使用，渲染成按 cell 排列的文本视图才有意义。解析失败/结构不符返回空串，
    调用方落回原文本（当作普通 UTF-8 文件读）。
    """
    try:
        nb = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        return ""
    out: list[str] = []
    for i, cell in enumerate(nb["cells"], 1):
        if not isinstance(cell, dict):
            continue
        kind = cell.get("cell_type", "code")
        src = cell.get("source")
        if isinstance(src, list):
            src = "".join(str(s) for s in src)
        else:
            src = str(src or "")
        out.append(f"### Cell {i}（{kind}）")
        if src.strip():
            body = src.rstrip("\n")
            out.append(f"```\n{body}\n```" if kind != "markdown" else body)
        for output in cell.get("outputs") or []:
            if not isinstance(output, dict):
                continue
            otype = output.get("output_type")
            if otype == "stream":
                stream = output.get("text") or []
                if isinstance(stream, list):
                    stream = "".join(str(s) for s in stream)
                if str(stream).strip():
                    out.append(f"[stdout]\n{str(stream).rstrip(chr(10))}")
            elif otype in ("execute_result", "display_data"):
                data = output.get("data") or {}
                if not isinstance(data, dict):
                    continue
                for mime, content in data.items():
                    if isinstance(content, list):
                        content = "".join(str(s) for s in content)
                    content = str(content or "")
                    if mime == "text/plain" and content.strip():
                        out.append(content.rstrip(chr(10)))
                    elif mime.startswith("image/"):
                        out.append(f"[图片输出 {mime}]")
                    elif mime != "text/plain":
                        out.append(f"[{mime} 输出]")
    return "\n\n".join(out)


def _resolve(workspace: Path, given: str) -> Path:
    try:
        return resolve_within(workspace, given)
    except ValueError as exc:
        raise FileToolError(str(exc)) from exc


def _lexical(workspace: Path, given: str) -> Path:
    """用户给的原词法路径：展开 ~、补成工作区下绝对路径，但**不**展开符号链接。

    gate.authorize_write 对 canonical（resolve 后）与 lexical（resolve 前）都查
    deny/敏感集合——软链把词法段藏掉（如 link -> ../.git）时，canonical 看不到
    但 lexical 看得到，双查兜住（对齐 claude"词法原样 + realpath 终值"都判）。
    """
    candidate = Path(given).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    return candidate


def _capped(text: str, limit: int = 60_000) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit // 2):]
    return f"{head}\n……[中间省略 {len(text) - limit} 字符]……\n{tail}"


def _file_state(target: Path) -> tuple[int, int]:
    """文件指纹：(st_mtime_ns, st_size)，写前必读比较用。"""
    st = target.stat()
    return (st.st_mtime_ns, st.st_size)


def _record_read(env: ToolEnv, target: Path) -> None:
    """登记"已读过该文件当前版本"（供写前护栏 / 重复读去重）。"""
    try:
        env.read_state[str(target)] = _file_state(target)
    except OSError:
        pass


def _maybe_activate_skill(env: ToolEnv, target: Path) -> None:
    """文件触碰成功后回调 SkillStore.activate_for_paths（R-F 条件技能激活）。

    命中 paths 的技能随即进入模型可见清单（agent.run 每步重算 schemas）。只做
    幂等标记，任何失败静默（技能激活是锦上添花，不能拖垮文件操作）。
    """
    skills = getattr(env, "skills", None)
    activator = getattr(skills, "activate_for_paths", None)
    if activator is None:
        return
    try:
        activator([target])
    except Exception:
        pass


def _is_skipped_path(p: Path) -> bool:
    return any(part in SCAN_SKIP_DIRS for part in p.parts)


def _guard_write(env: ToolEnv, target: Path) -> str:
    """严格写前必读护栏。返回 None=放行；否则返回给模型的错误文本。

    规则（对齐 claude）：
    - 目标文件不存在（=新建）→ 放行，不需要读过；
    - 已存在但本会话从未 read_file 过 → 拒绝，引导先读；
    - 已存在、读过，但读后文件被外部改过（指纹变了）→ 拒绝，引导重读最新内容。
    """
    try:
        exists = target.exists()
    except OSError:
        return f"错误：无法访问文件 {target}"
    if not exists:
        return ""
    key = str(target)
    recorded = env.read_state.get(key)
    if recorded is None:
        return (
            f"错误：请先 read_file \"{target}\" 再编辑它（防止基于过期内容覆盖）。"
            "读取后如果文件没变就能正常修改。"
        )
    try:
        current = _file_state(target)
    except OSError:
        return f"错误：无法读取文件状态 {target}"
    if recorded != current:
        return (
            f"错误：文件 {target} 自上次读取后已被外部改动，请先重新 read_file "
            "查看最新内容，再决定如何修改。"
        )
    return ""


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


def build_fs_tools(env: ToolEnv) -> list[Tool]:
    workspace: Path = env.workspace
    gate: PermissionGate = env.gate

    def list_dir(path: str = ".") -> str:
        try:
            target = _resolve(workspace, path)
            gate.authorize_read(target, env.confirm)
            if not target.is_dir():
                return f"错误：不是目录：{target}"
            entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
            lines = []
            for entry in entries[:500]:
                marker = "/" if entry.is_dir() else ""
                lines.append(f"{entry.name}{marker}")
            return "\n".join(lines) if lines else "(空目录)"
        except (FileToolError, OSError) as exc:
            return _err(exc)

    def read_file(path: str, offset: int | None = None, limit: int | None = None) -> str:
        """读文件。offset/limit 给定时按"行"分页读取（可跳读大文件中部/末尾），
        每行带真实行号；不给则读全文（上限 200KB，超出部分截断）。"""
        try:
            target = _resolve(workspace, path)
            gate.authorize_read(target, env.confirm)
            if not target.is_file():
                return f"错误：文件不存在：{target}"
        except (FileToolError, OSError) as exc:
            return _err(exc)

        paged = offset is not None or limit is not None
        if paged:
            # 行分页：decode 全量后切片（超大文件这里按字节保底，>50MB 拒避免占内存）
            try:
                if target.stat().st_size > 50 * 1024 * 1024:
                    return f"错误：文件过大，请用 grep 缩小范围定位后配合 offset/limit 分页读取。"
                text = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return _err(exc)
            _record_read(env, target)
            _maybe_activate_skill(env, target)
            lines = text.splitlines()
            start = offset if isinstance(offset, int) and offset >= 0 else 0
            count = limit if isinstance(limit, int) and limit > 0 else MAX_PAGED_LINES
            segment = lines[start : start + count]
            if not segment:
                return f"(该范围无内容：文件共 {len(lines)} 行，从第 {start} 行起为空)"
            numbered = [f"{start + i + 1}: {line}" for i, line in enumerate(segment)]
            shown = f"{start + 1}–{start + len(segment)}"
            body = "\n".join(numbered)
            suffix = (
                f"\n……(共 {len(lines)} 行，已显示 {shown}；继续读用 offset={start + len(segment)})"
                if start + len(segment) < len(lines)
                else ""
            )
            return _capped(body, limit=100_000) + suffix

        try:
            raw = target.read_bytes()
        except OSError as exc:
            return _err(exc)
        if len(raw) > MAX_READ_BYTES:
            return f"错误：文件过大（{len(raw)} 字节），超过单次读取上限 {MAX_READ_BYTES}。请用 grep 定位，或加 offset/limit 分页读取。"
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"错误：不是文本文件（二进制）：{target}"

        # .ipynb 显示成按 cell 排列的 markdown 视图（原始 JSON 模型几乎无法使用）
        display = text
        if target.suffix.lower() == ".ipynb":
            view = _render_ipynb(text)
            if view:
                display = view

        # 重复读去重：同一路径、指纹未变、上次也是全量读 -> 内容没变，回 stub 省 token
        try:
            state = _file_state(target)
        except OSError:
            state = None
        key = str(target)
        last = getattr(env, "_last_full_read", None)
        if state is not None and last == (key, state):
            return f"文件 {path} 自上次读取后没有变化（内容同上一次 read_file 的结果，可对照上文）。"
        _record_read(env, target)
        _maybe_activate_skill(env, target)
        if state is not None:
            setattr(env, "_last_full_read", (key, state))
        if not display.strip():
            return "(空文件)"
        return _capped(display)

    def grep(
        pattern: str,
        path: str = ".",
        max_hits: int = MAX_GREP_HITS,
        output_mode: str = "content",
        head_limit: int | None = None,
        offset: int = 0,
    ) -> str:
        """按正则搜内容。output_mode：content=带行号命中行（默认）；files=只回命中
        文件相对路径（命中多时省 token）；count=每个文件的命中条数。head_limit 控制
        返回条数上限，offset 分页跳过头 N 条（files/count 模式下指文件行数）。"""
        try:
            target = _resolve(workspace, path)
            gate.authorize_read(target, env.confirm)
            regex = re.compile(pattern)
        except re.error as exc:
            return f"错误：正则不合法：{exc}"
        except (FileToolError, OSError) as exc:
            return _err(exc)

        files: list[Path] = [target] if target.is_file() else []
        if target.is_dir():
            for candidate in target.rglob("*"):
                if candidate.is_file() and not _is_skipped_path(candidate):
                    files.append(candidate)
                    if len(files) > 2000:  # 扫描上限，避免把整机扫一遍
                        break
        files.sort()

        limit = head_limit if isinstance(head_limit, int) and head_limit >= 0 else max_hits
        start = offset if isinstance(offset, int) and offset >= 0 else 0

        mode = (output_mode or "content").strip().lower()
        if mode not in ("content", "files", "count"):
            return f"错误：output_mode 只能是 content/files/count，收到 {output_mode!r}"

        # 先扫出 (文件相对路径, 行号, 文本) 命中；files/count 模式要完整扫才能去重/计数
        matched: list[tuple[str, int, str]] = []
        for file_path in files:
            try:
                with file_path.open("r", encoding="utf-8", errors="replace") as fh:
                    for line_no, line in enumerate(fh, 1):
                        if regex.search(line):
                            rel = str(file_path.relative_to(workspace)).replace("\\", "/")
                            matched.append((rel, line_no, line.rstrip("\n")))
                            if mode == "content" and len(matched) >= 2000:
                                break
            except OSError:
                continue
            if mode == "content" and len(matched) >= 2000:
                break

        if not matched:
            return f"未找到匹配：{pattern}"

        if mode == "content":
            total = len(matched)
            page = matched[start : start + limit]
            lines = [f"{rel}:{line_no}: {line.strip()[:160]}" for rel, line_no, line in page]
            out = "\n".join(lines)
            if total >= 2000:
                out += "\n……(命中过多，仅返回前 2000 条)"
            elif start + limit < total:
                out += f"\n……共 {total} 条，已显示 {len(lines)}（用 head_limit/offset 翻页）"
            return out

        if mode == "files":
            # 保序去重成文件清单
            seen: list[str] = []
            for rel, _ln, _tx in matched:
                if rel not in seen:
                    seen.append(rel)
            page = seen[start : start + limit]
            out = "\n".join(page)
            if not page:
                return f"未找到匹配：{pattern}"
            if start + limit < len(seen):
                out += f"\n……共 {len(seen)} 个文件命中，已显示 {len(page)}"
            return out

        # count：文件 -> 命中数（保序）
        order: list[str] = []
        counts: dict[str, int] = {}
        for rel, _ln, _tx in matched:
            if rel not in counts:
                counts[rel] = 0
                order.append(rel)
            counts[rel] += 1
        page = order[start : start + limit]
        lines = [f"{rel}: {counts[rel]}" for rel in page]
        if not lines:
            return f"未找到匹配：{pattern}"
        out = "\n".join(lines)
        if start + limit < len(order):
            out += f"\n……共 {len(order)} 个文件命中"
        return out

    def glob(pattern: str, path: str = ".") -> str:
        """按通配符列文件（pathlib glob，** 递归）。结果 mtime 新的在前，返回相对路径。"""
        if not pattern or not str(pattern).strip():
            return "错误：pattern 不能为空（如 *.py、**/*.ts、src/**/*.test.ts）。"
        try:
            target = _resolve(workspace, path)
            gate.authorize_read(target, env.confirm)
            base = target if target.is_dir() else target.parent
        except (FileToolError, OSError) as exc:
            return _err(exc)

        pat = str(pattern).strip().replace("\\", "/")
        try:
            candidates = list(base.glob(pat))
        except (NotImplementedError, OSError) as exc:
            return f"错误：glob 模式不合法：{exc}"

        matches = [
            p for p in candidates
            if p.is_file() and not _is_skipped_path(p)
        ]
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        matches = matches[:MAX_GLOB_HITS]

        def _display(p: Path) -> str:
            try:
                rel = p.relative_to(workspace)
                return str(rel).replace("\\", "/")
            except ValueError:
                return str(p)

        if not matches:
            return f"没有匹配 {pattern!r} 的文件（在 {path or '.'} 下）"
        return "\n".join(_display(p) for p in matches)

    def write_file(path: str, content: str) -> str:
        try:
            target = _resolve(workspace, path)
            gate.authorize_write(target, env.confirm, lexical=_lexical(workspace, path))
        except (FileToolError, OSError) as exc:
            return _err(exc)
        blocked = _guard_write(env, target)
        if blocked:
            return blocked
        try:
            patch = env.patches.apply_write(target, content)
            _record_read(env, target)
            _maybe_activate_skill(env, target)
            return f"已写入 {patch.summary}（{len(content)} 字符，seq={patch.seq}）"
        except (FileToolError, NotTextFileError, OSError) as exc:
            return _err(exc)

    def file_edit(
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        """在已有文件上做精确文本替换（一处），记录 checkpoint 可 /undo。"""
        try:
            target = _resolve(workspace, path)
            gate.authorize_write(target, env.confirm, lexical=_lexical(workspace, path))
        except (FileToolError, OSError) as exc:
            return _err(exc)
        blocked = _guard_write(env, target)
        if blocked:
            return blocked
        try:
            patch = env.patches.apply_edit(
                target,
                old_string,
                new_string,
                replace_all=replace_all,
            )
            _record_read(env, target)
            _maybe_activate_skill(env, target)
            return f"已修改 {patch.summary}（seq={patch.seq}）"
        except (FileToolError, NotTextFileError, OSError) as exc:
            return _err(exc)

    return [
        Tool(
            name="list_dir",
            description="列出目录内容（工作区内）。返回文件名，目录带 / 后缀。",
            parameters={
                "properties": {
                    "path": {"type": "string", "description": "相对或绝对路径，默认当前目录", "default": "."},
                },
            },
            category=ToolCategory.READ,
            fn=list_dir,
        ),
        Tool(
            name="read_file",
            description=(
                "读取文本文件内容（工作区内）。默认读全文（上限 200KB，超出截断头尾）。"
                "给 offset/limit 可改为按行分页读取并带行号——适合跳读大文件的中部/末尾，"
                "或先看 grep 定位到的行附近。注意：编辑一个已存在的文件前必须先 read_file 过它。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要读取的文件路径"},
                    "offset": {
                        "type": "integer",
                        "description": "跳过前 N 行再开始（从 0 计），与 limit 配合分页",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回多少行（默认 500），每行带真实行号前缀",
                    },
                },
                "required": ["path"],
            },
            category=ToolCategory.READ,
            fn=read_file,
        ),
        Tool(
            name="glob",
            description=(
                "用通配符列出文件名（工作区内，默认从当前目录起）。"
                "支持 ** 递归，如：**/*.py、src/**/*.test.ts、*.json。"
                "按修改时间新的在前，最多 200 条，返回相对路径。"
                "用于\"找出所有符合条件的文件\"，比逐个 list_dir 高效。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 通配模式（** 表示任意多层目录）"},
                    "path": {"type": "string", "description": "起始目录（相对或绝对），默认 .", "default": "."},
                },
                "required": ["pattern"],
            },
            category=ToolCategory.READ,
            fn=glob,
        ),
        Tool(
            name="grep",
            description=(
                "在工作区里用正则搜索文件内容。output_mode=content 返回 文件:行号: 文本"
                "（默认）；命中文件很多时用 files 只要文件清单、用 count 看每个文件的命中数，"
                "可省大量 token。head_limit 限返回条数，offset 翻页。用于定位符号、报错、关键词。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "path": {"type": "string", "description": "起始目录或文件，默认 .", "default": "."},
                    "max_hits": {
                        "type": "integer",
                        "description": "content 模式最多命中条数（旧参数，可用 head_limit 替代）",
                        "default": MAX_GREP_HITS,
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files", "count"],
                        "description": "content=带行号文本；files=命中文件清单；count=每文件命中数",
                        "default": "content",
                    },
                    "head_limit": {
                        "type": "integer",
                        "description": "返回条数上限（默认同 max_hits）",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "跳过前 N 条（files/count 模式下指文件行）",
                        "default": 0,
                    },
                },
                "required": ["pattern"],
            },
            category=ToolCategory.READ,
            fn=grep,
        ),
        Tool(
            name="write_file",
            description=(
                "新建文件或整体重写已有文件（工作区内）。会先记录改动前的副本，可用 /undo 回滚。"
                "写代码文件时请提供完整的新内容。已有文件的小改动请改用 file_edit 精确替换。"
                "对已存在的文件，必须先 read_file 过且文件未被外部改动才能写（防覆盖过期内容）。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要写入的文件路径"},
                    "content": {"type": "string", "description": "文件的完整新内容"},
                },
                "required": ["path", "content"],
            },
            category=ToolCategory.WRITE,
            fn=write_file,
        ),
        Tool(
            name="file_edit",
            description=(
                "在已有文件上做精确文本替换（工作区内，默认替换一处）。"
                "适合小改动：改一行、改函数名、加/删一段字段。old_string 必须能在当前文件内容里"
                "唯一匹配，找不到或匹配多处会报错——请先 read_file 确认当前内容再编辑，"
                "或用 replace_all=true 替换全部出现。替换前会记录改动前副本，可用 /undo 整体回滚。"
                "对已存在的文件，必须先 read_file 过且文件未被外部改动才能改。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要编辑的文件路径"},
                    "old_string": {"type": "string", "description": "要替换的原文片段（必须唯一匹配当前文件内容）"},
                    "new_string": {"type": "string", "description": "替换成的新文本（与 old_string 不同；可为空表示删除）"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "是否替换所有出现的 old_string（默认 false，只替换唯一的一处）",
                        "default": False,
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
            category=ToolCategory.WRITE,
            fn=file_edit,
        ),
    ]
