"""The leftover auto-commit must never clobber the mutator's real hypothesis.

When the headless mutator commits its variant but leaves stray uncommitted
files, ``mutate_with_claude`` folds those into a second, *bookkeeping* commit
whose subject is the placeholder. Reading only HEAD's subject (the old
behaviour) recorded that placeholder as the genome's hypothesis and lost the
agent's real one. ``_extract_hypothesis`` must scan ``before..after`` and pick
the genuine ``foundry-mutation:`` line instead.
"""
import subprocess

import pytest

from foundry.mutate import (
    _LEFTOVER_COMMIT_SUBJECT,
    _extract_hypothesis,
    _commit_all,
    head,
)


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "base")
    return tmp_path


def _write(repo, name, text):
    (repo / name).write_text(text)


def test_real_hypothesis_survives_leftover_autocommit(repo):
    before = head(repo)

    # agent makes its proper variant commit
    _write(repo, "anima_change.py", "x = 1\n")
    _commit_all(repo, "foundry-mutation: raise mining loop throughput")

    # agent left a stray file uncommitted → folded into placeholder commit,
    # which is now HEAD (the failure trigger).
    _write(repo, "leftover.txt", "stray\n")
    _commit_all(repo, _LEFTOVER_COMMIT_SUBJECT)
    after = head(repo)
    assert after != before

    # HEAD subject is the placeholder — the old code would have returned its body.
    head_subj = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"], cwd=str(repo),
        capture_output=True, text=True).stdout.strip()
    assert head_subj == _LEFTOVER_COMMIT_SUBJECT

    hyp = _extract_hypothesis(repo, before, after)
    assert hyp == "raise mining loop throughput"
    assert "auto-commit" not in hyp


def test_single_clean_commit_extracts_body(repo):
    before = head(repo)
    _write(repo, "anima_change.py", "x = 1\n")
    _commit_all(repo, "foundry-mutation: pin Blacksmith at birth")
    after = head(repo)
    assert _extract_hypothesis(repo, before, after) == "pin Blacksmith at birth"


def test_untagged_commit_falls_back_to_subject(repo):
    before = head(repo)
    _write(repo, "anima_change.py", "x = 1\n")
    _commit_all(repo, "tweak movement throttle")  # older lineage, no tag
    after = head(repo)
    assert _extract_hypothesis(repo, before, after) == "tweak movement throttle"


def test_only_leftover_commit_does_not_return_placeholder_body(repo):
    # Degenerate: the only commit is the placeholder (agent committed nothing of
    # its own but left stray files). We must not surface the placeholder body.
    before = head(repo)
    _write(repo, "leftover.txt", "stray\n")
    _commit_all(repo, _LEFTOVER_COMMIT_SUBJECT)
    after = head(repo)
    assert after != before
    hyp = _extract_hypothesis(repo, before, after)
    assert hyp == _LEFTOVER_COMMIT_SUBJECT  # whole subject, never the body
    assert "auto-commit" in hyp  # honest about what happened, not a fake hypothesis
