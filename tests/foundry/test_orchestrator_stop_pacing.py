"""STOP-file pacing: a mid-run `touch foundry/STOP` must stop SCHEDULING the
remaining cycles, not merely self-skip cycles already enqueued.

Regression: `pool.submit` never blocks, so the old loop enqueued ALL n_cycles
jobs within milliseconds of run start — before any operator STOP (the documented
halt: `touch foundry/STOP`) or the genome cap could take effect. The
scheduling-loop guards were dead for every realistic run (n_cycles > parallel):
each remaining cycle was submitted anyway and only self-skipped inside the
worker, after constructing its outcome and consuming a pool thread.

The fix paces submission to slot availability so the STOP/cap guards are
re-evaluated at the REAL boundary of each cycle start. We assert that once STOP
is set after the first cycle, the orchestrator submits no further cycles — it
constructs at most `parallel` outcomes beyond the one in flight, not all
n_cycles.
"""
from __future__ import annotations

import threading

from foundry import orchestrator
from foundry.orchestrator import RunConfig, _CycleOutcome


class _FakeArchive:
    """Minimal Archive stand-in: non-empty grid (skip seeding), no-op add."""

    def __init__(self, *_a, **_k) -> None:
        pass

    def elites(self):
        return [object()]  # non-empty → run() skips seed_archive

    def summary(self):
        return {"total_genomes": 0, "filled_cells": 0, "qd_score": 0.0,
                "best_fitness": 0.0}

    def add(self, g):  # pragma: no cover - not reached (evals never "ok")
        raise AssertionError("add should not run in this test")


def test_stop_after_first_cycle_halts_scheduling(monkeypatch):
    N_CYCLES = 12
    first_eval_started = threading.Event()
    release_first_eval = threading.Event()
    constructed: list[int] = []

    # --- count every cycle the scheduler actually submits ------------------
    orig_outcome = _CycleOutcome

    def _spy_outcome(*args, **kwargs):
        out = orig_outcome(*args, **kwargs)
        constructed.append(out.cycle)
        return out

    monkeypatch.setattr(orchestrator, "_CycleOutcome", _spy_outcome)

    # --- STOP flips on once the first cycle's eval is under way ------------
    def _kill_switch_active(*_a, **_k):
        # Active only after the first eval has begun: the first cycle runs to
        # completion, then scheduling must stop before any further submit.
        return first_eval_started.is_set()

    monkeypatch.setattr(orchestrator.safety, "kill_switch_active",
                        _kill_switch_active)
    monkeypatch.setattr(orchestrator.safety, "head_sha", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr(orchestrator.safety, "revert_kernel", lambda *_a, **_k: None)
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

    def _fake_eval(cfg, **_k):
        # First eval blocks until released, signalling it started so STOP can
        # flip; later evals (if the bug let them be scheduled) return at once.
        if not first_eval_started.is_set():
            first_eval_started.set()
            release_first_eval.wait(timeout=5)
        return _FailedEval()

    monkeypatch.setattr(orchestrator, "run_eval_multi", _fake_eval)

    rc = RunConfig(n_cycles=N_CYCLES, parallel=1, backend="noop",
                   window_s=1, seeds=1)

    # Let the first cycle finish shortly after it has started + STOP is set.
    def _releaser():
        first_eval_started.wait(timeout=5)
        release_first_eval.set()

    t = threading.Thread(target=_releaser, daemon=True)
    t.start()

    orchestrator.run(rc)
    t.join(timeout=5)

    # With the fix: cycle 1 is submitted and runs; STOP is then observed at the
    # next scheduling boundary, so NO further cycle is submitted. With the bug,
    # all N_CYCLES jobs were submitted up front (each self-skipping), so far
    # more than a couple of outcomes would be constructed.
    assert constructed == [1], (
        f"expected only cycle 1 to be scheduled before STOP, got {constructed}"
    )
