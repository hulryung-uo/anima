"""reeval._main must not abort the whole --elites pass on a null-config genome.

A legacy / hand-edited genome record can carry ``"config": null``: ``Genome``'s
``from_dict`` does ``config=d.get("config", {})``, which returns the EXPLICIT
``None`` when the key is present with a null value (the default only applies when
the key is ABSENT). The per-genome status line in ``_main`` did
``g.config.get('persona')`` with no guard, so that one record raised
``AttributeError: 'NoneType' object has no attribute 'get'`` and aborted the
ENTIRE re-eval loop — silently dropping every genome queued after it, the
demotion-evidence median included. This is the same one-bad-record-aborts-the-
batch failure mode score.py and _summarize_ratios were hardened against;
reeval_genome itself already guards with ``g.config or {}``.
"""
from __future__ import annotations

from foundry import reeval
from foundry.kernel.archive import Archive, Genome, cell_to_str


def _make_archive(tmp_path) -> Archive:
    arc = Archive(tmp_path / "archive")
    # A normal elite and a legacy elite whose config round-tripped to None.
    good = Genome(
        id="g_00001", code_ref="deadbeef",
        config={"persona": "miner", "fixed_start": "miner"},
        eval={"fitness": 50.0, "cell": ["GATHERING", 0], "per_seed_fitness": [50.0]},
    )
    bad = Genome(
        id="g_00002", code_ref="deadbeef",
        config=None,  # legacy/hand record: "config": null in the genome JSON
        eval={"fitness": 200.0, "cell": ["MAGIC", 1], "per_seed_fitness": [200.0]},
    )
    for g, cell in ((good, ["GATHERING", 0]), (bad, ["MAGIC", 1])):
        arc.save_genome(g)
        arc.grid[cell_to_str(tuple(cell))] = g.id
    return arc


def test_null_config_genome_does_not_abort_elites_pass(tmp_path, monkeypatch):
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

    # With the bug, the status print raised AttributeError on the null-config
    # genome and the whole loop aborted; with the fix it completes and BOTH
    # genomes are re-eval'd.
    rc = reeval._main(["--elites", "--seeds", "1", "--window", "60"])
    assert rc == 0
    assert sorted(calls) == ["g_00001", "g_00002"]


def test_null_config_genome_named_explicitly_still_runs(tmp_path, monkeypatch):
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

    rc = reeval._main(["g_00002", "--seeds", "1", "--window", "60"])
    assert rc == 0
    assert calls == ["g_00002"]
