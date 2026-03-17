"""Tests for the forum skill."""

from __future__ import annotations

import pytest

from anima.skills.forum import MockForumClient


@pytest.fixture
def forum():
    return MockForumClient()


class TestMockForumClient:
    @pytest.mark.asyncio
    async def test_create_and_read_post(self, forum: MockForumClient) -> None:
        post_id = await forum.create_post(
            title="My First Adventure",
            body="I visited the tavern today!",
            category="adventures",
        )
        assert post_id == "post_1"

        post = await forum.read_post(post_id)
        assert post is not None
        assert post.title == "My First Adventure"
        assert post.body == "I visited the tavern today!"
        assert post.category == "adventures"
        assert post.author == "Anima"

    @pytest.mark.asyncio
    async def test_read_posts_by_category(self, forum: MockForumClient) -> None:
        await forum.create_post("Post 1", "Body 1", "adventures")
        await forum.create_post("Post 2", "Body 2", "trading")
        await forum.create_post("Post 3", "Body 3", "adventures")

        adventures = await forum.read_posts("adventures")
        assert len(adventures) == 2
        assert all(p.category == "adventures" for p in adventures)

        trading = await forum.read_posts("trading")
        assert len(trading) == 1

    @pytest.mark.asyncio
    async def test_read_posts_limit(self, forum: MockForumClient) -> None:
        for i in range(5):
            await forum.create_post(f"Post {i}", f"Body {i}", "adventures")

        posts = await forum.read_posts("adventures", limit=3)
        assert len(posts) == 3

    @pytest.mark.asyncio
    async def test_reply_to_post(self, forum: MockForumClient) -> None:
        post_id = await forum.create_post("Question", "Help?", "general")
        reply_id = await forum.reply_to_post(post_id, "I can help!")

        assert reply_id.startswith("reply_")

        post = await forum.read_post(post_id)
        assert post is not None
        assert len(post.replies) == 1
        assert post.replies[0].body == "I can help!"

    @pytest.mark.asyncio
    async def test_reply_to_nonexistent_post(self, forum: MockForumClient) -> None:
        with pytest.raises(ValueError, match="not found"):
            await forum.reply_to_post("nonexistent", "Hello")

    @pytest.mark.asyncio
    async def test_search(self, forum: MockForumClient) -> None:
        await forum.create_post("Tavern Guide", "The tavern is great", "adventures")
        await forum.create_post("Bank Location", "Bank is near center", "guides")
        await forum.create_post("Trading Tips", "Sell at tavern", "trading")

        results = await forum.search("tavern")
        assert len(results) == 2
        titles = {p.title for p in results}
        assert "Tavern Guide" in titles
        assert "Trading Tips" in titles

    @pytest.mark.asyncio
    async def test_search_case_insensitive(self, forum: MockForumClient) -> None:
        await forum.create_post("TAVERN", "body", "adventures")

        results = await forum.search("tavern")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_read_nonexistent_post(self, forum: MockForumClient) -> None:
        post = await forum.read_post("nonexistent")
        assert post is None

    @pytest.mark.asyncio
    async def test_empty_category(self, forum: MockForumClient) -> None:
        posts = await forum.read_posts("empty_category")
        assert posts == []
