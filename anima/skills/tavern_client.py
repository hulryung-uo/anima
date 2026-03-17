"""UO Tavern REST API forum client — posts directly to uotavern.com."""

from __future__ import annotations

import time

import aiohttp
import structlog

from anima.skills.forum import ForumClient, ForumPost, ForumReply

logger = structlog.get_logger()


class TavernForumClient(ForumClient):
    """Forum client that talks to the UO Tavern Next.js API."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
        }

    async def read_posts(self, category: str, limit: int = 10) -> list[ForumPost]:
        board = self._category_to_board(category)
        url = f"{self._base_url}/posts?board={board}&limit={limit}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("tavern_read_failed", status=resp.status)
                    return []
                data = await resp.json()

        posts = []
        for p in data.get("posts", []):
            agent = p.get("agent") or {}
            posts.append(ForumPost(
                post_id=p["id"],
                title=p.get("title") or "",
                body=p.get("content", ""),
                author=agent.get("name", "unknown"),
                category=p.get("board", category),
                timestamp=_iso_to_ts(p.get("created_at", "")),
            ))
        return posts

    async def read_post(self, post_id: str) -> ForumPost | None:
        url = f"{self._base_url}/posts/{post_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                p = await resp.json()

        agent = p.get("agent") or {}
        replies = []
        for c in p.get("comments", []):
            c_agent = c.get("agent") or {}
            replies.append(ForumReply(
                reply_id=c["id"],
                body=c.get("content", ""),
                author=c_agent.get("name", "unknown"),
                timestamp=_iso_to_ts(c.get("created_at", "")),
            ))

        return ForumPost(
            post_id=p["id"],
            title=p.get("title") or "",
            body=p.get("content", ""),
            author=agent.get("name", "unknown"),
            category=p.get("board", "general"),
            timestamp=_iso_to_ts(p.get("created_at", "")),
            replies=replies,
        )

    async def create_post(self, title: str, body: str, category: str) -> str:
        board = self._category_to_board(category)
        url = f"{self._base_url}/agent/posts"
        payload: dict = {
            "board": board,
            "title": title,
            "content": body,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=self._headers()) as resp:
                if resp.status != 201:
                    text = await resp.text()
                    logger.error("tavern_post_failed", status=resp.status, body=text)
                    return ""
                data = await resp.json()
                post_id = data.get("id", "")
                logger.info("tavern_posted", post_id=post_id, title=title, board=board)
                return post_id

    async def reply_to_post(self, post_id: str, body: str) -> str:
        url = f"{self._base_url}/agent/posts/{post_id}/comments"
        payload = {"content": body}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=self._headers()) as resp:
                if resp.status != 201:
                    text = await resp.text()
                    logger.error("tavern_reply_failed", status=resp.status, body=text)
                    return ""
                data = await resp.json()
                reply_id = data.get("id", "")
                logger.info("tavern_replied", reply_id=reply_id, post_id=post_id)
                return reply_id

    async def search(self, query: str) -> list[ForumPost]:
        # UO Tavern doesn't have a search endpoint yet — read recent and filter
        posts = await self.read_posts("general", limit=50)
        query_lower = query.lower()
        return [
            p for p in posts
            if query_lower in p.title.lower() or query_lower in p.body.lower()
        ]

    async def send_experience(
        self,
        exp_type: str,
        summary: str,
        location: str | None = None,
        items_gained: list[dict] | None = None,
        items_lost: list[dict] | None = None,
        gold_delta: int = 0,
        notable: bool = False,
    ) -> str | None:
        """Send a game experience log to the Tavern (not part of abstract ForumClient)."""
        url = f"{self._base_url}/agent/experience"
        payload: dict = {
            "type": exp_type,
            "summary": summary,
            "gold_delta": gold_delta,
            "notable": notable,
        }
        if location:
            payload["location"] = location
        if items_gained:
            payload["items_gained"] = items_gained
        if items_lost:
            payload["items_lost"] = items_lost

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=self._headers()) as resp:
                if resp.status != 201:
                    logger.warning("tavern_experience_failed", status=resp.status)
                    return None
                data = await resp.json()
                return data.get("id")

    @staticmethod
    def _category_to_board(category: str) -> str:
        mapping = {
            "adventures": "general",
            "general": "general",
            "trade": "trade",
            "trading": "trade",
            "questions": "qa",
            "qa": "qa",
            "tavern": "tavern",
            "roleplay": "tavern",
        }
        return mapping.get(category.lower(), "general")


def _iso_to_ts(iso: str) -> float:
    if not iso:
        return time.time()
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return time.time()
