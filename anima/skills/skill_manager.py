"""Skill & stat lock manager — sets skill/stat locks based on persona.

UOR rules:
- Total skill cap: 700.0
- Individual skill cap: 100.0
- Total stat cap: 225
- Individual stat cap: 100
- Lock states: 0=Up (gain), 1=Down (lose to make room), 2=Locked (no change)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from anima.client.packets import build_skill_lock, build_stat_lock
from anima.perception.enums import Lock

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

logger = structlog.get_logger()

# Skill IDs (UOR era, 49 skills)
SKILL_ALCHEMY = 0
SKILL_ANATOMY = 1
SKILL_ANIMAL_LORE = 2
SKILL_ITEM_ID = 3
SKILL_ARMS_LORE = 4
SKILL_PARRYING = 5
SKILL_BEGGING = 6
SKILL_BLACKSMITH = 7
SKILL_BOWCRAFT = 8
SKILL_PEACEMAKING = 9
SKILL_CAMPING = 10
SKILL_CARPENTRY = 11
SKILL_CARTOGRAPHY = 12
SKILL_COOKING = 13
SKILL_DETECT_HIDDEN = 14
SKILL_ENTICEMENT = 15
SKILL_EVAL_INT = 16
SKILL_HEALING = 17
SKILL_FISHING = 18
SKILL_FORENSIC_EVAL = 19
SKILL_HERDING = 20
SKILL_HIDING = 21
SKILL_PROVOCATION = 22
SKILL_INSCRIPTION = 23
SKILL_LOCKPICKING = 24
SKILL_MAGERY = 25
SKILL_RESIST_SPELLS = 26
SKILL_TACTICS = 27
SKILL_SNOOPING = 28
SKILL_MUSICIANSHIP = 29
SKILL_POISONING = 30
SKILL_ARCHERY = 31
SKILL_SPIRIT_SPEAK = 32
SKILL_STEALING = 33
SKILL_TAILORING = 34
SKILL_ANIMAL_TAMING = 35
SKILL_TASTE_ID = 36
SKILL_TINKERING = 37
SKILL_TRACKING = 38
SKILL_VETERINARY = 39
SKILL_SWORDSMANSHIP = 40
SKILL_MACE_FIGHTING = 41
SKILL_FENCING = 42
SKILL_WRESTLING = 43
SKILL_LUMBERJACKING = 44
SKILL_MINING = 45
SKILL_MEDITATION = 46
SKILL_STEALTH = 47
SKILL_REMOVE_TRAP = 48

# Persona skill/stat lock profiles
# skills_up: skills to raise (Lock.UP)
# stats_lock: (str_lock, dex_lock, int_lock)
# All other skills default to Lock.LOCKED

PERSONA_SKILL_PROFILES: dict[str, dict] = {
    "adventurer": {
        "skills_up": [
            SKILL_SWORDSMANSHIP, SKILL_HEALING, SKILL_TACTICS,
            SKILL_ANATOMY, SKILL_PARRYING, SKILL_WRESTLING,
        ],
        "stats_lock": (Lock.UP, Lock.UP, Lock.LOCKED),
    },
    "blacksmith": {
        "skills_up": [
            SKILL_MINING, SKILL_BLACKSMITH, SKILL_ARMS_LORE,
            SKILL_TINKERING, SKILL_CARPENTRY,
        ],
        "stats_lock": (Lock.UP, Lock.LOCKED, Lock.LOCKED),
    },
    "merchant": {
        "skills_up": [
            SKILL_TINKERING, SKILL_TAILORING, SKILL_ITEM_ID,
            SKILL_ARMS_LORE, SKILL_COOKING,
        ],
        "stats_lock": (Lock.LOCKED, Lock.UP, Lock.UP),
    },
    "mage": {
        "skills_up": [
            SKILL_MAGERY, SKILL_MEDITATION, SKILL_EVAL_INT,
            SKILL_RESIST_SPELLS, SKILL_INSCRIPTION, SKILL_WRESTLING,
        ],
        "stats_lock": (Lock.LOCKED, Lock.LOCKED, Lock.UP),
    },
    "bard": {
        "skills_up": [
            SKILL_MUSICIANSHIP, SKILL_PEACEMAKING, SKILL_PROVOCATION,
            SKILL_MAGERY, SKILL_MEDITATION,
        ],
        "stats_lock": (Lock.LOCKED, Lock.UP, Lock.UP),
    },
    "ranger": {
        "skills_up": [
            SKILL_ARCHERY, SKILL_TACTICS, SKILL_HEALING,
            SKILL_TRACKING, SKILL_LUMBERJACKING, SKILL_ANATOMY,
        ],
        "stats_lock": (Lock.UP, Lock.UP, Lock.LOCKED),
    },
    "woodcutter": {
        "skills_up": [
            SKILL_LUMBERJACKING, SKILL_CARPENTRY, SKILL_TINKERING,
            SKILL_ARMS_LORE,
        ],
        "stats_lock": (Lock.UP, Lock.LOCKED, Lock.LOCKED),
    },
    "miner": {
        "skills_up": [
            SKILL_MINING, SKILL_BLACKSMITH, SKILL_TINKERING,
            SKILL_ARMS_LORE,
        ],
        "stats_lock": (Lock.UP, Lock.LOCKED, Lock.LOCKED),
    },
}


async def apply_skill_locks(ctx: BrainContext, persona_name: str) -> int:
    """Set skill and stat locks based on persona profile.

    Skills in the persona's skills_up list are set to Up.
    All other known skills are set to Locked.
    Stats are set per persona profile.

    Returns the number of lock packets sent.
    """
    profile = PERSONA_SKILL_PROFILES.get(persona_name)
    if not profile:
        logger.warning("skill_profile_not_found", persona=persona_name)
        return 0

    skills_up = set(profile["skills_up"])
    str_lock, dex_lock, int_lock = profile["stats_lock"]

    sent = 0

    # Set skill locks for all known skills
    for skill_id, skill_info in ctx.perception.self_state.skills.items():
        desired = Lock.UP if skill_id in skills_up else Lock.LOCKED
        if skill_info.lock != desired:
            await ctx.conn.send_packet(build_skill_lock(skill_id, desired.value))
            sent += 1

    # Set stat locks
    stat_locks = [(0, str_lock), (1, dex_lock), (2, int_lock)]
    for stat_idx, desired_lock in stat_locks:
        await ctx.conn.send_packet(build_stat_lock(stat_idx, desired_lock.value))
        sent += 1

    logger.info(
        "skill_locks_applied",
        persona=persona_name,
        skills_up=[s for s in skills_up],
        stats=f"STR={str_lock.name}/DEX={dex_lock.name}/INT={int_lock.name}",
        packets_sent=sent,
    )
    return sent
