"""MAX_GENOMES_PER_RUN bounds what THIS run produces, not the all-time total.

Regression: the scheduling guard compared ``arc.summary()["total_genomes"]``
(every genome every prior run ever wrote, counted off disk) against
``MAX_GENOMES_PER_RUN``. ``total_genomes`` only grows, so once the archive
accumulated that many genomes across its whole history, EVERY subsequent run
refused to schedule even its first cycle — the "runaway loop backstop" inverted
into a permanent global ceiling that silently bricked the evolution loop.

The fix caps on this run's DELTA (current total − baseline at run start, plus
the in-flight ``submitted`` count). We assert that with a pre-existing archive
already at the cap, a fresh run still schedules its cycles normally.
"""
from __future__ import annotations

from foundry import orchestrator
from foundry.orchestrator import RunConfig, _CycleOutcome


class _FakeArchive:
    """Archive stand-in whose all-time genome count starts AT the cap.

    The grid is non-empty so run() skips seeding; ``add`` never runs because the
    stubbed evals never report ok (we only care about what gets SCHEDULED).
    """

    def __init__(self, *_a, **_k) -> None:
        # Already at the per-run cap from prior runs' accumulated genomes.
        self._total = orchestrator.safety.MAX_GENOMES_PER_RUN

    def elites(self):
        return [object()]  # non-empty → run() skips seed_archive

    def summary(self):
        return {"total_genomes": self._total, "filled_cells": 0,
                "qd_score": 0.0, "best_fitness": 0.0}

    def add(self, g):  # pragma: no cover - evals never "ok" in this test
        raise AssertionError("add should not run in this test")


