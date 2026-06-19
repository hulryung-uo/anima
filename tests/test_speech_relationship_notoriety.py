"""Regression: a hostile speaker must not build positive disposition/trust.

The relationship table is written *only* from respond_to_speech, so the sign of
the disposition/trust delta there is the agent's entire friend/foe picture of a
person (it feeds memory/retrieval._disposition_word and from there the LLM
context). The old code added a flat +0.05 disposition / +0.02 trust on every
incoming line regardless of the speaker, so a red MURDERER or orange ENEMY who
merely kept talking climbed toward "friendly" with rising trust — and nothing
else ever recorded a negative disposition to undo it. This pins the corrected
behavior: hostile-notoriety speakers earn a negative delta; innocent/neutral
speakers keep the original small positive bump.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from anima.action.speech import respond_to_speech
from anima.memory.database import MemoryDB
from anima.perception.enums import MessageType, NotorietyFlag
from anima.perception.world_state import MobileInfo

AGENT = "Anima"
SELF_SERIAL = 0x00000001
SPEAKER_SERIAL = 0x00012345


class _FakeConn:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_packet(self, data: bytes) -> None:
        self.sent.append(data)


class _FakeWorld:
    def __init__(self) -> None:
        self.mobiles: dict[int, MobileInfo] = {}


async def _fresh_db() -> MemoryDB:
    db = MemoryDB(":memory:")
    await db.init()
    return db


def _make_ctx(db: MemoryDB, speaker_notoriety: NotorietyFlag) -> SimpleNamespace:
    speech = {
        "serial": SPEAKER_SERIAL,
        "name": "Stranger",
        "text": "you there",  # not a greeting -> no early tier-1 return matters
        "type": MessageType.REGULAR,
    }
    self_state = SimpleNamespace(serial=SELF_SERIAL, x=100, y=200)
    world = _FakeWorld()
    world.mobiles[SPEAKER_SERIAL] = MobileInfo(
        serial=SPEAKER_SERIAL, name="Stranger", notoriety=speaker_notoriety
    )
    perception = SimpleNamespace(self_state=self_state, world=world)
    blackboard: dict = {
        "pending_speech": [speech],
        "persona": SimpleNamespace(name=AGENT),
    }
    return SimpleNamespace(
        blackboard=blackboard,
        perception=perception,
        conn=_FakeConn(),
        llm=None,
        memory_db=db,
    )


def test_murderer_speech_lowers_disposition_and_trust() -> None:
    async def run() -> None:
        db = await _fresh_db()
        ctx = _make_ctx(db, NotorietyFlag.MURDERER)
        await respond_to_speech(ctx)
        rel = await db.get_relationship(AGENT, SPEAKER_SERIAL)
        assert rel is not None
        # Foe: disposition went negative, trust dropped below the 0.5 default.
        assert rel.disposition < 0.0
        assert rel.trust < 0.5

    asyncio.run(run())


@pytest.mark.parametrize(
    "noto", [NotorietyFlag.CRIMINAL, NotorietyFlag.ENEMY, NotorietyFlag.MURDERER]
)
def test_repeated_hostile_speech_never_reaches_friendly(noto: NotorietyFlag) -> None:
    async def run() -> None:
        db = await _fresh_db()
        # The whole point of the bug: volume must not buy friendship.
        for _ in range(50):
            ctx = _make_ctx(db, noto)
            await respond_to_speech(ctx)
        rel = await db.get_relationship(AGENT, SPEAKER_SERIAL)
        assert rel is not None
        assert rel.interaction_count == 50
        # Stays a foe in the signal _disposition_word reads: clamps at the floor,
        # never climbs toward "friendly".
        assert rel.disposition <= -0.5
        assert rel.trust == 0.0

    asyncio.run(run())


def test_innocent_speaker_still_gains_disposition() -> None:
    async def run() -> None:
        db = await _fresh_db()
        ctx = _make_ctx(db, NotorietyFlag.INNOCENT)
        await respond_to_speech(ctx)
        rel = await db.get_relationship(AGENT, SPEAKER_SERIAL)
        assert rel is not None
        # Friendly path unchanged: small positive bump above neutral/default.
        assert rel.disposition == pytest.approx(0.05)
        assert rel.trust == pytest.approx(0.52)

    asyncio.run(run())


def test_missing_world_falls_back_to_positive_bump() -> None:
    # If the speaker isn't tracked (or there's no world), we can't prove hostility
    # so we keep the original benign-of-the-doubt positive delta.
    async def run() -> None:
        db = await _fresh_db()
        ctx = _make_ctx(db, NotorietyFlag.INNOCENT)
        ctx.perception.world.mobiles.clear()
        await respond_to_speech(ctx)
        rel = await db.get_relationship(AGENT, SPEAKER_SERIAL)
        assert rel is not None
        assert rel.disposition == pytest.approx(0.05)
        assert rel.trust == pytest.approx(0.52)

    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
