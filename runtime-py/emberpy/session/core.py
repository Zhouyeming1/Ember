"""JSONL 会话日志：落盘 / 重建模型消息 / 断点续跑（Session 实现）。

设计目标：
- 只追加（append-only），崩溃不会损坏已有记录
- 事件里直接存"给模型的原始消息 dict"，重建历史时零丢失、零猜测
- 不自动重放未完成的命令（对齐 Ember 的做法：崩溃后能继续对话，但不静默重放）

落盘格式 v2（桌面端 ember-core 索引器可识别，2026-09 起）：
  会话文件放在 <sessions 目录>/<文件名>.jsonl（顶层平铺；桌面端 refresh 会自动
  把旧文件分区进 YYYY/MM/DD 并建回平铺硬链，我们只需写平铺）。
  首行必须是 {"type":"session","cwd":"<工作区绝对路径>",...}，否则侧栏索引器直接跳过。
  消息行 {"type":"message","message":{"role":"user|assistant|tool","content":...}}：
    user/tool 的 content 是字符串；assistant 的 content 是 parts 数组
    （{type:thinking|text|toolCall,...}）——索引器靠它们取标题/摘要，模型消息靠它们还原。
  checkpoint 记成 type:"patch" 的自定义行（索引器忽略）。

向后兼容 v1：早期无头文件（meta/user/assistant/tool/patch 存原始事件行）仍可原样
读写——文件首行不是 session 头就退回 v1 模式，续写也保持 v1 行，文件内部自洽。

内存事件模型（events() 的返回）与 v1 完全一致：user/assistant 存原始模型消息 dict
（assistant 保留 reasoning_content / tool_calls），只改"文件边界"的序列化/加载。

事件类型（内存模型）：
- meta        {key, value}              会话元信息
- user        {message: {role, content}}
- assistant   {message: {role, content?, reasoning_content?, tool_calls?}}
- tool        {message: {role: tool, tool_call_id, content}}
- patch       {patch: {seq, path, before, after}}   改前/改后全文（供跨重启 /undo）
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..llm.base import without_reasoning
from ..patches import Patch
from .stats import SessionStats


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _utcnow_iso() -> str:
    """UTC 毫秒 ISO 串（带 Z 结尾），如 2026-09-08T08:00:00.123Z。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def resolve_sessions_dir() -> Path:
    """桌面端会话目录：桌面初始化会设给子进程的 env 优先，其次 ~/.ember/sessions。"""
    env = os.environ.get("EMBER_SESSIONS_DIR")
    if env and env.strip():
        return Path(env).expanduser().resolve()
    home = os.environ.get("EMBER_HOME")
    base = Path(home).expanduser() if home and home.strip() else Path.home() / ".ember"
    return (base / "sessions").resolve()


def allocate_session_path(sessions_dir: Optional[Path] = None) -> Path:
    """在会话目录里分配一个新的 .jsonl 路径（只定路径，不写文件）。

    文件名带毫秒时间戳 + uuid，保证唯一；字符全在 [A-Za-z0-9._-] 内，Windows 合法。
    """
    folder = Path(sessions_dir).expanduser().resolve() if sessions_dir is not None else resolve_sessions_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3]
    return folder / f"{stamp}_{uuid.uuid4().hex}.jsonl"


# ---------------------------------------------------------------------------
# 序列化 / 反序列化（v2 <-> v1 内存事件）
# ---------------------------------------------------------------------------


def _assistant_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    """把原始 assistant 消息 dict 摊成索引器友好的 parts 数组。

    顺序：thinking（reasoning_content）→ text（content）→ toolCall（每个 tool_call）。
    toolCall.arguments 保留原始 JSON 字符串，加载时才能精确还原成 function.arguments。
    """
    parts: list[dict[str, Any]] = []
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        parts.append({"type": "thinking", "thinking": reasoning})
    content = message.get("content")
    if isinstance(content, str) and content:
        parts.append({"type": "text", "text": content})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") if isinstance(call, dict) else None
        fn = fn if isinstance(fn, dict) else {}
        arguments = fn.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False, default=str)
        part: dict[str, Any] = {
            "type": "toolCall",
            "id": str(call.get("id", "")) if isinstance(call, dict) else "",
            "name": str(fn.get("name", "")),
            "arguments": arguments,
        }
        parts.append(part)
    return parts


