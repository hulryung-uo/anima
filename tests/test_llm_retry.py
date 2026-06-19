"""Robustness: LLMClient.chat retries transient backend errors with backoff,
fails fast on non-transient errors, and never raises on a flaky transport.

The HTTP transport (litellm.acompletion) is fully mocked — no real endpoint is
ever contacted.
"""

from __future__ import annotations

import asyncio

import litellm
import pytest

from anima.brain.llm import LLMClient


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Usage:
    prompt_tokens = 3
    completion_tokens = 5


class _Resp:
    def __init__(self, content: str = "ok") -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage()
        self.model = "mock-model"


def _transient(cls: type[Exception]) -> Exception:
    return cls(message="boom", model="m", llm_provider="ollama")


@pytest.mark.asyncio
async def test_transient_error_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    async def fake_acompletion(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _transient(litellm.APIConnectionError)
        return _Resp("hello")

    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    client = LLMClient(provider="ollama", max_retries=2, retry_backoff=0.5)
    resp = await client.chat([{"role": "user", "content": "hi"}])

    assert resp.text == "hello"
    assert calls["n"] == 3  # 2 failures + 1 success
    assert sleeps == [0.5, 1.0]  # exponential backoff between attempts


@pytest.mark.asyncio
async def test_transient_error_exhausts_and_returns_empty(monkeypatch):
    calls = {"n": 0}

    async def fake_acompletion(**kwargs):
        calls["n"] += 1
        raise _transient(litellm.Timeout)

    async def fake_sleep(delay):
        return None

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    client = LLMClient(provider="ollama", max_retries=2)
    resp = await client.chat([{"role": "user", "content": "hi"}])

    assert resp.text == ""  # graceful empty, never raises
    assert calls["n"] == 3  # initial attempt + 2 retries


@pytest.mark.asyncio
async def test_non_transient_error_fails_fast(monkeypatch):
    calls = {"n": 0}

    async def fake_acompletion(**kwargs):
        calls["n"] += 1
        raise litellm.BadRequestError(
            message="malformed", model="m", llm_provider="ollama"
        )

    async def fake_sleep(delay):  # pragma: no cover - must not be reached
        raise AssertionError("must not back off on a non-transient error")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    client = LLMClient(provider="ollama", max_retries=5)
    resp = await client.chat([{"role": "user", "content": "hi"}])

    assert resp.text == ""
    assert calls["n"] == 1  # no retries for a non-transient error
