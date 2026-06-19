"""Tests for the offline meta-controller shadow-log analyzer.

The analyzer is a pure reader of `data/meta_shadow.jsonl`: it must compute
correct would-pick-vs-actual agreement stats, split them by policy, and never
raise on a missing/empty/malformed log.
"""
import json

import pytest

from anima.planner.shadow_analysis import (
    ShadowReport,
    analyze_shadow_log,
)


def _write_log(tmp_path, rows):
    p = tmp_path / "meta_shadow.jsonl"
    with p.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def test_missing_file_yields_empty_report(tmp_path):
    rep = analyze_shadow_log(tmp_path / "does_not_exist.jsonl")
    assert isinstance(rep, ShadowReport)
    assert rep.total_rows == 0
    assert rep.overall_agreement_rate == 0.0
    assert rep.by_policy == {}


def test_agreement_rate_math(tmp_path):
    # 3 heuristic rows: 2 agree, 1 diverge -> 2/3.
    rows = [
        {"policy": "heuristic", "would_pick": "mining", "actual_mode": "mining"},
        {"policy": "heuristic", "would_pick": "mining", "actual_mode": "mining"},
        {"policy": "heuristic", "would_pick": "rest", "actual_mode": "mining"},
    ]
    rep = analyze_shadow_log(_write_log(tmp_path, rows))
    assert rep.total_rows == 3
    assert rep.malformed_rows == 0
    s = rep.by_policy["heuristic"]
    assert s.rows == 3
    assert s.agreements == 2
    assert s.agreement_rate == pytest.approx(2 / 3)
    assert s.divergence_rate == pytest.approx(1 / 3)
    # the one divergence is recorded keyed by actual->would_pick
    assert s.divergences["mining->rest"] == 1
    assert rep.overall_agreement_rate == pytest.approx(2 / 3)


def test_recomputes_agreement_ignoring_stale_flag(tmp_path):
    # `agree` in the row is wrong; analyzer must recompute from the modes.
    rows = [
        {"policy": "llm", "would_pick": "combat", "actual_mode": "mining",
         "agree": True},  # lie: modes differ -> must count as divergence
    ]
    rep = analyze_shadow_log(_write_log(tmp_path, rows))
    s = rep.by_policy["llm"]
    assert s.agreements == 0
    assert s.divergences["mining->combat"] == 1


def test_policy_populations_are_separated(tmp_path):
    rows = [
        {"policy": "heuristic", "would_pick": "mining", "actual_mode": "mining"},
        {"policy": "llm", "would_pick": "rest", "actual_mode": "mining"},
        {"policy": "llm", "would_pick": "mining", "actual_mode": "mining"},
    ]
    rep = analyze_shadow_log(_write_log(tmp_path, rows))
    assert rep.by_policy["heuristic"].agreement_rate == pytest.approx(1.0)
    assert rep.by_policy["llm"].agreement_rate == pytest.approx(0.5)
    # overall pools all graded rows: 2 agree of 3
    assert rep.overall_agreement_rate == pytest.approx(2 / 3)


def test_malformed_and_missing_field_rows_are_counted_not_fatal(tmp_path):
    p = tmp_path / "meta_shadow.jsonl"
    with p.open("w") as f:
        f.write('{"policy": "heuristic", "would_pick": "mining", "actual_mode": "mining"}\n')
        f.write("not json at all\n")               # unparseable
        f.write("\n")                                # blank -> skipped, not counted
        f.write('{"policy": "heuristic", "would_pick": "rest"}\n')  # missing actual_mode
        f.write("[1, 2, 3]\n")                       # valid json, not a dict
    rep = analyze_shadow_log(p)
    # 4 non-blank lines counted as rows; 3 of them malformed
    assert rep.total_rows == 4
    assert rep.malformed_rows == 3
    assert rep.by_policy["heuristic"].rows == 1


def test_missing_policy_defaults_to_unknown(tmp_path):
    rows = [{"would_pick": "mining", "actual_mode": "rest"}]
    rep = analyze_shadow_log(_write_log(tmp_path, rows))
    assert "unknown" in rep.by_policy
    assert rep.by_policy["unknown"].rows == 1


def test_render_is_safe_on_empty_report():
    # render must not divide-by-zero on a no-rows report
    out = ShadowReport().render()
    assert "agreement" in out
