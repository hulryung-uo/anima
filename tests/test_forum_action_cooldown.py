"""forum_write_action cooldown integrity on the degraded-network path.

forum_write_action is the periodic-essay path actually wired in main.py. A
transient transport failure makes TavernForumClient.create_post return an
empty post id; the BT node must NOT arm its (default 1h) post_interval
cooldown on such a failure, otherwise one dropped request silently suppresses
in-world posting for a full interval. A successful post (non-empty id) must
arm the cooldown as before. Mirrors tests/test_forum_post_cooldown.py for the
forum_skill.ForumPost path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.brain.behavior_tree import Status
from anima.skills.forum import MockForumClient
from anima.skills.forum_action import forum_write_action


class _FailingForumClient(MockForumClient):
    """Mimics the real client's degraded-network behavior: create returns ""."""

    async def create_post(self, title: str, body: str, category: str) -> str:
        return ""

    async def send_experience(self, **kwargs) -> str:
        return ""


class _OkForumClient(MockForumClient):
    async def send_experience(self, **kwargs) -> str:
        return "exp_1"


class _FakeLLM:
    async def chat(self, messages):
        return SimpleNamespace(text="TITLE: A Day\nBODY:\nI mined some ore.")


class _Episode(SimpleNamespace):
    pass


class _FakeMemory:
    async def query_episodes(self, agent, limit=15):
        # >= 3 episodes so the node proceeds past the min-episode gate.
        return [
            _Episode(action="mine", target="vein", outcome="success"),
            _Episode(action="smelt", target="ore", outcome="success"),
            _Episode(action="craft", target="tool", outcome="failure"),
        ]

    async def add_knowledge(self, *args, **kwargs):
        return None


def _make_ctx(forum) -> SimpleNamespace:
    self_state = SimpleNamespace(
        gold=0,
        hits=50,
        hits_max=50,
        weight=0,
        weight_max=0,
        x=100,
        y=100,
        skills={},
        equipment={},
    )
    perception = SimpleNamespace(
        self_state=self_state,
        world=SimpleNamespace(items={}),
    )
    cfg = SimpleNamespace(
        forum=SimpleNamespace(post_interval=3600, read_interval=300)
    )
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

    result = await forum_write_action(ctx)

    assert result is Status.FAILURE
    # The degraded-network failure must leave the cooldown untouched so the
    # next tick can retry instead of going silent for a full post_interval.
    assert "forum_last_post" not in ctx.blackboard


@pytest.mark.asyncio
async def test_successful_post_arms_cooldown() -> None:
    ctx = _make_ctx(_OkForumClient())

    result = await forum_write_action(ctx)

    assert result is Status.SUCCESS
    # A real post (non-empty id) arms the cooldown, blocking immediate reposting.
    assert "forum_last_post" in ctx.blackboard
