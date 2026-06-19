"""Per-cycle in-world chronicle posts to the UO Tavern forum.

Each develop cycle is one of the agents spending a session at their trade and
either bettering themselves or not. This narrates that, IN WORLD — a training
log a Britannia resident might keep — with no fitness/genome/code vocabulary.
The orchestrator calls post_cycle_note() after each evaluated cycle; failures
never propagate into the run (the forum is best-effort).

Mutator-editable infrastructure — it only reads results and posts text; it
never touches the kernel or the grid.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# profession cell key -> (trade noun, workplace, first-person verb phrase)
_TRADE = {
    "GATHERING": ("mining", "the Minoc rock face", "swung my pick at the mountain"),
    "CRAFTING": ("smithing", "the forge", "stood at the anvil"),
    "COMBAT": ("the blade", "the practice ground", "traded blows with the beasts"),
    "MAGIC": ("the Art", "my reagent table", "drilled my spells"),
    "BARD-SOCIAL": ("music", "the square", "played until my fingers ached"),
    "THIEF-STEALTH": ("the shadows", "a quiet corner", "practiced melting from sight"),
    "NONE": ("wandering", "the road", "kept myself busy"),
}

_SOC = {0: "kept to myself", 1: "muttered to passers-by now and then",
        2: "chattered the whole while"}


def _config_key() -> tuple[str, str] | None:
    try:
        import yaml
        cfg = yaml.safe_load((REPO / "config.yaml").read_text())
        f = cfg.get("forum", {})
        base = f.get("base_url", "https://www.uotavern.com/api").rstrip("/")
        key = f.get("api_key", "")
        return (base, key) if key else None
    except Exception:
        return None


def cycle_text(cell: tuple, status: str, fitness: float,
               prev_fitness: float | None) -> tuple[str, str]:
    """Build (title, body) for a cycle outcome. Pure — easy to unit-test."""
    prof = cell[0] if cell else "NONE"
    soc_bin = cell[1] if cell and len(cell) > 1 else 0
    trade, place, verb = _TRADE.get(prof, _TRADE["NONE"])
    soc = _SOC.get(soc_bin, _SOC[0])

    if status == "filled":
        title = f"A new hand at {trade}"
        outcome = (f"Today I {verb} at {place} for the first time in earnest, "
                   f"and {soc}. It is a beginning — no one had kept this exact "
                   f"craft-and-temperament before me. I will build on it.")
    elif status == "improved":
        # The kernel promotes on the RELIABILITY bound (mean − λ·pstdev), not the
        # raw point fitness — so an "improved" cycle can carry a point `fitness`
        # that is LOWER than the displaced elite's `prev_fitness` (it won by being
        # steadier, not higher). Claiming "bettered my best (200 → 150)" then prints
        # a DECREASE while asserting a gain — a self-contradiction. Only narrate a
        # numeric betterment when the point score actually rose; otherwise frame it
        # as the steadier result the promotion rule actually rewarded.
        if prev_fitness is None:
            gain = ""
        elif fitness > prev_fitness:
            gain = f" My work bettered my own best ({prev_fitness:.0f} → {fitness:.0f})."
        else:
            gain = (f" No louder than my old best ({prev_fitness:.0f}), but steadier "
                    f"hand to hand — and that is what held.")
        title = f"A better day at {trade}"
        outcome = (f"I {verb} at {place} and {soc}.{gain} Small change, real "
                   f"difference — the kind you only find by doing the thing again.")
    else:  # rejected
        title = f"An ordinary day at {trade}"
        outcome = (f"I {verb} at {place} and {soc}. I tried a new way of it, but "
                   f"my old habits still served me better today. Not every "
                   f"attempt is an improvement — that is the work.")

    body = (f"*Training log.*\n\n{outcome}\n\n— *one of us, at {place}*")
    return title, body


def post_cycle_note(cell: tuple, status: str, fitness: float,
                    prev_fitness: float | None, board: str = "tavern") -> bool:
    """Best-effort: post an in-world note for this cycle. Never raises."""
    creds = _config_key()
    if creds is None:
        return False
    base, key = creds
    try:
        # Everything below — text rendering, JSON serialization, request
        # construction, and the network round-trip — runs under one guard so
        # the documented "Never raises" contract holds. A malformed cell or a
        # non-numeric fitness (e.g. the "improved" branch's `:.0f` format) used
        # to raise OUTSIDE this try and crash the orchestrator's per-cycle path;
        # the forum is decorative and must never break the evolution run.
        title, body = cycle_text(cell, status, fitness, prev_fitness)
        import json
        payload = json.dumps({"board": board, "title": title,
                              "content": body}).encode()
        req = urllib.request.Request(
            f"{base}/agent/posts", data=payload, method="POST",
            headers={"x-api-key": key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 201
    except Exception:
        return False
