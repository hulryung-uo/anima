"""ForumPost cooldown integrity on the degraded-network path.

A transient transport failure makes TavernForumClient.create_post return an
empty post id. The periodic-essay skill must NOT arm its (default 1h)
post_interval cooldown on such a failure, otherwise one dropped request
silently suppresses in-world posting for a full interval. A successful post
(non-empty id) must arm the cooldown as before.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.skills.forum import MockForumClient
from anima.skills.forum_skill import ForumPost


class _FailingForumClient(MockForumClient):
    """Mimics the real client's degraded-network behavior: create returns ""."""

    async def create_post(self, title: str, body: str, category: str) -> str:
        return ""


class _FakeLLM:
    async def chat(self, messages):
        return SimpleNamespace(text="TITLE: A Day\nBODY:\nI mined some ore.")


class _FakeMemory:
    async def query_episodes(self, agent, limit=15):
        return []

    async def query_knowledge(self, agent, limit=5):
        return []


def _make_ctx(forum) -> SimpleNamespace:
    self_state = SimpleNamespace(
        gold=0,
        hits=50,
        hits_max=50,
        weight=0,
        weight_max=0,
        skills={},
        equipment={},  # no backpack -> inventory section is skipped
    )
    perception = SimpleNamespace(
        self_state=self_state,
        world=SimpleNamespace(items={}),
    )
    cfg = SimpleNamespace(forum=SimpleNamespace(post_interval=3600))
    return SimpleNamespace(
        blackboard={"forum_client": forum},
        memory_db=_FakeMemory(),
        llm=_FakeLLM(),
        perception=perception,
        cfg=cfg,
    )


@pytest.mark.asyncio
async def test_failed_post_does_not_arm_cooldown() -> None:
    ctx = _make_ctx(_FailingForumClient())
    skill = ForumPost()

    # No prior post -> eligible.
    assert await skill.can_execute(ctx) is True

    result = await skill.execute(ctx)
    assert result.success is False
    # The degraded-network failure must leave the cooldown untouched so the
    # next tick can retry instead of going silent for a full post_interval.
    assert "forum_last_post" not in ctx.blackboard
    assert await skill.can_execute(ctx) is True


@pytest.mark.asyncio
async def test_successful_post_arms_cooldown() -> None:
    ctx = _make_ctx(MockForumClient())
    skill = ForumPost()

    assert await skill.can_execute(ctx) is True

    result = await skill.execute(ctx)
    assert result.success is True
    # A real post arms the cooldown, blocking immediate re-posting.
    assert "forum_last_post" in ctx.blackboard
    assert await skill.can_execute(ctx) is False
