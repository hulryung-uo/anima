"""Apprentice soak analyzer (A1) — pure-function tests over synthetic
hp_samples timelines (no shard). Validates death/recovery reconstruction and
the autonomy metrics that measure Rank 6 (flee/recover) over a long horizon.
"""
from foundry.apprentice import (
    analyze_deaths,
    build_report,
    _longest_alive_stretch,
    _longest_sample_gap,
)
from foundry.kernel.trajectory import TrajectorySummary


def _summary(hp_samples, start=1000.0, end=2800.0):  # 1800s = 0.5h window
    return TrajectorySummary(start_ts=start, end_ts=end, hp_samples=hp_samples)


class TestAnalyzeDeaths:
    def test_no_death(self):
        s = _summary([(1000, 50, 50), (1500, 30, 50), (2800, 45, 50)])
        assert analyze_deaths(s) == []

    def test_transient_hp0_blip_not_counted(self):
        # hits 0 then 1 one second later → transient reading, not a ghost death
        # (a real death stays a ghost until a healer resurrection, many seconds).
        s = _summary([(1000, 50, 50), (1542, 0, 71), (1543, 1, 71)])
        assert analyze_deaths(s) == []

    def test_self_rescued_within_grace(self):
        s = _summary([(1000, 50, 50), (1100, 0, 50), (1200, 40, 50)])  # dead 100s
        ev = analyze_deaths(s, grace_s=180)
        assert len(ev) == 1
        assert ev[0].self_rescued is True
        assert ev[0].needed_intervention is False
        assert ev[0].dead_s == 100

    def test_needs_intervention_when_dead_past_grace(self):
        s = _summary([(1000, 50, 50), (1100, 0, 50), (1400, 40, 50)])  # dead 300s
        ev = analyze_deaths(s, grace_s=180)
        assert ev[0].self_rescued is False
        assert ev[0].needed_intervention is True

    def test_never_revived_runs_to_window_end(self):
        s = _summary([(1000, 50, 50), (1100, 0, 50)])  # dead until end (1700s)
        ev = analyze_deaths(s, grace_s=180)
        assert len(ev) == 1
        assert ev[0].revived_ts is None
        assert ev[0].needed_intervention is True
        assert ev[0].dead_s == 1700

    def test_multiple_deaths(self):
        s = _summary([(1000, 50, 50), (1100, 0, 50), (1150, 40, 50),   # rescued (50s)
                      (2000, 0, 50), (2400, 30, 50)])                   # intervention (400s)
        ev = analyze_deaths(s, grace_s=180)
        assert len(ev) == 2
        assert ev[0].self_rescued and not ev[1].self_rescued


class TestLongestAliveStretch:
    def test_picks_longer_segment_around_death(self):
        # alive 1000-1100 (100s), dead, alive 1200-2800 (1600s) → 1600
        s = _summary([(1000, 50, 50), (1100, 0, 50), (1200, 40, 50)])
        assert _longest_alive_stretch(s) == 1600

    def test_no_samples_assumes_full_duration(self):
        assert _longest_alive_stretch(_summary([])) == 1800


class TestLongestSampleGap:
    def test_gap_between_samples(self):
        # samples are dense at the edges; the silence is in the middle (600s).
        s = _summary([(1000, 50, 50), (1100, 50, 50),
                      (1700, 50, 50), (2800, 50, 50)])
        # interior gaps: 100, 600, 1100; edges flush → 1100 still the max here,
        # but the point is the middle gap is seen.
        assert _longest_sample_gap(s) == 1100

    def test_trailing_silence_counts(self):
        # agent reports in only near the start, then goes silent until the
        # window closes — a stall the old (inter-sample-only) metric missed.
        s = _summary([(1000, 50, 50), (1050, 50, 50)])  # last sample @1050
        # window ends at 2800 → 1750s of trailing dead air dominates.
        assert _longest_sample_gap(s) == 1750

    def test_leading_silence_counts(self):
        # connects but sends nothing until late in the window.
        s = _summary([(2700, 50, 50), (2800, 50, 50)])  # first sample @2700
        assert _longest_sample_gap(s) == 1700  # 1000..2700 leading dead air

    def test_no_samples_is_full_window(self):
        # never reported vitals/position at all → the whole window is a gap,
        # not 0.0 ("looks fine") as the old code claimed.
        assert _longest_sample_gap(_summary([])) == 1800

    def test_single_sample_brackets_both_edges(self):
        # one mid-window sample: max(leading, trailing) dead air, not 0.0.
        s = _summary([(1500, 50, 50)])  # 500s before, 1300s after
        assert _longest_sample_gap(s) == 1300

    def test_out_of_window_sample_is_clamped(self):
        # a stray packet past window end must not invent a negative/huge gap.
        s = _summary([(1000, 50, 50), (5000, 50, 50)])
        assert _longest_sample_gap(s) == 1800  # clamps to [1000, 2800]

    def test_uses_positions_as_well_as_hp(self):
        s = TrajectorySummary(start_ts=1000.0, end_ts=2800.0,
                              hp_samples=[(1000, 50, 50)],
                              positions=[(2790, 5, 5)])
        # liveness from a position sample keeps the trailing gap small.
        assert _longest_sample_gap(s) == 1790  # 1000->2790, then 10 to end


class TestBuildReport:
    def test_autonomy_metrics(self):
        s = _summary([(1000, 50, 50), (1100, 0, 50), (1150, 40, 50),
                      (2000, 0, 50), (2400, 30, 50)])
        r = build_report(s, "adventurer", "warrior", grace_s=180)
        assert r.deaths == 2
        assert r.self_rescued == 1
        assert r.shadow_interventions == 1
        assert r.self_rescue_rate == 0.5
        assert r.interventions_per_hour == 2.0   # 1 intervention / 0.5h
        assert r.duration_h == 0.5

    def test_clean_run_reports_full_autonomy(self):
        s = _summary([(1000, 50, 50), (2800, 50, 50)])
        r = build_report(s, "mage", "mage")
        assert r.deaths == 0
        assert r.shadow_interventions == 0
        assert r.self_rescue_rate == 1.0
        assert r.interventions_per_hour == 0.0
