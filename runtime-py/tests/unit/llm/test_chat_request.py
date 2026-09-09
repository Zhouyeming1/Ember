"""ChatLLM 请求差异（reasoner vs 聊天模型）离线测试：不真联网。

用假 httpx.Client 抓请求体，验证：
- reasoner：请求不带 temperature（DeepSeek reasoner 不支持采样参数），
  且 reasoning_content 解析成 AssistantReply.thinking；
- 聊天模型：正常带 temperature，thinking 为 None。
"""
from __future__ import annotations

from emberpy.llm.chat import ChatLLM


class _FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    @property
    def status_code(self) -> int:
        return 200

    def json(self) -> dict:
        return self._data


class _FakeClient:
    """httpx.Client 替身：记下请求体，返回固定 payload。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.body: dict | None = None

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def post(self, url: str, headers: dict | None = None, json: dict | None = None):
        self.body = json
        return _FakeResponse(self._payload)


def _payload(reasoning: str | None = None) -> dict:
    message: dict = {"role": "assistant", "content": "答案"}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {"choices": [{"message": message}], "usage": {"total_tokens": 3}}


def test_reasoner_omits_temperature_and_reads_reasoning(monkeypatch) -> None:
    fake = _FakeClient(_payload(reasoning="先想一下"))
    monkeypatch.setattr("httpx.Client", lambda **kw: fake)
    llm = ChatLLM(api_key="test-key", model="deepseek-reasoner")
    reply = llm.complete([{"role": "user", "content": "hi"}])

    assert fake.body is not None
    assert fake.body["model"] == "deepseek-reasoner"
    assert "temperature" not in fake.body  # reasoner 不带采样参数
    assert reply.thinking == "先想一下"
    assert reply.content == "答案"
    assert reply.raw_message["reasoning_content"] == "先想一下"


def test_chat_model_sends_temperature_no_reasoning(monkeypatch) -> None:
    fake = _FakeClient(_payload())
    monkeypatch.setattr("httpx.Client", lambda **kw: fake)
    llm = ChatLLM(api_key="test-key", model="deepseek-chat", temperature=0.2)
    reply = llm.complete([{"role": "user", "content": "hi"}])

    assert fake.body is not None
    assert fake.body["model"] == "deepseek-chat"
    assert fake.body["temperature"] == 0.2
    assert reply.thinking is None
    assert "reasoning_content" not in reply.raw_message


def test_blank_reasoning_becomes_none(monkeypatch) -> None:
    fake = _FakeClient(_payload(reasoning="   "))
    monkeypatch.setattr("httpx.Client", lambda **kw: fake)
    llm = ChatLLM(api_key="test-key", model="deepseek-reasoner")
    reply = llm.complete([{"role": "user", "content": "hi"}])
    assert reply.thinking is None