def _text_of(content: Any) -> str:
    """从索引器风格的 content（字符串或 parts 数组）里取出纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                chunks.append(str(part.get("text") or ""))
        return "".join(chunks)
    return ""


def _assistant_from_parts(content: Any) -> dict[str, Any]:
    """把 parts 数组还原成原始 assistant 消息 dict（v1 内存事件形状）。

    text 拼回 content（纯工具轮无文本时置空串）；thinking 拼回 reasoning_content；
    toolCall parts 还原成 OpenAI 风格 tool_calls（arguments 原文 JSON 串）。
    """
    text_chunks: list[str] = []
    think_chunks: list[str] = []
    calls: list[dict[str, Any]] = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "text":
                text_chunks.append(str(part.get("text") or ""))
            elif kind == "thinking":
                think_chunks.append(str(part.get("thinking") or ""))
            elif kind == "toolCall":
                arguments = part.get("arguments", "{}")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False, default=str)
                calls.append(
                    {
                        "id": str(part.get("id", "")),
                        "type": "function",
                        "function": {"name": str(part.get("name", "")), "arguments": arguments},
                    }
                )
    text = "".join(text_chunks)
    message: dict[str, Any] = {"role": "assistant", "content": text if text or not calls else ""}
    if think_chunks:
        message["reasoning_content"] = "\n\n".join(think_chunks)
    if calls:
        message["tool_calls"] = calls
    return message


def closed_model_messages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从一段事件序列里重建"能直接发给模型"的闭合消息列表。

    规则与续跑裁剪一致：只取 user/assistant/tool 消息事件；丢弃找不到对应
    assistant 的孤立工具结果；若末尾存在"有 tool_calls 但缺工具结果"的半截
    assistant 轮次，把它及之后裁掉（避免 API 400）；回发前剥掉 assistant 的
    reasoning_content（DeepSeek reasoner 多轮规范）。compact 对"被压缩段"也
    用同一套裁剪，保证喂给摘要模型的消息序列合法。
    """
    result: list[dict[str, Any]] = []
    pending: set[str] = set()
    for event in events:
        if event.get("type") not in ("user", "assistant", "tool"):
            continue
        msg = event["message"]
        role = msg.get("role")
        if role == "assistant":
            result.append(msg)
            for call in msg.get("tool_calls") or []:
                pending.add(call.get("id", "") if isinstance(call, dict) else "")
        elif role == "tool":
            # 找不到对应 assistant 的孤立工具结果直接丢弃
            if msg.get("tool_call_id") in pending:
                pending.discard(msg.get("tool_call_id"))
                result.append(msg)
        else:  # user
            result.append(msg)

    if pending:
        # 从后往前找最后一个引入了未闭合 tool_calls 的 assistant，裁掉它及其后
        cut: Optional[int] = None
        for index in range(len(result) - 1, -1, -1):
            msg = result[index]
            calls = msg.get("tool_calls") or []
            if msg.get("role") == "assistant" and calls:
                if any((call.get("id", "") if isinstance(call, dict) else "") in pending for call in calls):
                    cut = index
                    break
        if cut is not None:
            result = result[:cut]
    # 推理文本（reasoning_content）只展示给人看，回发模型前必须剥掉
    return [without_reasoning(m) if m.get("role") == "assistant" else m for m in result]


def _last_user_event_index(events: list[dict[str, Any]]) -> int:
    """最后一个 user 消息事件的下标；没有 user 返回 -1（compact 的保留起点）。"""
    found = -1
    for index, event in enumerate(events):
        if event.get("type") == "user":
            found = index
    return found


