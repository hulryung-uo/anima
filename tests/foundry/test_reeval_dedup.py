"""reeval._main must not re-eval the same genome twice.

A genome named explicitly on the command line that is ALSO a grid elite used to
appear twice in the target list (the ``--elites`` set + the explicit id, with no
de-dup). Each duplicate burns a second full live eval window for no new evidence
AND counts that genome's replication ratio twice in the final median verdict —
the median is supposed to summarise N DISTINCT genomes, so a doubled entry
silently biases this demotion-evidence tool toward one genome.
"""
from __future__ import annotations

from foundry import reeval
from foundry.kernel.archive import Archive, Genome, cell_to_str


def _make_archive(tmp_path) -> Archive:
    arc = Archive(tmp_path / "archive")
    # Two grid elites in distinct cells.
    for n, cell, fit in ((1, ["GATHERING", 0], 50.0), (2, ["MAGIC", 1], 200.0)):
        g = Genome(
            id=f"g_{n:05d}",
            code_ref="deadbeef",
            config={"persona": "miner", "fixed_start": "miner"},
            eval={"fitness": fit, "cell": cell, "per_seed_fitness": [fit]},
        )
        arc.save_genome(g)
        arc.grid[cell_to_str(tuple(cell))] = g.id
    return arc


def test_explicit_id_that_is_also_an_elite_runs_once(tmp_path, monkeypatch, capsys):
    arc = _make_archive(tmp_path)

    calls: list[str] = []

    def _fake_reeval(arc, g, seeds, window_s, slot=0):
        calls.append(g.id)
        return {
            "genome": g.id, "cell": g.cell, "recorded": g.fitness,
            "recorded_seeds": [g.fitness], "ok": True,
            "held_out": g.fitness, "held_out_seeds": [g.fitness],
            "held_out_cell": list(g.cell), "ratio": 1.0,
        }

    monkeypatch.setattr(reeval, "Archive", lambda root: arc)
    monkeypatch.setattr(reeval, "reeval_genome", _fake_reeval)

    # g_00001 is named AND is a grid elite (so --elites would pick it up too).
    rc = reeval._main(["g_00001", "--elites", "--seeds", "1", "--window", "60"])
    assert rc == 0

    # Each distinct genome eval'd exactly once — no doubled live window.
    assert calls.count("g_00001") == 1
    assert calls.count("g_00002") == 1
    assert sorted(calls) == ["g_00001", "g_00002"]

    # The median verdict is computed over the 2 DISTINCT genomes, not 3 entries.
    out = capsys.readouterr().out
    assert "over 2 genomes" in out


def test_repeated_explicit_id_runs_once(tmp_path, monkeypatch):
    arc = _make_archive(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr(reeval, "Archive", lambda root: arc)
    monkeypatch.setattr(
        reeval, "reeval_genome",
        lambda arc, g, seeds, window_s, slot=0: (
            calls.append(g.id) or {
                "genome": g.id, "cell": g.cell, "recorded": g.fitness,
                "recorded_seeds": [g.fitness], "ok": True,
                "held_out": g.fitness, "held_out_seeds": [g.fitness],
                "held_out_cell": list(g.cell), "ratio": 1.0,
            }
        ),
    )

    rc = reeval._main(["g_00001", "g_00001", "--seeds", "1", "--window", "60"])
    assert rc == 0
    assert calls == ["g_00001"]
