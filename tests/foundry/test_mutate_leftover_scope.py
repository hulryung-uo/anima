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
    # Tracked files OUTSIDE the genome body that the mutator must NOT carry into
    # a variant: a persona template and a piece of non-kernel Foundry machinery
    # (safety.revert_kernel only restores foundry/kernel, never foundry/select.py).
    (tmp_path / "personas").mkdir()
    (tmp_path / "personas" / "miner.yaml").write_text("name: miner\n")
    (tmp_path / "foundry").mkdir()
    (tmp_path / "foundry" / "select.py").write_text("PARENT_BIAS = 1.0\n")
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


def test_modified_tracked_file_outside_anima_is_not_genome_content(repo):
    # The mutator is told to edit ONLY anima/, but headless claude CAN edit other
    # tracked files. A persona template edit and -- critically -- a non-kernel
    # Foundry-machinery edit (which safety.revert_kernel does NOT undo) must not
    # be folded into the variant's genome commit / code_ref, or the change rides
    # into the genome's lineage and every descendant worktree.
    (repo / "personas" / "miner.yaml").write_text("name: miner\nhacked: true\n")
    (repo / "foundry" / "select.py").write_text("PARENT_BIAS = 99.0  # self-edit\n")
    assert _genome_leftovers(repo) == []


def test_only_the_anima_part_of_a_mixed_change_is_folded(repo):
    # Mutator legitimately edits anima/ AND incidentally touches a tracked file
    # outside it: only the anima/ path counts as genome content.
    (repo / "anima" / "keep.py").write_text("base = 2\n")
    (repo / "foundry" / "select.py").write_text("PARENT_BIAS = 2.0\n")
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
