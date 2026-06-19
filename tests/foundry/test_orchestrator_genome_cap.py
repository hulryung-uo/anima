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


def test_per_run_cap_still_stops_a_runaway_run(monkeypatch):
    """The cap must still fire on THIS run's own output (backstop intact)."""
    constructed: list[int] = []
    orig_outcome = _CycleOutcome

    def _spy_outcome(*args, **kwargs):
        out = orig_outcome(*args, **kwargs)
        constructed.append(out.cycle)
        return out

    monkeypatch.setattr(orchestrator, "_CycleOutcome", _spy_outcome)
    monkeypatch.setattr(orchestrator.safety, "MAX_GENOMES_PER_RUN", 3)
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
        error = "stubbed"

    monkeypatch.setattr(orchestrator, "run_eval_multi",
                        lambda cfg, **_k: _FailedEval())

    rc = RunConfig(n_cycles=10, parallel=1, backend="noop", window_s=1, seeds=1)
    orchestrator.run(rc)

    # submitted reaches the cap (3) and the guard stops scheduling the rest,
    # even though disk total never changed (evals fail → no add).
    assert constructed == [1, 2, 3], (
        f"per-run cap (3) must still backstop a runaway run; got {constructed}"
    )
