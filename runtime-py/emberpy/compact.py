"""旧对话压缩（compact）——把长历史归纳成一条摘要，释放模型上下文。

复刻 claude-code-analysis `services/compact/` 的行为设计（只借鉴接口/行为，
代码自写）。与 claude 对齐的关键点：
- 触发：每轮 prompt 开始前 **proactive** 检测（`should_auto_compact`），token
  估算 ≥ 阈值才压；手动 `/compact` 无条件尝试、但"太短"要报 nothing-to-compact。
- 阈值：`autoCompactThreshold = contextWindow − min(maxOutput, 20_000) − 13_000`，
  即给"压缩摘要输出 + 安全缓冲"留足空间（claude 的 AUTOCOMPACT_BUFFER=13k、
  MAX_OUTPUT_FOR_SUMMARY=20k）。
- 摘要回填：旧段删除、换成一条携带摘要的消息，后续轮次把摘要当上下文续用。
- 防重复压缩水位：claude 用 system compact_boundary 标记；我们**不做**——旧消息
  被物理移除后，"水位之后 = 全部当前消息"，重复压缩天然只处理新增段（成本可忽略，
  见模块底部注记）。

与 claude 的差异（有意裁剪，本引擎阶段约束）：
- claude 全量压缩不留 verbatim 尾部，靠"重注入最近读过的文件"恢复近期上下文；
  我们没有这套附件重注入机制 → 改为**保留"最后一个 user 消息及其后的整轮"原样**，
  只压缩它之前的旧段。既有最近现场、语义也干净（压缩旧对话，保留当前问答）。
- claude 把摘要包装成 user 消息且 `isVisibleInTranscriptOnly`（UI 隐藏）；Ember
  renderer 没有该隐藏位 → 用 **assistant 消息 + 中文 marker 行**，在转录里如实可见。
- 9 段式 `<summary>` 结构按我们的语言/技术栈重写（参考其意图，不抄原文）。
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from .llm import LLM
from .session import closed_model_messages

# 摘要输出预留 + 自动压缩安全缓冲（对齐 claude 常量）
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
AUTOCOMPACT_BUFFER_TOKENS = 13_000

# 每个模型的默认输出上限（deepseek-chat / reasoner 都按此保守预留）
_DEFAULT_MAX_OUTPUT_TOKENS = 8_192

# 多少字符约等于一个 token（纯粗算：无 usage 统计阶段的回退口径）。
# claude 用 ≈4 chars/token；我们中文多，用 ÷3 更贴实（文档写清楚是粗算）。
_CHARS_PER_TOKEN = 3

# 手动 /compact 认为"对话太短不值得压"的旧段 token 下限。
# 小于它抛 nothing to compact（renderer 正则 /nothing to compact|session too short/ 匹配）。
MIN_COMPACT_TOKENS = 600


def min_compact_tokens() -> int:
    """旧段 token 下限（可被 env 覆盖：EMBERPY_COMPACT_MIN_TOKENS，测试缩阈用）。"""
    return env_int("EMBERPY_COMPACT_MIN_TOKENS", MIN_COMPACT_TOKENS)

# 保留"最后一个 user 之后"的整轮不动，只压缩它前面的旧消息。
# 这一刀语义由 compact 本身决定，与 token 预算无关。


def _chars_to_tokens(char_len: int, per_token: int = _CHARS_PER_TOKEN) -> int:
    """字符数 -> 粗略 token 数（默认 ÷3 后取整；空串为 0，非空至少 1）。"""
    if not char_len:
        return 0
    return max(1, round(char_len / per_token))


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：纯文本字符数 ÷3（无 usage 时的回退口径，见模块 docstring）。"""
    return _chars_to_tokens(len(text))


