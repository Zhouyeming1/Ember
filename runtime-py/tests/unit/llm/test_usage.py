"""UsageTracker 单测：DeepSeek/OpenAI 兼容 usage → Anthropic 语义累加 + cost + context。

口径对齐 claude cost-tracker：input 不含 cache、cache read/write 单列、context
占用 = input + cache_read + cache_write（不含 output）、cost = Σ token/1e6 × 每 M 单价。
"""
from __future__ import annotations

import pytest

from emberpy.llm.usage import UsageTracker

DEEPSEEK = {
    "prompt_tokens": 1000,
    "completion_tokens": 50,
    "prompt_cache_hit_tokens": 800,
    "prompt_cache_miss_tokens": 200,
}
# 默认单价 USD /1M：input 0.27 output 1.10 cache_read 0.07 cache_write 0.27
EXPECT_COST = 800 / 1e6 * 0.07 + 200 / 1e6 * 0.27 + 50 / 1e6 * 1.10


def test_deepseek_normalization_and_tokens() -> None:
    t = UsageTracker()
    t.track(DEEPSEEK)
    # DeepSeek 把整个 prompt 计入 hit/miss -> 无"新鲜且不缓存"的 input
    assert t.input == 0
    assert t.output == 50
    assert t.cache_read == 800
    assert t.cache_write == 200
    assert t.tokens_dict() == {
        "input": 0,
        "output": 50,
        "cacheRead": 800,
        "cacheWrite": 200,
        "total": 1050,
    }
    assert t.cost == pytest.approx(EXPECT_COST)
    assert t.any_usage


def test_openai_no_cache_fields() -> None:
    t = UsageTracker()
    t.track({"prompt_tokens": 100, "completion_tokens": 20})
    assert t.input == 100
    assert t.output == 20
    assert t.cache_read == 0
    assert t.cache_write == 0
    assert t.tokens_dict()["total"] == 120


def test_accumulates_across_calls() -> None:
    t = UsageTracker()
    t.track(DEEPSEEK)
    t.track(DEEPSEEK)
    assert t.input == 0 and t.output == 100
    assert t.cache_read == 1600 and t.cache_write == 400
    assert t.tokens_dict()["total"] == 2100
    assert t.cost == pytest.approx(2 * EXPECT_COST)


def test_track_none_and_garbage_ignored() -> None:
    t = UsageTracker()
    t.track(None)
    t.track("usage")
    t.track({})
    assert not t.any_usage
    assert t.tokens_dict() == {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}
    assert t.cost == 0


def test_context_usage() -> None:
    t = UsageTracker()
    assert t.context_usage(64000) is None  # 没跑过模型
    t.track({"prompt_tokens": 6400, "completion_tokens": 10})
    cu = t.context_usage(64000)
    assert cu is not None
    assert cu["tokens"] == 6400
    assert cu["contextWindow"] == 64000
    assert cu["percent"] == 10.0


def test_context_percent_saturated() -> None:
    t = UsageTracker()
    t.track({"prompt_tokens": 200_000, "completion_tokens": 1})
    cu = t.context_usage(64_000)
    assert cu is not None and cu["percent"] == 100.0


def test_context_usage_uses_last_prompt() -> None:
    t = UsageTracker()
    t.track({"prompt_tokens": 1000, "completion_tokens": 5})
    t.track({"prompt_tokens": 3000, "completion_tokens": 5})
    cu = t.context_usage(64_000)
    assert cu is not None and cu["tokens"] == 3000  # 上下文占用取最近一次


def test_merge_subagent_usage() -> None:
    parent, child = UsageTracker(), UsageTracker()
    parent.track(DEEPSEEK)
    child.track({"prompt_tokens": 500, "completion_tokens": 30})  # 无 cache 网关
    parent.merge(child)
    assert parent.input == 500
    assert parent.output == 80
    assert parent.cache_read == 800
    assert parent.cost == pytest.approx(EXPECT_COST + 500 / 1e6 * 0.27 + 30 / 1e6 * 1.10)


def test_cost_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBERPY_COST_OUTPUT_PER_MT", "2")
    monkeypatch.setenv("EMBERPY_COST_CACHE_READ_PER_MT", "abc")  # 非法值忽略
    t = UsageTracker()
    t.track(DEEPSEEK)
    assert t.cost == pytest.approx(800 / 1e6 * 0.07 + 200 / 1e6 * 0.27 + 50 / 1e6 * 2)
