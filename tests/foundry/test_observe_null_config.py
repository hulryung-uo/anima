"""observe.history() / _is_dead_end() must survive a null-config genome.

A legacy or hand-edited genome record can carry ``"config": null``. The JSON
loads to ``None`` (``Genome.from_dict`` does ``config=d.get("config", {})``,
which returns the explicit null when the KEY exists), so ``g.config.get(...)``
raises ``AttributeError``. ``observe.history()`` is built EVERY develop cycle
(orchestrator ``job`` → ``observe.history(arc)``) over ``archive.all_genomes()``
— every genome ever written, including legacy ones — so a single null-config
record made history() raise, the broad ``except`` in ``job`` swallowed it as a
cycle failure, and the run produced nothing cycle after cycle. This is the same
null-config crash class just fixed in ``reeval`` (commit 5fc4e03), but on the
per-cycle hot path. Guard with ``g.config or {}`` like reeval does.
"""
from __future__ import annotations

import pytest

from foundry import observe
from foundry.kernel.archive import Archive, Genome


def _null_config_genome(gid: str = "g_00001") -> Genome:
    # Survives the two breakdown short-circuits in _is_dead_end (non-zero skill
    # gain, healthy viability gate) so the code path REACHES the config access —
    # otherwise the bug is masked by an early return.
    return Genome.from_dict({
        "id": gid,
        "parent": None,
        "code_ref": "deadbeef",
        "config": None,  # the legacy null-config record
        "eval": {
            "fitness": 50.0,
            "cell": ["GATHERING", 0],
            "per_seed_fitness": [50.0],
            "breakdown": {"skill_gain_rate": 5.0, "viability_gate": 1.0},
        },
        "hypothesis": "legacy null-config genome",
    })


def test_null_config_genome_loads_to_none():
    # Pin the precondition: an explicit JSON null becomes Python None (NOT {}).
    assert _null_config_genome().config is None


def test_is_dead_end_tolerates_null_config():
    g = _null_config_genome()
    # Must not raise AttributeError; with no target_cell it is not a dead end
    # (it gained skill and stayed viable).
    assert observe._is_dead_end(g) is False


def test_history_tolerates_null_config_genome(tmp_path):
    arc = Archive(tmp_path / "archive")
    # A healthy current genome plus a legacy null-config one in the archive.
    arc.add(Genome(
        id="g_00002", parent=None, code_ref="cafe",
        config={"target_cell": None},
        eval={
            "fitness": 80.0,
            "cell": ["GATHERING", 1],
            "per_seed_fitness": [80.0, 80.0],
            "breakdown": {"skill_gain_rate": 8.0, "viability_gate": 1.0},
        },
        hypothesis="healthy",
    ))
    arc.save_genome(_null_config_genome("g_00001"))

    # history() iterates all_genomes() (includes the null-config record). It must
    # render without raising and still mention both genomes.
    text = observe.history(arc)
    assert "g_00001" in text
    assert "g_00002" in text


def test_history_would_raise_without_guard():
    # Direct proof the guard is load-bearing: the raw ``g.config.get`` the fix
    # replaced raises on a null config, while the guarded form does not.
    g = _null_config_genome()
    with pytest.raises(AttributeError):
        g.config.get("target_cell")          # the old, unguarded access
    assert (g.config or {}).get("target_cell") is None  # the guarded access
