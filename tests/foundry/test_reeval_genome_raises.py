"""reeval._main must not abort the whole --elites pass when reeval_genome RAISES.

The per-genome STATUS PRINT and reeval_genome's config read were already
hardened against a null-config record. But reeval_genome itself runs real I/O
that can raise on a single genome: ``_prepare_worktree`` does
``git worktree add/reset --hard <code_ref>`` and raises ``RuntimeError`` when
that ref is unresolvable — a pinned ``refs/foundry/<id>`` commit that was
garbage-collected, or a stale/hand-edited ``code_ref`` no longer reachable in
the repo — and the eval runner can surface its own exception. With no guard
around the ``reeval_genome`` call, that one genome aborted the ``--elites``
demotion-evidence loop mid-run and silently dropped every genome queued after
it (the cross-genome replication median included). This is the same
one-bad-record-aborts-the-batch failure mode ``score.py``'s loop,
``_summarize_ratios``, and the null-config status print were already hardened
against.
"""
from __future__ import annotations

from foundry import reeval
from foundry.kernel.archive import Archive, Genome, cell_to_str


def _make_archive(tmp_path) -> Archive:
    arc = Archive(tmp_path / "archive")
    # Three grid elites. The MIDDLE one's worktree checkout will blow up
    # (unreachable code_ref); the pass must still re-eval the first and last.
    genomes = [
        Genome(id="g_00001", code_ref="deadbeef",
               config={"persona": "miner", "fixed_start": "miner"},
               eval={"fitness": 50.0, "cell": ["GATHERING", 0],
                     "per_seed_fitness": [50.0]}),
        Genome(id="g_00002", code_ref="gone0000",  # ref no longer reachable
               config={"persona": "mage", "fixed_start": "mage"},
               eval={"fitness": 200.0, "cell": ["MAGIC", 1],
                     "per_seed_fitness": [200.0]}),
        Genome(id="g_00003", code_ref="cafe0000",
               config={"persona": "thief", "fixed_start": "thief"},
               eval={"fitness": 30.0, "cell": ["THIEF-STEALTH", 0],
                     "per_seed_fitness": [30.0]}),
    ]
    cells = [["GATHERING", 0], ["MAGIC", 1], ["THIEF-STEALTH", 0]]
    for g, cell in zip(genomes, cells):
        arc.save_genome(g)
        arc.grid[cell_to_str(tuple(cell))] = g.id
    return arc


def test_reeval_genome_exception_does_not_abort_elites_pass(tmp_path, monkeypatch):
    arc = _make_archive(tmp_path)
    calls: list[str] = []

    def _fake_reeval(arc, g, seeds, window_s, slot=0):
        calls.append(g.id)
        if g.id == "g_00002":
            # What _prepare_worktree raises for an unresolvable code_ref.
            raise RuntimeError("worktree add failed: fatal: invalid reference: gone0000")
        return {
            "genome": g.id, "cell": g.cell, "recorded": g.fitness,
            "recorded_seeds": [g.fitness], "ok": True,
            "held_out": g.fitness, "held_out_seeds": [g.fitness],
            "held_out_cell": list(g.cell), "ratio": 1.0,
        }

    monkeypatch.setattr(reeval, "Archive", lambda root: arc)
    monkeypatch.setattr(reeval, "reeval_genome", _fake_reeval)

    rc = reeval._main(["--elites", "--seeds", "1", "--window", "60"])

    # The pass completes (does not propagate the RuntimeError)...
    assert rc == 0
    # ...and EVERY genome was attempted — the one after the raising genome was
    # NOT silently dropped (the bug aborted the loop at g_00002).
    assert sorted(calls) == ["g_00001", "g_00002", "g_00003"]


def test_reeval_genome_exception_excluded_from_replication_median(tmp_path, monkeypatch, capsys):
    arc = _make_archive(tmp_path)

    def _fake_reeval(arc, g, seeds, window_s, slot=0):
        if g.id == "g_00002":
            raise RuntimeError("worktree reset failed")
        return {
            "genome": g.id, "cell": g.cell, "recorded": g.fitness,
            "recorded_seeds": [g.fitness], "ok": True,
            "held_out": g.fitness, "held_out_seeds": [g.fitness],
            "held_out_cell": list(g.cell), "ratio": 1.0,
        }

    monkeypatch.setattr(reeval, "Archive", lambda root: arc)
    monkeypatch.setattr(reeval, "reeval_genome", _fake_reeval)

    rc = reeval._main(["--elites", "--seeds", "1", "--window", "60"])
    assert rc == 0

    out = capsys.readouterr().out
    # The failed genome is reported as skipped (not swallowed),
    assert "g_00002: SKIPPED" in out
    # and the two healthy genomes still produced the cross-genome median.
    assert "median replication ratio" in out