def test_full_archive_does_not_block_a_fresh_run(monkeypatch):
    N_CYCLES = 5
    constructed: list[int] = []

    orig_outcome = _CycleOutcome

    def _spy_outcome(*args, **kwargs):
        out = orig_outcome(*args, **kwargs)
        constructed.append(out.cycle)
        return out

    monkeypatch.setattr(orchestrator, "_CycleOutcome", _spy_outcome)
    monkeypatch.setattr(orchestrator.safety, "kill_switch_active",
                        lambda *_a, **_k: False)
    monkeypatch.setattr(orchestrator.safety, "head_sha",
                        lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr(orchestrator.safety, "revert_kernel",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator.safety, "kernel_is_clean",
                        lambda *_a, **_k: True)

    monkeypatch.setattr(orchestrator, "Archive", _FakeArchive)
    monkeypatch.setattr(orchestrator, "_prepare_worktree",
                        lambda slot, ref: orchestrator.ANIMA_ROOT)
    monkeypatch.setattr(orchestrator, "_pin_ref", lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator.select, "suggest_target_cell",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator.select, "choose_parent_for_target",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator.observe, "history", lambda *_a, **_k: "")

    class _NoopMutation:
        changed = True
        code_ref = "deadbeef"
        hypothesis = "noop"
        error = ""

    monkeypatch.setattr(orchestrator.mutate, "mutate_noop",
                        lambda *_a, **_k: _NoopMutation())

    class _FailedEval:
        ok = False
        error = "stubbed: never inserts"

    monkeypatch.setattr(orchestrator, "run_eval_multi",
                        lambda cfg, **_k: _FailedEval())

    rc = RunConfig(n_cycles=N_CYCLES, parallel=1, backend="noop",
                   window_s=1, seeds=1)

    orchestrator.run(rc)

    # With the bug, the guard saw total_genomes (== cap) + submitted >= cap on
    # the very first iteration and broke before scheduling anything. With the
    # fix, the per-run delta starts at 0, so all N_CYCLES are scheduled.
    assert constructed == list(range(1, N_CYCLES + 1)), (
        f"a pre-filled archive must not block a fresh run; got {constructed}"
    )


class _CountingArchive:
    """Archive stand-in whose disk total GROWS by one per successfully-added
    genome — the realistic path the runaway backstop must bound.

    Starts at the cap so the test also proves the per-run guard reads the
    DELTA (this run's own output), not the all-time total.
    """

    def __init__(self, *_a, **_k) -> None:
        self._total = orchestrator.safety.MAX_GENOMES_PER_RUN

    def elites(self):
        return [object()]  # non-empty → run() skips seed_archive

    def summary(self):
        return {"total_genomes": self._total, "filled_cells": 0,
                "qd_score": 0.0, "best_fitness": 0.0}

    def add(self, g):  # each drained genome bumps the on-disk total
        self._total += 1


def _patch_common(monkeypatch, archive_cls, eval_factory, cap=None):
    if cap is not None:
        monkeypatch.setattr(orchestrator.safety, "MAX_GENOMES_PER_RUN", cap)
    monkeypatch.setattr(orchestrator.safety, "kill_switch_active",
                        lambda *_a, **_k: False)
    monkeypatch.setattr(orchestrator.safety, "head_sha",
                        lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr(orchestrator.safety, "revert_kernel",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator.safety, "kernel_is_clean",
                        lambda *_a, **_k: True)
    monkeypatch.setattr(orchestrator, "Archive", archive_cls)
    monkeypatch.setattr(orchestrator, "_prepare_worktree",
                        lambda slot, ref: orchestrator.ANIMA_ROOT)
    monkeypatch.setattr(orchestrator, "_pin_ref", lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator.select, "suggest_target_cell",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator.select, "choose_parent_for_target",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator.observe, "history", lambda *_a, **_k: "")

    class _NoopMutation:
        changed = True
        code_ref = "deadbeef"
        hypothesis = "noop"
        error = ""

    monkeypatch.setattr(orchestrator.mutate, "mutate_noop",
                        lambda *_a, **_k: _NoopMutation())
    monkeypatch.setattr(orchestrator, "run_eval_multi",
                        lambda cfg, **_k: eval_factory())
    # _drain_one builds a Genome from the (stubbed) eval result; bypass the
    # heavy real assembly and just hand `add` a placeholder.
    monkeypatch.setattr(orchestrator, "_genome_from",
                        lambda *a, **k: _StubGenome())


class _StubGenome:
    id = "g_stub"
    code_ref = "deadbeef"
    fitness = 1.0
    cell = ("GATHERING", 0)


class _InsertResult:
    status = "filled"
    prev_fitness = None


def _spy_constructed(monkeypatch):
    constructed: list[int] = []
    orig_outcome = _CycleOutcome

    def _spy_outcome(*args, **kwargs):
        out = orig_outcome(*args, **kwargs)
        constructed.append(out.cycle)
        return out

    monkeypatch.setattr(orchestrator, "_CycleOutcome", _spy_outcome)
    return constructed


class _OkEval:
    ok = True
    cell = ("GATHERING", 0)


def test_per_run_cap_fires_at_full_budget_not_half(monkeypatch):
    """Regression: with genomes ACTUALLY accumulating, the cap must trip at the
    full budget. The old guard added the cumulative `submitted` count to the
    disk delta, double-counting every drained genome and stopping at ~half the
    budget (here: after 2 cycles instead of 4).
    """
    constructed = _spy_constructed(monkeypatch)
    monkeypatch.setattr(_CountingArchive, "add",
                        lambda self, g: setattr(self, "_total", self._total + 1))
    _patch_common(monkeypatch, _CountingArchive, _OkEval, cap=4)
    # add() must return an InsertResult-shaped object for _drain_one's print.
    monkeypatch.setattr(_CountingArchive, "add",
                        lambda self, g: (setattr(self, "_total", self._total + 1)
                                         or _InsertResult()))

    rc = RunConfig(n_cycles=10, parallel=1, backend="noop", window_s=1, seeds=1)
    orchestrator.run(rc)

    # delta grows 0,1,2,3 as cycles drain; in_flight is 0/1 at each guard. The
    # guard trips when delta(+in_flight) >= 4, i.e. exactly the 4 budgeted
    # genomes get scheduled — not 2 (the buggy half-budget halt).
    assert constructed == [1, 2, 3, 4], (
        f"per-run cap (4) must fire at the FULL budget, not half; got {constructed}"
    )


def test_failed_evals_do_not_consume_the_cap(monkeypatch):
    """A cycle whose eval fails produces NO genome, so it must not count toward
    the per-run cap (only real output bounds the run)."""
    constructed = _spy_constructed(monkeypatch)

    class _FailedEval:
        ok = False
        error = "stubbed"

    _patch_common(monkeypatch, _FakeArchive, _FailedEval, cap=3)

    rc = RunConfig(n_cycles=6, parallel=1, backend="noop", window_s=1, seeds=1)
    orchestrator.run(rc)

    # No genome ever lands (disk delta stays 0) and only one cycle is ever in
    # flight at a time, so the cap never fires — all 6 cycles are scheduled.
    assert constructed == [1, 2, 3, 4, 5, 6], (
        f"failed evals must not consume the genome cap; got {constructed}"
    )
