"""foundry.score batch mode must survive a single malformed trajectory.

Regression: ``main`` caught only ``FileNotFoundError``. The tool's documented
batch form (``python3 -m foundry.score *.jsonl``) can hand it a path that exists
but is not a scoreable trajectory — a stray directory the glob matched
(``IsADirectoryError``) or a truncated/half-written JSONL whose parse raises.
Any non-FNF exception propagated out of the loop and aborted the batch, silently
dropping every file queued AFTER the bad one. The fix catches the per-file
failure, reports it, and continues — so a later valid trajectory is still scored.
"""
from __future__ import annotations

from foundry import score


def test_non_fnf_error_does_not_abort_the_batch(monkeypatch, capsys):
    scored: list[str] = []

    def _fake_score_one(path: str) -> None:
        if path == "bad":
            raise IsADirectoryError("[Errno 21] Is a directory: 'bad'")
        if path == "boom":
            raise ValueError("truncated JSONL")
        scored.append(path)

    monkeypatch.setattr(score, "score_one", _fake_score_one)

    rc = score.main(["good1", "bad", "boom", "good2"])

    # Every GOOD file after the failing ones still gets scored.
    assert scored == ["good1", "good2"]
    assert rc == 0
    out = capsys.readouterr().out
    # Both failures are reported per-file (not swallowed silently, not raised).
    assert "bad" in out and "IsADirectoryError" in out
    assert "boom" in out and "ValueError" in out


def test_missing_file_keeps_its_specific_message(monkeypatch, capsys):
    # FileNotFoundError retains its dedicated "(not found: ...)" wording rather
    # than the generic error line.
    def _fake_score_one(path: str) -> None:
        raise FileNotFoundError(path)

    monkeypatch.setattr(score, "score_one", _fake_score_one)
    rc = score.main(["nope.jsonl"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(not found: nope.jsonl)" in out
    # The generic handler's prefix must NOT also fire for a plain FNF.
    assert "error scoring nope.jsonl" not in out


def test_all_good_files_score_and_return_zero(monkeypatch, capsys):
    scored: list[str] = []
    monkeypatch.setattr(score, "score_one", lambda p: scored.append(p))
    rc = score.main(["a.jsonl", "b.jsonl"])
    assert rc == 0
    assert scored == ["a.jsonl", "b.jsonl"]


def test_empty_argv_returns_usage_code(capsys):
    rc = score.main([])
    assert rc == 2
    assert "usage:" in capsys.readouterr().out
