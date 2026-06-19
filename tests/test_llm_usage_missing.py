"""Robustness: LLMClient.chat must not raise when the backend response omits a
``usage`` field.

litellm's ModelResponse does not guarantee a ``usage`` attribute — several real
providers (Ollama, some OpenAI-compatible proxies, coalesced streaming) return a
payload without it. The extraction code used ``response.usage`` directly, which
raises AttributeError *after* the retry try/except, propagating up and killing
the whole think tick. This test pins the graceful behaviour.

The HTTP transport (litellm.acompletion) is fully mocked — no real endpoint is
ever contacted.
"""

from __future__ import annotations

import litellm
import pytest

from anima.brain.llm import LLMClient


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _RespNoUsage:
    """Mirrors a litellm ModelResponse that carries no ``usage`` attribute."""

    def __init__(self, content: str = "ok") -> None:
        self.choices = [_Choice(content)]
        self.model = "mock-model"
        # NOTE: intentionally no ``usage`` attribute at all.


@pytest.mark.asyncio
async def test_missing_usage_does_not_raise(monkeypatch):
    async def fake_acompletion(**kwargs):
        return _RespNoUsage("hello")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    client = LLMClient(provider="ollama")
    # Before the fix this raised AttributeError: 'ModelResponse' object has no
    # attribute 'usage'.
    resp = await client.chat([{"role": "user", "content": "hi"}])

    assert resp.text == "hello"
    assert resp.model == "mock-model"
    # Token counts degrade to zero rather than crashing.
    assert resp.prompt_tokens == 0
    assert resp.eval_tokens == 0


class _Usage:
    prompt_tokens = 7
    completion_tokens = 11


class _RespWithUsage:
    def __init__(self, content: str = "ok") -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage()
        self.model = "mock-model"


@pytest.mark.asyncio
async def test_present_usage_is_still_reported(monkeypatch):
    """The guard must not regress the normal path where usage IS present."""

    async def fake_acompletion(**kwargs):
        return _RespWithUsage("hi there")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    client = LLMClient(provider="ollama")
    resp = await client.chat([{"role": "user", "content": "hi"}])

    assert resp.text == "hi there"
    assert resp.prompt_tokens == 7
    assert resp.eval_tokens == 11
