"""UsageTracker：把每次模型响应的 usage 归一化成"Anthropic 语义"并累计。

Ember UI 的 Context & usage 面板（AgentSessionStats.tokens / contextUsage）按
Anthropic 口径设计：input 不含 cache、cache read / cache write 单列、上下文占用 =
input + cache_read + cache_write（不含 output）。我们的引擎走 DeepSeek（OpenAI
兼容 usage），字段不同，在这里做一次归一化，累加结果直接喂给面板，无需前端改动。

DeepSeek usage 形状（OpenAI 兼容 + 缓存记账）：
    prompt_tokens / completion_tokens / total_tokens
    prompt_cache_hit_tokens            -> 命中缓存 = cache read
    prompt_cache_miss_tokens           -> 未命中并写入缓存 = cache write(creation)
    （DeepSeek 把整个 prompt 都计入 hit/miss，故 prompt_tokens ≈ hit + miss，
      完全"新鲜且不缓存"的 input 通常为 0 —— 与 Anthropic 的 input_tokens 对应）
无 cache 字段的其它 OpenAI 兼容网关：hit=miss=0 -> input 即整个 prompt。

cost 用每 1M token 的美元价按分量计价（对齐 claude tokensToUSDCost 的分量式
算法），默认 DeepSeek 官方价，可用 env 覆盖（价格非密钥，可硬编码默认值）。
"""
from __future__ import annotations

import os
from typing import Any, Optional

# -- 默认单价（USD / 1M token；DeepSeek 官方定价，生产请按真实账单校准） ---------
# cache_write 在 DeepSeek 没有单独档位，未命中输入即"写入缓存"，计输入价。
_DEFAULT_PRICES = {
    "input": 0.27,       # 全新输入（DeepSeek 通常为 0）
    "output": 1.10,
    "cache_read": 0.07,
    "cache_write": 0.27,
}

# 覆盖示例：EMBERPY_COST_INPUT_PER_MT=0.3 EMBERPY_COST_OUTPUT_PER_MT=1.2 ...
_ENV_KEY = {
    "input": "EMBERPY_COST_INPUT_PER_MT",
    "output": "EMBERPY_COST_OUTPUT_PER_MT",
    "cache_read": "EMBERPY_COST_CACHE_READ_PER_MT",
    "cache_write": "EMBERPY_COST_CACHE_WRITE_PER_MT",
}


def _prices() -> dict[str, float]:
    out = dict(_DEFAULT_PRICES)
    for key, env in _ENV_KEY.items():
        raw = os.environ.get(env)
        if raw is None:
            continue
        try:
            out[key] = float(raw)
        except ValueError:
            pass  # 非法值忽略，保留默认
    return out


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


class UsageTracker:
    """会话级 usage 累加器：喂每次 AssistantReply.usage，取面板所需统计。"""

    __slots__ = (
        "input",
        "output",
        "cache_read",
        "cache_write",
        "_last_prompt_tokens",
        "cost",
        "_prices",
    )

    def __init__(self) -> None:
        self.input = 0
        self.output = 0
        self.cache_read = 0
        self.cache_write = 0
        self._last_prompt_tokens: Optional[float] = None  # 最近一次请求的完整 prompt
        self.cost = 0.0
        self._prices = _prices()

    # -- 喂入 --------------------------------------------------------------
    def track(self, usage: Optional[dict[str, Any]]) -> None:
        """把一次模型响应的原始 usage 归一化并累计。usage 为 None 时忽略。"""
        if not isinstance(usage, dict):
            return
        prompt = _num(usage.get("prompt_tokens"))          # 本次完整输入
        output = _num(usage.get("completion_tokens"))
        cache_read = _num(usage.get("prompt_cache_hit_tokens"))
        cache_write = _num(usage.get("prompt_cache_miss_tokens"))
        # 无缓存记账的网关：整个 prompt 都是"新鲜 input"；DeepSeek hit+miss 近似等于
        # prompt，input 分量趋近 0（与 Anthropic 的 input_tokens 语义一致）。
        fresh = max(0.0, prompt - cache_read - cache_write)

        p = self._prices
        self.cost += (
            fresh / 1_000_000 * p["input"]
            + output / 1_000_000 * p["output"]
            + cache_read / 1_000_000 * p["cache_read"]
            + cache_write / 1_000_000 * p["cache_write"]
        )
        self.input += fresh
        self.output += output
        self.cache_read += cache_read
        self.cache_write += cache_write
        if prompt:
            self._last_prompt_tokens = prompt

    def merge(self, other: "UsageTracker") -> None:
        """把另一个 tracker（子 agent）的统计并入自己。"""
        if not isinstance(other, UsageTracker):
            return
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.cost += other.cost
        if other._last_prompt_tokens:
            self._last_prompt_tokens = other._last_prompt_tokens

    # -- 取数（喂 Ember AgentSessionStats） --------------------------------
    @property
    def any_usage(self) -> bool:
        """是否已累计到任何真实 usage（区分"没跑过"与"全 0"）。"""
        return self._last_prompt_tokens is not None

    def tokens_dict(self) -> dict[str, int]:
        """面板 tokens 字段。total 含 cache 分量（对齐 Anthropic totalTokens）。"""
        total = self.input + self.cache_read + self.cache_write + self.output
        return {
            "input": round(self.input),
            "output": round(self.output),
            "cacheRead": round(self.cache_read),
            "cacheWrite": round(self.cache_write),
            "total": round(total),
        }

    def context_usage(self, context_window: int) -> Optional[dict[str, Any]]:
        """上下文占用圆环数据。没跑过模型返回 None（UI 走 fallback）。"""
        if self._last_prompt_tokens is None or not context_window:
            return None
        tokens = round(self._last_prompt_tokens)
        percent = min(100.0, tokens / context_window * 100.0)
        return {
            "tokens": tokens,
            "contextWindow": context_window,
            "percent": round(percent, 1),
        }
