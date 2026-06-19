"""chronicle.post_cycle_note is best-effort: the in-world training-log post is
decorative and must NEVER raise into the orchestrator's per-cycle path.

Regression: text rendering, JSON serialization and request construction used to
run OUTSIDE the try/except, so a malformed cell or a non-numeric fitness (the
"improved" branch formats fitness with `:.0f`) propagated a TypeError straight
into the evolution run instead of being swallowed as a failed post.
"""
import urllib.request

import pytest

from foundry import chronicle


def _creds(monkeypatch):
    # Get past the early "no forum configured" return without touching config.
    monkeypatch.setattr(chronicle, "_config_key", lambda: ("https://x/api", "k"))


def test_bad_fitness_does_not_propagate(monkeypatch):
    _creds(monkeypatch)
    # urlopen must not even be reached — the failure happens while rendering text.
    def _boom(*a, **k):  # pragma: no cover - asserts it is never called
        raise AssertionError("network must not be attempted on a render failure")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    # object() breaks the `:.0f` format in the "improved" branch of cycle_text.
    assert chronicle.post_cycle_note((), "improved", object(), 1.0) is False


def test_bad_cell_does_not_propagate(monkeypatch):
    _creds(monkeypatch)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("unreachable")))
    # A non-iterable, truthy cell makes `cell[0]` raise inside cycle_text.
    assert chronicle.post_cycle_note(123, "filled", 5.0, None) is False


def test_network_error_returns_false(monkeypatch):
    _creds(monkeypatch)

    def _raise(*a, **k):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(urllib.request, "urlopen", _raise)

    # Valid inputs, but the round-trip fails: still best-effort False, no raise.
    assert chronicle.post_cycle_note(("COMBAT", 1), "improved", 9.0, 4.0) is False


def test_no_creds_returns_false(monkeypatch):
    monkeypatch.setattr(chronicle, "_config_key", lambda: None)
    assert chronicle.post_cycle_note(("MAGIC", 2), "filled", 3.0, None) is False


class _Resp:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_happy_path_returns_true(monkeypatch):
    _creds(monkeypatch)
    seen = {}

    def _ok(req, timeout=None):
        seen["url"] = req.full_url
        return _Resp()
    monkeypatch.setattr(urllib.request, "urlopen", _ok)

    assert chronicle.post_cycle_note(("GATHERING", 0), "filled", 7.0, None) is True
    assert seen["url"] == "https://x/api/agent/posts"


def test_improved_with_higher_point_fitness_narrates_a_gain():
    # Genuine numeric betterment: point fitness rose — the "bettered" phrasing
    # is correct and shows the arrow in the right direction.
    _title, body = chronicle.cycle_text(
        ("MAGIC", 1), "improved", fitness=200.0, prev_fitness=150.0)
    assert "bettered my own best (150 → 200)" in body


def test_improved_on_reliability_does_not_claim_a_numeric_increase():
    # The kernel promotes on the reliability bound (mean − λ·pstdev), so an
    # "improved" cycle can have a LOWER point fitness than the elite it displaced
    # (it won by being steadier). The note must NOT assert a betterment whose
    # numbers run backwards (the old "bettered my best (200 → 150)" bug).
    _title, body = chronicle.cycle_text(
        ("MAGIC", 1), "improved", fitness=150.0, prev_fitness=200.0)
    # Never an arrow that goes down while claiming a gain.
    assert "→ 150" not in body or "200 → 150" not in body
    assert "bettered my own best (200 → 150)" not in body
    # It still acknowledges the displaced score honestly...
    assert "200" in body
    # ...and frames the win as steadiness, not a higher number.
    assert "steadier" in body


def test_improved_equal_point_fitness_is_not_framed_as_a_gain():
    # Tie on point fitness (promoted purely on lower variance) — same rule:
    # do not claim the number went up.
    _title, body = chronicle.cycle_text(
        ("COMBAT", 0), "improved", fitness=100.0, prev_fitness=100.0)
    assert "bettered my own best" not in body
    assert "steadier" in body


def test_improved_subinteger_gain_not_framed_as_equal_arrow():
    # Point fitness genuinely ROSE (150.9 > 150.7) but both render "151" under
    # the sentence's :.0f formatting. The old `fitness > prev_fitness` guard took
    # the "bettered" branch and printed the self-contradicting "bettered my own
    # best (151 → 151)" — an arrow whose endpoints are equal. The note must never
    # claim a gain whose displayed numbers are identical: compare the ROUNDED
    # integers and fall through to the honest steadier framing instead.
    _title, body = chronicle.cycle_text(
        ("GATHERING", 1), "improved", fitness=150.9, prev_fitness=150.7)
    assert "151 → 151" not in body
    assert "bettered my own best" not in body
    assert "steadier" in body


@pytest.mark.parametrize("prev", [None, 0.0])
def test_improved_handles_missing_or_zero_prev(prev):
    # prev_fitness None (legacy/odd record) renders no gain clause; a real 0.0
    # prev with a positive new fitness is a genuine rise.
    _title, body = chronicle.cycle_text(("CRAFTING", 2), "improved", 12.0, prev)
    if prev is None:
        assert "bettered" not in body and "steadier" not in body
    else:
        assert "bettered my own best (0 → 12)" in body