class Session:
    """一个会话。

    path 为 None 时只保存在内存（适合一次性 run 命令）；
    指定 path 时以追加模式写盘，重建消息历史会读回已有内容。

    v2 格式写入：新文件先写 session 头；每类事件序列化成索引器能识别的行。
    旧 v1 无头文件：检测到首行非 session 头就保持 v1 原样读写，保证兼容。
    """

    def __init__(self, path: Optional[Path] = None, cwd: Optional[Path] = None) -> None:
        self.path = Path(path).expanduser().resolve() if path is not None else None
        self.cwd = cwd
        self._events: list[dict[str, Any]] = []
        self._v2 = False
        self._session_id: Optional[str] = None
        self._header: Optional[dict[str, Any]] = None  # v2 首行头（compact 整文件重写时复用）
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > 0:
            with self.path.open("r", encoding="utf-8") as handle:
                lines = [line.strip() for line in handle if line.strip()]
            if lines and _is_v2_header(lines[0]):
                self._v2 = True
                try:
                    self._header = json.loads(lines[0])
                except json.JSONDecodeError:
                    self._header = None
                for raw in lines[1:]:
                    self._consume_v2_line(raw)
            else:
                self._v2 = False
                for raw in lines:
                    try:
                        self._events.append(json.loads(raw))
                    except json.JSONDecodeError:
                        continue  # 坏行不致命：跳过，保留其它内容
        else:
            # 新文件：先写 session 头，保证侧栏索引器能识别（cwd 必须绝对路径）
            self._v2 = True
            self._write_header()

    # -- 磁盘写入 --------------------------------------------------------
    def _write_line(self, line: str) -> None:
        # 逐条 open(append) 写完即关：不长期占住句柄，Windows 上桌面端的
        # 分区 rename/硬链不会被"正打开的文件"挡住；append-only 崩溃安全不变。
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _write_header(self) -> None:
        self._session_id = uuid.uuid4().hex
        header = {
            "type": "session",
            "version": 3,
            "id": self._session_id,
            "timestamp": _utcnow_iso(),
            "cwd": str(self.cwd) if self.cwd else str(Path.cwd().resolve()),
        }
        self._header = header
        self._write_line(_dump(header))

    def _append(self, event: dict[str, Any]) -> None:
        self._events.append(event)
        if self.path is not None:
            out = self._serialize(event) if self._v2 else event
            self._write_line(_dump(out))

    def replace_events(self, new_events: list[dict[str, Any]]) -> None:
        """把整段历史换成新事件（compact 用），并原子重写磁盘文件。

        之后继续 _append 只把后续事件追加到重写后的文件上，现场与落盘一致。
        非 v2（纯内存 / 旧 v1 无头文件）：只改内存 _events，不碰磁盘。
        """
        self._events = [event for event in new_events]
        if self.path is not None and self._v2 and self._header is not None:
            # 先写临时文件再原子替换：压缩是"重写历史"的破坏性操作，不能因中途崩溃
            # 留下半截文件（append-only 的崩溃安全在这里不再成立，靠原子替换兜底）。
            tmp = self.path.with_name(self.path.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(_dump(self._header) + "\n")
                for event in self._events:
                    handle.write(_dump(self._serialize(event)) + "\n")
            os.replace(tmp, self.path)

    def _serialize(self, event: dict[str, Any]) -> dict[str, Any]:
        """v2：把 user/assistant/tool 事件序列化成索引器能识别的 message 行；其它原样。"""
        etype = event.get("type")
        if etype in ("user", "assistant", "tool"):
            message = event.get("message") or {}
            role = message.get("role")
            if role == "user":
                return {"type": "message", "message": {"role": "user", "content": message.get("content") or ""}}
            if role == "assistant":
                return {"type": "message", "message": {"role": "assistant", "content": _assistant_parts(message)}}
            if role == "tool":
                return {
                    "type": "message",
                    "message": {
                        "role": "tool",
                        "tool_call_id": message.get("tool_call_id"),
                        "content": message.get("content") or "",
                    },
                }
        return event  # meta / patch 等自定义行原样写（索引器忽略）

    def _consume_v2_line(self, raw: str) -> None:
        """把一行 v2 JSON 还原进 _events（v1 内存事件形状）。"""
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(line, dict):
            return
        etype = line.get("type")
        if etype == "message":
            message = line.get("message")
            if not isinstance(message, dict):
                return
            role = message.get("role")
            if role == "user":
                self._events.append({"type": "user", "message": {"role": "user", "content": _text_of(message.get("content"))}})
            elif role == "assistant":
                self._events.append({"type": "assistant", "message": _assistant_from_parts(message.get("content"))})
            elif role == "tool":
                self._events.append(
                    {
                        "type": "tool",
                        "message": {
                            "role": "tool",
                            "tool_call_id": message.get("tool_call_id"),
                            "content": _text_of(message.get("content")),
                        },
                    }
                )
        elif etype == "meta":
            self._events.append({"type": "meta", "key": line.get("key"), "value": line.get("value")})
        elif etype == "patch":
            patch = line.get("patch")
            if isinstance(patch, dict):
                self._events.append({"type": "patch", "patch": patch})
        # session 头 / model_change / session_info 等外部行忽略

    # -- 事件写入 --------------------------------------------------------
    def meta(self, key: str, value: Any) -> None:
        self._append({"type": "meta", "key": key, "value": value})

    def add_user(self, content: str) -> None:
        self._append({"type": "user", "message": {"role": "user", "content": content}})

    def add_assistant(self, message: dict[str, Any]) -> None:
        """message 应是模型返回的原始 assistant 消息 dict（含 tool_calls 则原样保存）。"""
        self._append({"type": "assistant", "message": message})

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self._append({
            "type": "tool",
            "message": {"role": "tool", "tool_call_id": tool_call_id, "content": content},
        })

    def add_patch(self, patch: Patch) -> None:
        """记录一次写文件 checkpoint（存改前/改后全文，供跨重启 /undo 还原）。"""
        self._append({
            "type": "patch",
            "patch": {
                "seq": patch.seq,
                "path": str(patch.path),
                "before": patch.before,
                "after": patch.after,
            },
        })

    # -- 读取 ------------------------------------------------------------
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def messages(self) -> list[dict[str, Any]]:
        """重建给模型的对话消息（丢弃 meta / patch 等非消息事件）。"""
        return [event["message"] for event in self._events if event.get("type") in ("user", "assistant", "tool")]

    def resume_messages(self) -> list[dict[str, Any]]:
        """续跑用的历史消息。

        崩溃可能留下"有 tool_calls 但缺工具结果"的半截 assistant 轮次，
        直接发给模型会被 API 拒绝。这里把未完成的那一轮及之后裁掉
        （对齐设计原则：可以续对话，但不静默重放未完成的动作）。
        """
        return closed_model_messages(self._events)

    def stats(self) -> SessionStats:
        stats = SessionStats()
        for event in self._events:
            etype = event.get("type")
            if etype == "user":
                stats.user_messages += 1
            elif etype == "assistant":
                stats.assistant_messages += 1
                if event.get("message", {}).get("tool_calls"):
                    stats.tool_calls += len(event["message"]["tool_calls"])
            elif etype == "tool":
                stats.tool_results += 1
            elif etype == "patch":
                stats.patches += 1
        return stats

    def close(self) -> None:
        # v2 采用"逐条写完即关"，无常开句柄需要释放；close() 保留为兼容 no-op
        pass

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _is_v2_header(raw: str) -> bool:
    """首行是否为 session 头（type=session 且 cwd 是字符串）。"""
    try:
        line = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return isinstance(line, dict) and line.get("type") == "session" and isinstance(line.get("cwd"), str)
