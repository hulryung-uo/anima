"""Tests for the UO Tavern REST forum client — robustness / graceful degradation.

The network is fully mocked; no real endpoint is ever contacted. We monkeypatch
``_request`` (the single transport seam) to simulate success, non-2xx, and
transport-level failure (board unreachable / timeout).
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from anima.skills.tavern_client import TavernForumClient


@pytest.fixture
def client() -> TavernForumClient:
    return TavernForumClient("https://tavern.example/api", "secret-key")


def _stub_request(result):
    """Build an async stand-in for ``_request`` that always returns ``result``."""

    async def _request(self, method, url, *, json=None, headers=None):  # noqa: A002
        return result

    return _request


def _raising_request(exc: Exception):
    """Build an async ``_request`` that mimics a transport failure.

    The real ``_request`` swallows ``aiohttp.ClientError`` / ``TimeoutError`` and
    returns ``(None, None)``; this stub reproduces that contract so callers are
    exercised exactly as they would be when the board is unreachable.
    """

    async def _request(self, method, url, *, json=None, headers=None):  # noqa: A002
        # Sanity: the exception type really is one the helper is meant to catch.
        assert isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError))
        return None, None

    return _request


class TestTavernUnreachable:
    """When the board is down, every call degrades to its empty sentinel."""

    @pytest.mark.asyncio
    async def test_read_posts_returns_empty(self, client, monkeypatch) -> None:
        monkeypatch.setattr(
            TavernForumClient,
            "_request",
            _raising_request(aiohttp.ClientConnectorError.__new__(aiohttp.ClientConnectorError)),
            raising=True,
        )
        # Force a clean ClientError instance without needing connector internals.
        monkeypatch.setattr(
            TavernForumClient,
            "_request",
            _raising_request(aiohttp.ClientError("connection refused")),
        )
        posts = await client.read_posts("tavern")
        assert posts == []

    @pytest.mark.asyncio
    async def test_read_post_returns_none(self, client, monkeypatch) -> None:
        monkeypatch.setattr(
            TavernForumClient, "_request", _raising_request(asyncio.TimeoutError())
        )
        assert await client.read_post("post_1") is None

    @pytest.mark.asyncio
    async def test_create_post_returns_empty_string(self, client, monkeypatch) -> None:
        monkeypatch.setattr(
            TavernForumClient, "_request", _raising_request(aiohttp.ClientError())
        )
        assert await client.create_post("t", "b", "tavern") == ""

    @pytest.mark.asyncio
    async def test_reply_returns_empty_string(self, client, monkeypatch) -> None:
        monkeypatch.setattr(
            TavernForumClient, "_request", _raising_request(aiohttp.ClientError())
        )
        assert await client.reply_to_post("post_1", "hi") == ""

    @pytest.mark.asyncio
    async def test_search_returns_empty(self, client, monkeypatch) -> None:
        monkeypatch.setattr(
            TavernForumClient, "_request", _raising_request(aiohttp.ClientError())
        )
        assert await client.search("anything") == []

    @pytest.mark.asyncio
    async def test_send_experience_returns_none(self, client, monkeypatch) -> None:
        monkeypatch.setattr(
            TavernForumClient, "_request", _raising_request(asyncio.TimeoutError())
        )
        assert await client.send_experience("kill", "slew a ratman") is None


class TestTavernNon2xx:
    """A non-2xx status (e.g. 500) is treated the same as unreachable."""

    @pytest.mark.asyncio
    async def test_read_posts_500(self, client, monkeypatch) -> None:
        monkeypatch.setattr(
            TavernForumClient, "_request", _stub_request((500, "boom"))
        )
        assert await client.read_posts("tavern") == []

    @pytest.mark.asyncio
    async def test_create_post_500(self, client, monkeypatch) -> None:
        monkeypatch.setattr(
            TavernForumClient, "_request", _stub_request((500, {"error": "x"}))
        )
        assert await client.create_post("t", "b", "tavern") == ""


class TestTavernHappyPath:
    """Successful responses are still parsed correctly through the helper."""

    @pytest.mark.asyncio
    async def test_read_posts_parses(self, client, monkeypatch) -> None:
        payload = {
            "posts": [
                {
                    "id": "p1",
                    "title": "Hello",
                    "content": "body",
                    "board": "tavern",
                    "agent": {"name": "Anima"},
                    "created_at": "2026-06-19T00:00:00Z",
                }
            ]
        }
        monkeypatch.setattr(
            TavernForumClient, "_request", _stub_request((200, payload))
        )
        posts = await client.read_posts("tavern")
        assert len(posts) == 1
        assert posts[0].post_id == "p1"
        assert posts[0].title == "Hello"
        assert posts[0].author == "Anima"

    @pytest.mark.asyncio
    async def test_create_post_returns_id(self, client, monkeypatch) -> None:
        monkeypatch.setattr(
            TavernForumClient, "_request", _stub_request((201, {"id": "new1"}))
        )
        assert await client.create_post("t", "b", "tavern") == "new1"


class TestTavernMalformedPayload:
    """A 200 response whose body is structurally malformed external JSON must
    degrade gracefully — NOT crash the (un-try/except'd) Forum brain tick."""

    @pytest.mark.asyncio
    async def test_read_posts_skips_non_dict_and_idless_elements(
        self, client, monkeypatch
    ) -> None:
        # A real board can return null/string elements and an id-less post mixed
        # in with a good one. The old ``p["id"]`` raised TypeError/KeyError here.
        payload = {
            "posts": [
                None,
                "oops-not-a-dict",
                {"title": "no id here", "content": "x"},  # missing "id"
                {
                    "id": "p1",
                    "title": "Hello",
                    "content": "body",
                    "agent": {"name": "Anima"},
                },
            ]
        }
        monkeypatch.setattr(
            TavernForumClient, "_request", _stub_request((200, payload))
        )
        posts = await client.read_posts("tavern")
        # Only the one well-formed post survives; no exception is raised.
        assert [p.post_id for p in posts] == ["p1"]
        assert posts[0].author == "Anima"

    @pytest.mark.asyncio
    async def test_read_posts_non_list_posts_field(self, client, monkeypatch) -> None:
        # "posts" present but not a list (e.g. an error object) must not crash.
        monkeypatch.setattr(
            TavernForumClient,
            "_request",
            _stub_request((200, {"posts": {"unexpected": "shape"}})),
        )
        assert await client.read_posts("tavern") == []

    @pytest.mark.asyncio
    async def test_read_post_missing_id_returns_none(self, client, monkeypatch) -> None:
        # A 200 post body with no "id" is unparseable — return None, don't crash.
        monkeypatch.setattr(
            TavernForumClient,
            "_request",
            _stub_request((200, {"title": "t", "content": "b"})),
        )
        assert await client.read_post("whatever") is None

    @pytest.mark.asyncio
    async def test_read_post_skips_malformed_comments(self, client, monkeypatch) -> None:
        payload = {
            "id": "p9",
            "title": "Top",
            "content": "body",
            "agent": {"name": "Anima"},
            "comments": [
                None,
                "nope",
                {"content": "no id"},  # missing "id"
                {"id": "c1", "content": "real reply", "agent": {"name": "Bob"}},
            ],
        }
        monkeypatch.setattr(
            TavernForumClient, "_request", _stub_request((200, payload))
        )
        post = await client.read_post("p9")
        assert post is not None
        assert post.post_id == "p9"
        # Only the well-formed comment survives; no KeyError on the others.
        assert [r.reply_id for r in post.replies] == ["c1"]
        assert post.replies[0].author == "Bob"
