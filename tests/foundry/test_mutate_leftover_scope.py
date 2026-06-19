"""The leftover fold must only commit the mutator's OWN genome work.

A reused worktree slot keeps preserved untracked junk (``_prepare_worktree``
runs ``git clean -e data``), so a prior cycle's untracked ``data/*.json`` files
survive into the next cycle. If claude edits nothing this cycle (timed out /
errored), folding that junk as a "leftover" used to advance HEAD and report the
cycle as a real variant — burning an eval window on parent-identical code and
planting a placeholder-labelled near-duplicate genome.

``_genome_leftovers`` discriminates real mutation content (tracked changes, or
untracked files under ``anima/``) from preserved non-anima junk.
"""
import subprocess

import pytest

from foundry.mutate import _genome_leftovers, _commit_all, head


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "anima").mkdir()
    (tmp_path / "anima" / "keep.py").write_text("base = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_preserved_non_anima_junk_is_not_genome_content(repo):
    # prior-cycle untracked junk preserved by `git clean -e data`
    (repo / "data").mkdir()
    (repo / "data" / "cliloc.kor.json").write_text("{}\n")
    assert _genome_leftovers(repo) == []


def test_untracked_anima_file_is_genome_content(repo):
    (repo / "anima" / "new.py").write_text("x = 1\n")
    assert _genome_leftovers(repo) == ["anima/new.py"]


def test_modified_tracked_file_is_genome_content(repo):
    (repo / "anima" / "keep.py").write_text("base = 2\n")
    assert _genome_leftovers(repo) == ["anima/keep.py"]


def test_noop_mutation_with_only_junk_does_not_fabricate_a_commit(repo):
    # Simulate the leftover-fold branch of mutate_with_claude directly: claude
    # changed nothing, but the reused slot carries preserved untracked junk.
    before = head(repo)
    (repo / "data").mkdir()
    (repo / "data" / "cliloc.kor.json").write_text("{}\n")

    leftovers = _genome_leftovers(repo)
    if leftovers:
        _git(repo, "add", "--", *leftovers)
        _git(repo, "commit", "-m", "foundry-mutation: (auto-commit leftover changes)")

    # No genome content → no commit → cycle correctly reports "no variant".
    assert head(repo) == before


def test_real_genome_leftover_is_committed(repo):
    before = head(repo)
    (repo / "anima" / "new.py").write_text("x = 1\n")
    (repo / "data").mkdir()
    (repo / "data" / "cliloc.kor.json").write_text("{}\n")  # incidental junk

    leftovers = _genome_leftovers(repo)
    assert leftovers  # the anima file is real work
    _git(repo, "add", "--", *leftovers)
    _git(repo, "commit", "-m", "foundry-mutation: (auto-commit leftover changes)")

    after = head(repo)
    assert after != before
    # the junk was NOT dragged into the variant commit
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(repo),
        capture_output=True, text=True).stdout.split()
    assert "anima/new.py" in tracked
    assert "data/cliloc.kor.json" not in tracked