def _message_char_split(message: dict[str, Any]) -> tuple[int, int]:
    """一条消息拆成 (普通文本字符数, 稠密字符数)。

    稠密内容（工具结果、tool_calls 的 JSON 参数）按 ÷2 估 token——结构紧凑、英文
    居多，比普通对话文本（÷3）更贴实。中文消息 ÷3 仍比 claude 的 ÷4 保守。
    返回值是字符数（int），token 折算交给 _chars_to_tokens。
    """
    role = message.get("role")
    content = message.get("content")
    text = 0
    dense = 0
    if role == "tool":
        # 工具结果：多为 JSON/命令输出/文件片段，按稠密计
        if isinstance(content, str):
            dense += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    dense += len(part["text"])
        return text, dense
    if isinstance(content, str):
        text += len(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                chunk = part.get("text")
                if isinstance(chunk, str):
                    text += len(chunk)
                thinking = part.get("thinking")
                if isinstance(thinking, str):
                    text += len(thinking)
    # assistant 消息的 tool_calls 参数（JSON）按稠密计
    for call in message.get("tool_calls") or []:
        fn = call.get("function") if isinstance(call, dict) else None
        fn = fn if isinstance(fn, dict) else {}
        arguments = fn.get("arguments")
        if isinstance(arguments, str):
            dense += len(arguments)
        elif arguments is not None:
            dense += len(json.dumps(arguments, ensure_ascii=False))
    return text, dense


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """按内容类型分权估算整段消息的 token 数（比统一 ÷3 更准）。

    普通文本 ÷3；工具结果与 JSON 工具参数 ÷2。压缩触发线据此更贴真实占用。
    """
    total = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        text, dense = _message_char_split(message)
        total += _chars_to_tokens(text) + _chars_to_tokens(dense, per_token=2)
    return total


def effective_context_window(context_window: int, max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS) -> int:
    """压紧可用窗 = contextWindow − 摘要输出预留（claude getEffectiveContextWindowSize）。"""
    reserve = min(max_output_tokens, MAX_OUTPUT_TOKENS_FOR_SUMMARY)
    return context_window - reserve


def auto_compact_threshold(context_window: int, max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS) -> int:
    """自动压缩触发线 = 生效窗 − 13k 缓冲（claude getAutoCompactThreshold）。"""
    return effective_context_window(context_window, max_output_tokens) - AUTOCOMPACT_BUFFER_TOKENS


def should_auto_compact(messages: list[dict[str, Any]], threshold: int) -> bool:
    """消息估算 token ≥ 阈值才需要自动压缩（proactive 判定）。"""
    if not messages:
        return False
    return estimate_messages_tokens(messages) >= threshold


# ---------------------------------------------------------------------------
# 压缩 prompt（结构参考 claude，中文重写）
# ---------------------------------------------------------------------------


COMPACT_SYSTEM_PROMPT = (
    "你是一个对话压缩助手。下面会给你一段 coding-agent 与其用户的旧对话"
    "（多轮，可能夹杂工具调用与工具输出）。请把它压缩成一段结构化中文摘要，"
    "必须覆盖以下方面：\n"
    "1. 用户的主要请求与意图；\n"
    "2. 关键技术点、做出的设计与决策；\n"
    "3. 改动过的文件路径与代码位置（尽量保留准确的相对路径与文件名）；\n"
    "4. 遇到的错误与修复方式；\n"
    "5. 问题解决的关键过程；\n"
    "6. 对话里出现过的全部用户消息（尽量逐条简短概括）；\n"
    "7. 尚未完成的任务/待办；\n"
    "8. 当前的工作进度；\n"
    "9. 下一步该做什么。\n"
    "保留对继续工作有用的事实与准确引用，省略寒暄与无关细节。"
    "只输出摘要正文本身，不要输出任何解释、不要调用工具。"
)


def build_compact_messages(
    segment: list[dict[str, Any]],
    extra_instruction: Optional[str] = None,
) -> list[dict[str, Any]]:
    """组装给"压紧模型"的消息：system 指令 + 旧段全文 + 收尾请求。

    extra_instruction 供后续 /compact 自定义指令扩展（本期 UI 不传，保留钩子）。
    """
    system = COMPACT_SYSTEM_PROMPT
    if extra_instruction and extra_instruction.strip():
        system += "\n\n额外要求：" + extra_instruction.strip()
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    messages.extend(segment)
    messages.append({"role": "user", "content": "请输出上面这段对话的压缩摘要。"})
    return messages


def summarize(llm: LLM, segment: list[dict[str, Any]], extra_instruction: Optional[str] = None) -> str:
    """调模型把旧段压成摘要文本（无工具）。失败抛异常由上层决定是否熔断。"""
    reply = llm.complete(build_compact_messages(segment, extra_instruction), tools=None)
    if reply is None or not reply.content or not reply.content.strip():
        raise RuntimeError("压缩摘要为空：模型没返回可用的摘要文本")
    return reply.content.strip()


# ---------------------------------------------------------------------------
# 摘要消息的组装（回填历史）
# ---------------------------------------------------------------------------


def summary_text(summary: str, removed: int, tokens_before: int) -> str:
    """压缩产物可见文本：marker 行 + 摘要（assistant 角色承载，转录里如实显示）。"""
    return (
        f"＞ 已压缩 {removed} 条较旧消息（压缩前约 {tokens_before:,} tokens），"
        f"旧对话归纳为以下摘要：\n\n{summary}"
    )


def split_old_events(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把事件序列切成 [旧段, 保留尾]：保留"最后一个 user 消息及之后"整轮。

    返回 (old_events, keep_events)；没有 user（纯助手历史）时 old 为空。
    保留尾从最后一个 user 事件开始——它在重放/续跑里一定是合法轮次起点。
    """
    from .session.core import _last_user_event_index  # 内部助手，避免扩公共 API

    cut = _last_user_event_index(events)
    if cut < 0:
        return [], list(events)
    return events[:cut], events[cut:]


def old_segment_messages(old_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把旧段事件裁成"能喂给压紧模型"的闭合消息（孤工具结果/半截轮次/推理全处理）。"""
    return closed_model_messages(old_events)


# ---------------------------------------------------------------------------
# 测试/配置辅助
# ---------------------------------------------------------------------------


def env_int(name: str, default: int) -> int:
    """读 env 整数覆盖（测试缩阈值用；无则回默认）。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default
