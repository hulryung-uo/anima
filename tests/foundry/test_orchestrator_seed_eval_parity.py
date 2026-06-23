"""seed_archive must evaluate HEAD with the SAME adaptive-seed knobs as the cycle.

The cycle eval (orchestrator.run) calls run_eval_multi with
``seeds=rc.seeds, max_seeds=rc.max_seeds, cv_high=rc.cv_high`` so a noisy genome
tops up to max_seeds and is judged under the configured CV gate. seed_archive
seeds the very same code (current HEAD) as the root genome — but historically
passed only ``seeds=rc.seeds``, dropping the top-up cap and CV threshold. The
root genome was therefore measured under a *different* trust regime than every
descendant the run produced, biasing the seed's fitness/reliability against the
cycle's. This guards that the seed path forwards both kwargs verbatim from the
RunConfig.
"""
from __future__ import annotations

import foundry.orchestrator as orch
from foundry.kernel.eval import EvalResult
from foundry.orchestrator import RunConfig, seed_archive


def _capture(monkeypatch):
    """Stub run_eval_multi to record its kwargs and short-circuit seed_archive.

    Returning ok=False makes seed_archive return None right after the call,
    before it touches the Archive / genome helpers — so we exercise only the
    eval-invocation contract under test.
    """
    captured: dict = {}

    def fake_run_eval_multi(cfg, seeds=1, max_seeds=None, cv_high=0.30):
        captured["seeds"] = seeds
        captured["max_seeds"] = max_seeds
        captured["cv_high"] = cv_high
        return EvalResult(ok=False, error="stub: short-circuit")

    monkeypatch.setattr(orch, "run_eval_multi", fake_run_eval_multi)
    return captured


def test_seed_archive_forwards_max_seeds_and_cv_high(monkeypatch) -> None:
    captured = _capture(monkeypatch)
    # max_seeds must exceed base seeds (and fit the lane budget) to survive
    # RunConfig.__post_init__ without being nulled out.
    rc = RunConfig(parallel=1, seeds=2, max_seeds=4, cv_high=0.42)
    assert rc.max_seeds == 4  # precondition: post_init kept the top-up cap

    out = seed_archive(arc=None, rc=rc)

    assert out is None  # stub forced the failure short-circuit
    assert captured["seeds"] == rc.seeds
    assert captured["max_seeds"] == rc.max_seeds
    assert captured["cv_high"] == rc.cv_high


def test_seed_archive_matches_cycle_eval_knobs(monkeypatch) -> None:
    """The seed call's adaptive-seed knobs are exactly the ones the cycle uses:
    rc.seeds / rc.max_seeds / rc.cv_high — no defaults sneaking in."""
    captured = _capture(monkeypatch)
    rc = RunConfig(parallel=1, seeds=1, max_seeds=3, cv_high=0.25)

    seed_archive(arc=None, rc=rc)

    # mirrors the cycle-eval kwargs in orchestrator.run()
    assert captured == {
        "seeds": rc.seeds,
        "max_seeds": rc.max_seeds,
        "cv_high": rc.cv_high,
    }


def test_seed_archive_passes_default_cv_high_when_unset(monkeypatch) -> None:
    """With no overrides, the RunConfig default cv_high still flows through
    (it is not silently replaced by the run_eval_multi signature default)."""
    captured = _capture(monkeypatch)
    rc = RunConfig(parallel=1, seeds=1)

    seed_archive(arc=None, rc=rc)

    assert captured["cv_high"] == rc.cv_high
    assert captured["max_seeds"] == rc.max_seeds
