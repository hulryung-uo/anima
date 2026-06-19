"""A transient downward HP dip DURING recovery must not re-open a resolved death.

``analyze_deaths`` closes a death at a SUSTAINED resurrection (hits>0 held for
>= min_death_s). To measure that hold it scanned for the next ``hits<=0`` sample
as the "relapse" — but a 1-second stale-vitals dip to 0 that itself recovers is,
by MIN_DEATH_S's own definition, NOT a ghost death. Using it as the relapse
truncated the post-resurrection alive-run, rejected the genuine sustained
resurrection as a "flicker", and kept the long-resolved death OPEN — inflating
``dead_s`` and (when the dip straddled the grace boundary) fabricating a shadow
GM intervention out of a clean self-rescue, corrupting the one autonomy metric
this module exists to measure. The symmetric counterpart of bd75575 (the upward
mid-death flicker double-count guard).
"""
from foundry.apprentice import analyze_deaths, build_report
from foundry.kernel.trajectory import TrajectorySummary


def _summary(hp, start=1000.0, end=2800.0):  # 1800s window
    return TrajectorySummary(start_ts=start, end_ts=end, hp_samples=hp)


def test_transient_downward_dip_during_recovery_does_not_inflate_dead_s():
    # Death at 1100. Sustained resurrection at 1200 (dead 100s, within 180s
    # grace → self-rescued). A 1s stale-packet dip to 0 at 1201 recovers at 1202
    # and stays alive to the window end; it is NOT a ghost death (MIN_DEATH_S),
    # so it must not keep the resolved death open.
    s = _summary([(1000, 50, 50), (1100, 0, 50),
                  (1200, 40, 50), (1201, 0, 71), (1202, 45, 50)])
    ev = analyze_deaths(s, grace_s=180)
    assert len(ev) == 1
    assert ev[0].dead_s == 100          # 1200 - 1100, NOT 1202 - 1100 (=102)
    assert ev[0].self_rescued is True
    assert ev[0].needed_intervention is False


def test_recovery_dip_does_not_flip_intervention():
    # Death at 1100, sustained resurrection at 1279 (dead 179s, just inside the
    # 180s grace). A 1s downward flicker at 1280 must NOT push dead_s to 181s
    # (which would fabricate a shadow intervention out of a self-rescue).
    s = _summary([(1000, 50, 50), (1100, 0, 50),
                  (1279, 40, 50), (1280, 0, 50), (1281, 45, 50)])
    r = build_report(s, "adventurer", "warrior", grace_s=180)
    assert r.deaths == 1
    assert r.shadow_interventions == 0
    assert r.self_rescue_rate == 1.0


def test_sustained_relapse_still_a_second_death():
    # Control: resurrection at 1200, then a genuine SUSTAINED second death at
    # 2000 (dead to window end). Two distinct deaths, behaviour unchanged.
    s = _summary([(1000, 50, 50), (1100, 0, 50), (1200, 40, 50), (2000, 0, 50)])
    ev = analyze_deaths(s, grace_s=180)
    assert len(ev) == 2
    assert ev[0].dead_s == 100
