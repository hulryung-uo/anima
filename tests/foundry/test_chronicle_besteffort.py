"""chronicle.post_cycle_note is best-effort: the in-world training-log post is
decorative and must NEVER raise into the orchestrator's per-cycle path.

Regression: text rendering, JSON serialization and request construction used to
run OUTSIDE the try/except, so a malformed cell or a non-numeric fitness (the
"improved" branch formats fitness with `:.0f`) propagated a TypeError straight
into the evolution run instead of being swallowed as a failed post.
"""
import urllib.request

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
