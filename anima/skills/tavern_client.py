"""UO Tavern REST API forum client — posts directly to uotavern.com."""

from __future__ import annotations

import asyncio
import time

import aiohttp
import structlog

from anima.skills.forum import ForumClient, ForumPost, ForumReply

logger = structlog.get_logger()

# Bound every request so a hung/unreachable board can't wedge the brain loop.
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10.0)


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

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int | None, object]:
        """Perform one HTTP request, degrading gracefully on network failure.

        Returns ``(status, payload)`` where ``status`` is the HTTP status code
        and ``payload`` is the decoded JSON (or the raw text body on a decode
        failure). On a transport-level error (board unreachable, DNS failure,
        timeout) ``status`` is ``None`` so callers fall through to their normal
        non-2xx sentinels instead of letting the exception crash the caller.
        """
        try:
            async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
                async with session.request(method, url, json=json, headers=headers) as resp:
                    try:
                        payload: object = await resp.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        payload = await resp.text()
                    return resp.status, payload
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("tavern_unreachable", method=method, url=url, error=str(exc))
            return None, None

    async def read_posts(self, category: str, limit: int = 10) -> list[ForumPost]:
        board = self._category_to_board(category)
        url = f"{self._base_url}/posts?board={board}&limit={limit}"
        status, data = await self._request("GET", url)
        if status != 200 or not isinstance(data, dict):
            logger.warning("tavern_read_failed", status=status)
            return []

        posts = []
        raw_posts = data.get("posts")
        if not isinstance(raw_posts, list):
            raw_posts = []
        for p in raw_posts:
            # Each list element is untrusted external JSON: a real board can
            # return a non-dict element (null / string), or a post object with
            # no "id". Indexing ``p["id"]`` then raised KeyError/TypeError and —
            # because this read path runs in the behavior tree's Forum step,
            # OUTSIDE the packet-dispatch try/except — propagated up and crashed
            # the brain tick. Skip an element we can't key (its id is the
            # identity used to reply), mirroring the write paths' ``.get("id")``.
            if not isinstance(p, dict):
                continue
            post_id = p.get("id")
            if not post_id:
                continue
            agent = p.get("agent") if isinstance(p.get("agent"), dict) else {}
            posts.append(ForumPost(
                post_id=post_id,
                title=p.get("title") or "",
                body=p.get("content", ""),
                author=agent.get("name", "unknown"),
                category=p.get("board", category),
                timestamp=_iso_to_ts(p.get("created_at", "")),
            ))
        return posts

    async def read_post(self, post_id: str) -> ForumPost | None:
        url = f"{self._base_url}/posts/{post_id}"
        status, p = await self._request("GET", url)
        if status != 200 or not isinstance(p, dict):
            return None

        # A 200 body with no "id" is a malformed/unparseable post — treat it as a
        # parse failure (the post_id is the post's identity), the same way a
        # non-dict body is rejected above. Without this, ``p["id"]`` below raised
        # KeyError outside the packet try/except and crashed the Forum tick.
        resolved_id = p.get("id")
        if not resolved_id:
            return None
        agent = p.get("agent") if isinstance(p.get("agent"), dict) else {}
        replies = []
        raw_comments = p.get("comments")
        if not isinstance(raw_comments, list):
            raw_comments = []
        for c in raw_comments:
            if not isinstance(c, dict):
                continue
            reply_id = c.get("id")
            if not reply_id:
                continue
            c_agent = c.get("agent") if isinstance(c.get("agent"), dict) else {}
            replies.append(ForumReply(
                reply_id=reply_id,
                body=c.get("content", ""),
                author=c_agent.get("name", "unknown"),
                timestamp=_iso_to_ts(c.get("created_at", "")),
            ))

        return ForumPost(
            post_id=resolved_id,
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

        status, data = await self._request("POST", url, json=payload, headers=self._headers())
        if status != 201 or not isinstance(data, dict):
            logger.error("tavern_post_failed", status=status, body=data)
            return ""
        post_id = data.get("id", "")
        logger.info("tavern_posted", post_id=post_id, title=title, board=board)
        return post_id

    async def reply_to_post(self, post_id: str, body: str) -> str:
        url = f"{self._base_url}/agent/posts/{post_id}/comments"
        payload = {"content": body}

        status, data = await self._request("POST", url, json=payload, headers=self._headers())
        if status != 201 or not isinstance(data, dict):
            logger.error("tavern_reply_failed", status=status, body=data)
            return ""
        reply_id = data.get("id", "")
        logger.info("tavern_replied", reply_id=reply_id, post_id=post_id)
        return reply_id

    async def search(self, query: str) -> list[ForumPost]:
        """Search across library and tavern boards for matching posts."""
        results: list[ForumPost] = []
        query_lower = query.lower()
        # Search library first (knowledge base), then tavern
        for board in ("library", "tavern"):
            posts = await self.read_posts(board, limit=20)
            for p in posts:
                if (query_lower in p.title.lower()
                        or query_lower in p.body.lower()):
                    results.append(p)
        return results

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

        status, data = await self._request("POST", url, json=payload, headers=self._headers())
        if status != 201 or not isinstance(data, dict):
            logger.warning("tavern_experience_failed", status=status)
            return None
        return data.get("id")

    @staticmethod
    def _category_to_board(category: str) -> str:
        # Valid boards: tavern, trade, qa, library
        mapping = {
            "adventures": "tavern",
            "general": "tavern",
            "tavern": "tavern",
            "roleplay": "tavern",
            "trade": "trade",
            "trading": "trade",
            "questions": "qa",
            "qa": "qa",
            "library": "library",
            "guide": "library",
        }
        return mapping.get(category.lower(), "tavern")


def _iso_to_ts(iso: str) -> float:
    if not iso:
        return time.time()
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return time.time()
