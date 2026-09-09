"""回合末记忆自动抽取（#10，对齐 claude extractMemories 的单次调用版）。

claude 原样 = 回合末 fork 一个"只读 + memory 工具"的子 agent 自己去重后写
memdir。本引擎采纳更轻的方案（方案 B）：**单次非流式 LLM 调用**——喂"本轮紧凑
工作档案 + 现有记忆索引"，模型一次回 JSON 记忆数组，引擎侧确定性 parse/去重/
校验后逐条 ``MemoryStore.save``。不做 fork 子 agent，理由见 MIGRATION-ROADMAP
十五：记忆写入只是原子 store.save，套子 agent 循环是浪费，且子 agent 现不继承
memory（M8 有意裁剪），为它新开记忆挂接正是要避免的复杂语义。

门槛/语义：
- 触发由 worker 在每轮收尾调用（见 worker._auto_extract_memories），本模块只含
  纯逻辑 + LLM 编排，离线可测（不 import worker / session / agent）。
- 默认**关闭**：env ``EMBERPY_AUTO_MEMORY`` 置 1/true/yes/on 才开。
  ``should_extract_turn`` 只做"开了之后跳过琐碎寒暄轮"的第二道闸。
- 去重只做到"同名覆盖"：同话题新名会另起一条，靠 prompt 引导模型复用 name。
- 本模块不抛异常：LLM 回散文/空 / 单条坏 entry 都不中断（坏条目丢弃记 dropped）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .store import MEMORY_TYPES, MemoryStore, _NAME_RE

AUTO_MEMORY_ENV = "EMBERPY_AUTO_MEMORY"
MIN_FINAL_CHARS = 80  # 无工具纯寒暄轮的最终答复长度下限（低于它不抽）

# 喂给模型的工作档案各自封顶（字符/条数），控制单次抽取成本
TASK_CHARS = 500
FINAL_CHARS = 1200
PATCH_LINES_MAX = 15
ERROR_SNIPPETS_MAX = 3
ERROR_CHARS = 200

# 与 bridge/worker 的 _IS_ERROR_PREFIXES 同口径：工具把权限拒绝/异常都转文本返回
_ERROR_PREFIXES = ("错误：", "权限拒绝：", "执行出错：")


def auto_memory_enabled() -> bool:
    """env 门控（默认关）：EMBERPY_AUTO_MEMORY = 1/true/yes/on 视为开。"""
    return os.environ.get(AUTO_MEMORY_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def min_final_chars() -> int:
    """纯寒暄轮的最终答复长度下限（env EMBERPY_AUTO_MEMORY_MIN_FINAL_CHARS 覆盖）。"""
    raw = os.environ.get("EMBERPY_AUTO_MEMORY_MIN_FINAL_CHARS")
    if raw is None:
        return MIN_FINAL_CHARS
    try:
        return int(raw)
    except ValueError:
        return MIN_FINAL_CHARS


def should_extract_turn(
    *,
    final_text: str,
    patch_count: int,
    tool_count: int,
    min_chars: Optional[int] = None,
) -> bool:
    """本轮值不值得跑一次抽取。

    - 有文件改动（patch）→ 一定抽（改动是最该留档的事实）；
    - 无改动、无工具、最终答复空/过短 → 琐碎寒暄，跳过；
    - 其余（长答复 / 有工具操作）→ 抽。
    """
    if patch_count > 0:
        return True
    final = (final_text or "").strip()
    limit = min_chars if min_chars is not None else min_final_chars()
    if tool_count == 0 and len(final) < limit:
        return False  # 琐碎寒暄（含空/过短答复）
    return True


# ---------------------------------------------------------------------------
# 抽取 prompt
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM_PROMPT = (
    "你是 coding-agent 的长期记忆策展人。每轮工作结束后，系统会给你一份"
    "「本轮工作档案」和当前已有的记忆索引。你的任务：把**值得跨会话保留**的"
    "事实提炼成记忆条目（用 JSON 数组返回），供以后的会话在接到新任务时回忆。\n\n"
    "## 四种类型\n"
    "- user：用户的身份/角色/技能/沟通偏好（最重要，帮助按其水平与偏好协作）；\n"
    "- feedback：用户纠正过「别这么做」或确认过「这样对」的做事方式（正文存原因 why）；\n"
    "- project：正在推进的工作/目标/约束/背景（用户说的、代码与 git 推不出来的）；\n"
    "- reference：外部系统/资源的指针（bug 在哪个面板、URL、账号归属…）。\n\n"
    "## 铁律\n"
    "1. **更新优于新建**：同一话题已有记忆（看索引里的 name）就复用它的 name，"
    "用新事实覆盖旧正文，绝不另起新名造成重复条目；\n"
    "2. **只存跨会话有用**：只对「以后别的会话」有保留价值才存。本轮问答现场、"
    "代码结构/架构/文件路径（重读代码就有）、git 历史、一次性的修复解法不存；\n"
    "3. **宁缺毋滥**：没有值得留档的就返回空数组 []，不要为凑数硬造记忆；\n"
    "4. name 用语义化 ASCII 短名（小写字母数字和 . _ -，如 user_preferences、"
    "feedback_terse、project 的短主题），别用句子；\n"
    "5. content 写清**事实 + 为什么**，简短明确（一两句到一小段），中文；\n"
    "6. description 是一行一句话提示（会出现在 MEMORY.md 索引行里）。\n\n"
    "## 输出\n"
    "只输出一个 JSON 数组，不要 markdown 代码围栏、不要多余解释。每个元素：\n"
    '{"name": "短名", "type": "user|feedback|project|reference", '
    '"content": "正文", "description": "一句话提示"}\n'
    "没有可留档内容就输出 []。"
)


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（原文过长，截断至 {limit} 字符）"


def build_extract_messages(
    task: str,
    final: str,
    patch_digest: str,
    tool_digest: str,
    index_block: str,
) -> list[dict[str, str]]:
    """组装抽取调用的消息：system 策展指令 + 本轮紧凑档案（user）。

    档案内容已由调用方折好（patch_digest / tool_digest 是文本块），这里只做
    总装与 task/final 的字符封顶。index_block 为 store.index_text()（可能为空串）。
    """
    task = _clip(task, TASK_CHARS)
    final = _clip(final, FINAL_CHARS)
    index = (index_block or "").strip() or "（还没有任何记忆）"
    user = (
        "# 本轮工作档案\n\n"
        f"## 用户任务\n{task or '（无）'}\n\n"
        f"## 你的最终答复\n{final or '（无）'}\n\n"
        f"## 本轮文件改动\n{patch_digest or '（无）'}\n\n"
        f"## 本轮工具轨迹\n{tool_digest or '（无工具）'}\n\n"
        f"## 已有记忆索引（复用的 name 从这里挑）\n{index}\n\n"
        "请按 system 规则提炼，返回 JSON 记忆数组。"
    )
    return [
        {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# 档案折叠：patches / 工具轨迹
# ---------------------------------------------------------------------------


def patch_lines(patches, workspace) -> str:
    """把本轮 Patch 折成「动作 相对路径」清单（封顶 PATCH_LINES_MAX 行）。

    workspace 是工作区绝对路径；patch.path 在工作区外的（理论不该有）退化到
    文件名。只列改动事实，不含 before/after 正文（太大，进档案没必要）。
    """
    ws = Path(workspace).resolve()
    all_patches = list(patches)
    lines = []
    for patch in all_patches[:PATCH_LINES_MAX]:
        path = patch.path
        try:
            rel = path.relative_to(ws).as_posix()  # 统一正斜杠（跨平台档案文本）
        except (ValueError, AttributeError):
            rel = Path(path).name
        action = "新建" if patch.before is None else ("删除" if patch.after is None else "修改")
        lines.append(f"{action} {rel}")
    if len(all_patches) > PATCH_LINES_MAX:
        lines.append(f"…另有 {len(all_patches) - PATCH_LINES_MAX} 处改动未列出")
    return "\n".join(lines)


def tool_digest(events) -> tuple[list[str], list[str]]:
    """从一轮事件切片里抽出 (工具名清单去重保序, 报错片段)。

    events 是 session 事件 dict（type=assistant/tool/…）：assistant 消息携带
    tool_calls 得工具名；tool 事件的 content 命中错误前缀（错误：/权限拒绝：/
    执行出错：）时收为报错片段。无工具/无错都回空列表。
    """
    names: list[str] = []
    errors: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        message = event.get("message") or {}
        if etype == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                name = fn.get("name")
                if isinstance(name, str) and name:
                    names.append(name)
        elif etype == "tool":
            content = message.get("content")
            if isinstance(content, str) and content.startswith(_ERROR_PREFIXES):
                errors.append(content[:ERROR_CHARS])
    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique, errors[:ERROR_SNIPPETS_MAX]


def tool_trail(names: list[str], errors: list[str]) -> str:
    """把 tool_digest 的两半折成档案文本块（供 worker 喂 build_extract_messages）。"""
    parts: list[str] = []
    if names:
        parts.append("用到的工具：" + "、".join(names))
    if errors:
        for err in errors:
            parts.append("报错片段：" + err.replace("\n", " ")[:ERROR_CHARS])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 严格 parse + 去重合并
# ---------------------------------------------------------------------------


def parse_memory_payload(text: str) -> tuple[list[dict[str, str]], list[str]]:
    """把模型回复解析成候选记忆（严格）。

    取首个 ``[`` 到末个 ``]`` 后 json.loads；必须是数组；每条 name 过 store 的
    _NAME_RE、type 在闭集、content 非空才收。坏条目进 issues 丢弃——**单条坏
    不整体失败**；完全不是 JSON 时回 (空, issues)。
    """
    issues: list[str] = []
    if not isinstance(text, str) or not text.strip():
        return [], ["模型回复为空"]
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return [], ["模型回复里没有 JSON 数组"]
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        return [], [f"JSON 解析失败：{exc}"]
    if not isinstance(data, list):
        return [], ["顶层不是数组"]
    valid: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            issues.append(f"条目不是对象：{item!r}")
            continue
        name = item.get("name")
        type_ = item.get("type")
        content = item.get("content")
        description = item.get("description") or ""
        name = name.strip() if isinstance(name, str) else ""
        if not _NAME_RE.match(name):
            issues.append(f"name 不合法：{name!r}（应是小写字母数字._-的短名）")
            continue
        if type_ not in MEMORY_TYPES:
            issues.append(f"type 不合法：{type_!r}（应为 {'/'.join(MEMORY_TYPES)}）")
            continue
        if not isinstance(content, str) or not content.strip():
            issues.append(f"{name!r} 的 content 为空")
            continue
        valid.append(
            {
                "name": name,
                "description": " ".join((description or "").split()),  # 压成单行
                "type": type_,
                "content": content.strip(),
            }
        )
    return valid, issues


@dataclass
class MergeReport:
    """一次抽取合并的落盘统计（saved = created + updated）。"""

    created: int = 0
    updated: int = 0
    dropped: int = 0

    @property
    def saved(self) -> int:
        return self.created + self.updated


def merge_memories(store: MemoryStore, candidates: list[dict[str, str]]) -> MergeReport:
    """把已校验的候选合并进记忆库：同名 → 覆盖（索引天然不重复），新名 → 新建。

    防御性再检一遍（content 空 / type 非法 / save 异常都逐条 drop 不中断），
    同名候选只保留最后一条，避免同轮重复 save 噪音。
    """
    report = MergeReport()
    dedup: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        dedup[candidate["name"]] = candidate  # 同名后写覆盖前写
    for candidate in dedup.values():
        if candidate["type"] not in MEMORY_TYPES or not (candidate.get("content") or "").strip():
            report.dropped += 1
            continue
        try:
            result = store.save(
                name=candidate["name"],
                description=candidate.get("description", ""),
                type_=candidate["type"],
                content=candidate["content"],
            )
        except Exception:
            report.dropped += 1  # 单条写入失败不中断整批
            continue
        if result.created:
            report.created += 1
        else:
            report.updated += 1
    return report


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


def run_memory_extraction(
    llm,
    store: MemoryStore,
    *,
    task: str,
    final: str,
    patch_digest: str,
    tool_digest: str,
) -> MergeReport:
    """完整跑一次抽取：build → complete(无工具) → parse → merge。

    绝不抛异常：reply 为 None / 无 content / 非 JSON 都回空报告；merge 对坏条目
    自容忍。索引文本在内部取 store.index_text()（引擎已封顶 200 行/25KB）。
    """
    messages = build_extract_messages(task, final, patch_digest, tool_digest, store.index_text())
    try:
        reply = llm.complete(messages, tools=None)
    except Exception:
        return MergeReport()  # 抽取失败绝不影响主流程
    if reply is None:
        return MergeReport()
    content = getattr(reply, "content", None)
    if not isinstance(content, str) or not content.strip():
        return MergeReport()
    candidates, _issues = parse_memory_payload(content)
    if not candidates:
        return MergeReport()
    return merge_memories(store, candidates)
