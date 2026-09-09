"""记忆工具：让模型读写跨会话记忆（memory_save / read / list / dir）。

与 fs 工具的区别：这些工具只操作记忆目录（memdir，EMBER_HOME/projects/.../memory），
**不碰工作区**，因此不走文件权限门（写的是引擎自己的记忆，不是用户代码）；名字已由
MemoryStore 校验（安全字符 / 防穿越 / 防覆盖 MEMORY.md），可安全作为文件名。

memory_save 自动完成 claude 的两步行（写文件 + 更新 MEMORY.md 索引）。
"""
from __future__ import annotations

from typing import Any

from ..memory import MEMORY_TYPES, MemoryStore
from .registry import Tool, ToolCategory


def _err(exc: Exception) -> str:
    return f"错误：{exc}"


def _type_help() -> str:
    return "、".join(MEMORY_TYPES)


def build_memory_tools(store: MemoryStore) -> list[Tool]:
    def memory_save(name: str, type: str, content: str, description: str = "") -> str:
        try:
            result = store.save(name, description=description, type_=type, content=content)
        except Exception as exc:
            return _err(exc)
        verb = "新存" if result.created else "更新"
        return f"已{verb}记忆 {name!r}（type={type}）。文件：{result.path}；MEMORY.md 索引已同步。"

    def memory_read(name: str) -> str:
        try:
            return store.read(name)
        except Exception as exc:
            return _err(exc)

    def memory_list() -> str:
        metas = store.list()
        if not metas:
            return f"（记忆目录 {store.root} 还没有任何记忆。）"
        lines = [f"记忆目录：{store.root}，共 {len(metas)} 条（最新在前）："]
        for m in metas:
            tag = f"[{m.type_}] " if m.type_ else ""
            hook = m.description or m.path.name
            lines.append(f"- {tag}{m.name} — {hook}")
        return "\n".join(lines)

    def memory_dir() -> str:
        return f"记忆目录：{store.root}"

    return [
        Tool(
            name="memory_save",
            description=(
                "保存一条跨会话记忆（新建或覆盖同名条目）。name 用语义化短名（只含字母数字._-，"
                f"如 user_preferences / feedback_terse）；type 是 {'/'.join(MEMORY_TYPES)}："
                "user=用户身份与偏好，feedback=纠正/确认过的做事方式（附 why），project=项目目标/约束"
                "（非代码可推导），reference=外部资源指针。content 正文写清事实与原因。会同时更新 MEMORY.md 索引。"
            ),
            parameters={
                "properties": {
                    "name": {"type": "string", "description": "记忆名字（语义化短名，字母数字._-）"},
                    "type": {"type": "string", "description": _type_help()},
                    "content": {"type": "string", "description": "记忆正文：事实 + 为什么（Why）"},
                    "description": {"type": "string", "description": "一句话提示，出现在 MEMORY.md 索引里（可省略）"},
                },
                "required": ["name", "type", "content"],
            },
            category=ToolCategory.WRITE,
            fn=memory_save,
        ),
        Tool(
            name="memory_read",
            description="读一条已存记忆的全文（frontmatter + 正文）。先 memory_list 看有什么。",
            parameters={
                "properties": {"name": {"type": "string", "description": "记忆名字（与 memory_save 一致）"}},
                "required": ["name"],
            },
            category=ToolCategory.READ,
            fn=memory_read,
        ),
        Tool(
            name="memory_list",
            description="列出记忆目录里全部记忆的名字与一句话提示（最新在前）。",
            parameters={"properties": {}, "required": []},
            category=ToolCategory.READ,
            fn=memory_list,
        ),
        Tool(
            name="memory_dir",
            description="返回记忆目录的绝对路径。",
            parameters={"properties": {}, "required": []},
            category=ToolCategory.READ,
            fn=memory_dir,
        ),
    ]
