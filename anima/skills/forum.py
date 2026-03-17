"""Forum client — abstract interface + mock implementation for uotavern.com."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ForumPost:
    post_id: str
    title: str
    body: str
    author: str
    category: str
    timestamp: float
    replies: list[ForumReply] = field(default_factory=list)


@dataclass
class ForumReply:
    reply_id: str
    body: str
    author: str
    timestamp: float


class ForumClient(ABC):
    """Abstract forum interface. Implementations can be REST API, mock, etc."""

    @abstractmethod
    async def read_posts(self, category: str, limit: int = 10) -> list[ForumPost]:
        ...

    @abstractmethod
    async def read_post(self, post_id: str) -> ForumPost | None:
        ...

    @abstractmethod
    async def create_post(self, title: str, body: str, category: str) -> str:
        """Create a post. Returns the post ID."""
        ...

    @abstractmethod
    async def reply_to_post(self, post_id: str, body: str) -> str:
        """Reply to a post. Returns the reply ID."""
        ...

    @abstractmethod
    async def search(self, query: str) -> list[ForumPost]:
        ...


class MockForumClient(ForumClient):
    """In-memory mock forum for testing."""

    def __init__(self) -> None:
        self._posts: dict[str, ForumPost] = {}
        self._next_id = 1

    async def read_posts(self, category: str, limit: int = 10) -> list[ForumPost]:
        posts = [p for p in self._posts.values() if p.category == category]
        posts.sort(key=lambda p: p.timestamp, reverse=True)
        return posts[:limit]

    async def read_post(self, post_id: str) -> ForumPost | None:
        return self._posts.get(post_id)

    async def create_post(self, title: str, body: str, category: str) -> str:
        post_id = f"post_{self._next_id}"
        self._next_id += 1
        self._posts[post_id] = ForumPost(
            post_id=post_id,
            title=title,
            body=body,
            author="Anima",
            category=category,
            timestamp=time.time(),
        )
        return post_id

    async def reply_to_post(self, post_id: str, body: str) -> str:
        post = self._posts.get(post_id)
        if post is None:
            raise ValueError(f"Post {post_id} not found")
        reply_id = f"reply_{self._next_id}"
        self._next_id += 1
        post.replies.append(
            ForumReply(
                reply_id=reply_id,
                body=body,
                author="Anima",
                timestamp=time.time(),
            )
        )
        return reply_id

    async def search(self, query: str) -> list[ForumPost]:
        query_lower = query.lower()
        return [
            p
            for p in self._posts.values()
            if query_lower in p.title.lower() or query_lower in p.body.lower()
        ]
