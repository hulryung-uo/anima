"""Robustness: LLMClient.chat must not raise when a returned choice carries a
``message`` of ``None`` (or a message lacking a ``content`` attribute).

Some providers/proxies emit a well-formed ``choices`` list whose first choice
has ``message=None`` (content-filtered, tool-call-only, or coalesced-streaming
payloads). The extraction code reached through ``choice.message.content``
directly, which raises AttributeError *after* the retry try/except — the error
propagates up and kills the whole think tick, defeating the module's documented
"a malformed/empty payload must not raise" contract. This test pins the
graceful behaviour: such a payload yields empty text, not an exception.

The HTTP transport (litellm.acompletion) is fully mocked — no real endpoint is
ever contacted.
"""

from __future__ import annotations

import litellm
import pytest

from anima.brain.llm import LLMClient


class _ChoiceNoneMessage:
    """A choice whose ``message`` is None (content-filtered / tool-only)."""

    def __init__(self) -> None:
        self.message = None


class _RespMessageNone:
    def __init__(self) -> None:
        self.choices = [_ChoiceNoneMessage()]
        self.model = "mock-model"
        # no usage — mirrors a stripped payload


@pytest.mark.asyncio
async def test_message_none_does_not_raise(monkeypatch):
    async def fake_acompletion(**kwargs):
        return _RespMessageNone()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    client = LLMClient(provider="ollama")
    # Before the fix this raised AttributeError: 'NoneType' object has no
    # attribute 'content'.
    resp = await client.chat([{"role": "user", "content": "hi"}])

    assert resp.text == ""
    assert resp.thinking == ""
    assert resp.model == "mock-model"
    assert resp.prompt_tokens == 0
    assert resp.eval_tokens == 0


class _MessageNoContent:
    """A message object that simply has no ``content`` attribute at all."""

    # deliberately empty — getattr(message, "content", None) must yield None
    pass


class _ChoiceNoContent:
    def __init__(self) -> None:
        self.message = _MessageNoContent()


class _RespNoContent:
    def __init__(self) -> None:
        self.choices = [_ChoiceNoContent()]
        self.model = "mock-model"


@pytest.mark.asyncio
async def test_message_without_content_attr_does_not_raise(monkeypatch):
    async def fake_acompletion(**kwargs):
        return _RespNoContent()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    client = LLMClient(provider="ollama")
    resp = await client.chat([{"role": "user", "content": "hi"}])

    assert resp.text == ""
    assert resp.thinking == ""
    assert resp.model == "mock-model"


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _RespOk:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]
        self.model = "mock-model"


@pytest.mark.asyncio
async def test_normal_content_still_extracted(monkeypatch):
    """The defensive getattr path must not regress the happy path."""

    async def fake_acompletion(**kwargs):
        return _RespOk("  hello world  ")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    client = LLMClient(provider="ollama")
    resp = await client.chat([{"role": "user", "content": "hi"}])

    assert resp.text == "hello world"
