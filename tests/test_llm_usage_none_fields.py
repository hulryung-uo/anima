"""Robustness: LLMClient.chat must keep token counts as ``int`` even when the
backend returns a ``usage`` object whose fields are ``None``.

litellm coerces ``None`` to 0 in its strictly-typed ``Usage`` model, but raw
Ollama / OpenAI-compatible-proxy / coalesced-streaming payloads surface an
untyped usage object (duck-typed, e.g. ``SimpleNamespace``) whose
``prompt_tokens`` / ``completion_tokens`` can literally be ``None``. The
extraction code used ``usage.prompt_tokens if usage else 0``, which passes that
``None`` straight into ``LLMResponse`` — whose fields are declared ``int`` — and
leaks it into the debug log and any downstream token arithmetic (sums, budget
caps). This test pins the int contract.

The HTTP transport (litellm.acompletion) is fully mocked — no real endpoint is
ever contacted.
"""

from __future__ import annotations

from types import SimpleNamespace

import litellm
import pytest

from anima.brain.llm import LLMClient


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _RespNoneUsageFields:
    """A response whose ``usage`` is present but carries ``None`` token fields,
    mirroring an untyped proxy / Ollama streaming-coalesced payload."""

    def __init__(self, content: str = "ok") -> None:
        self.choices = [_Choice(content)]
        self.model = "mock-model"
        self.usage = SimpleNamespace(prompt_tokens=None, completion_tokens=None)


@pytest.mark.asyncio
async def test_none_usage_fields_degrade_to_int_zero(monkeypatch):
    async def fake_acompletion(**kwargs):
        return _RespNoneUsageFields("hello")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    client = LLMClient(provider="ollama")
    resp = await client.chat([{"role": "user", "content": "hi"}])

    assert resp.text == "hello"
    # Token counts must stay int — never None — so downstream arithmetic is safe.
    assert resp.prompt_tokens == 0
    assert resp.eval_tokens == 0
    assert isinstance(resp.prompt_tokens, int)
    assert isinstance(resp.eval_tokens, int)
    # The canonical failure mode: summing into a running token budget.
    assert resp.prompt_tokens + resp.eval_tokens == 0


class _Usage:
    prompt_tokens = 7
    completion_tokens = 11


class _RespRealUsage:
    def __init__(self, content: str = "ok") -> None:
        self.choices = [_Choice(content)]
        self.model = "mock-model"
        self.usage = _Usage()


@pytest.mark.asyncio
async def test_real_usage_values_still_pass_through(monkeypatch):
    """The None-coercion must not clobber genuine non-zero counts."""

    async def fake_acompletion(**kwargs):
        return _RespRealUsage("hi there")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    client = LLMClient(provider="ollama")
    resp = await client.chat([{"role": "user", "content": "hi"}])

    assert resp.prompt_tokens == 7
    assert resp.eval_tokens == 11
