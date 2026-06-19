"""The orchestrator's kernel-restore commit must NOT carry non-genome edits.

Integrity invariant (FOUNDRY.md §2 + commits 69bca90/a06ca63): a genome's body
is ONLY ``anima/``. ``_genome_leftovers`` deliberately leaves uncommitted edits
to non-genome paths (``personas/*.yaml``, non-kernel Foundry machinery like
``foundry/select.py`` that ``safety.revert_kernel`` does NOT undo) OUT of the
variant commit so they can't ride into the genome's ``code_ref`` and every
descendant worktree.

But after a mutator that ALSO edited the kernel, the orchestrator runs
``revert_kernel`` then commits the revert. It used ``_commit_all`` (``git add
-A``), which staged EVERY uncommitted change — re-sweeping exactly those
non-genome leftovers into the genome's "restore pinned kernel" commit. The
scoped ``_commit_paths`` (foundry/kernel + anima/ only) closes that leak.
"""
import subprocess

import pytest

from foundry.mutate import _commit_paths, _commit_all, head


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), check=True,
                          capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "anima").mkdir()
    (tmp_path / "anima" / "keep.py").write_text("base = 1\n")
    (tmp_path / "foundry" / "kernel").mkdir(parents=True)
    (tmp_path / "foundry" / "kernel" / "fitness.py").write_text("RULER = 1.0\n")
    # Non-kernel Foundry machinery + a persona template: NON-genome paths that
    # revert_kernel never touches and that must stay out of the genome commit.
    (tmp_path / "foundry" / "select.py").write_text("PARENT_BIAS = 1.0\n")
    (tmp_path / "personas").mkdir()
    (tmp_path / "personas" / "miner.yaml").write_text("name: miner\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _tracked_at_head(repo, path):
    """The committed content of ``path`` at HEAD (or None if absent)."""
    r = subprocess.run(["git", "show", f"HEAD:{path}"], cwd=str(repo),
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def test_commit_paths_stages_only_the_given_pathspecs(repo):
    before = head(repo)
    # Edits across genome + non-genome paths, all uncommitted.
    (repo / "anima" / "keep.py").write_text("base = 2\n")
    (repo / "foundry" / "select.py").write_text("PARENT_BIAS = 99.0\n")
    (repo / "personas" / "miner.yaml").write_text("name: hacked\n")

    committed, _ = _commit_paths(repo, "scoped", "anima/")
    assert committed
    assert head(repo) != before
    # anima/ change landed; the non-genome edits did NOT.
    assert _tracked_at_head(repo, "anima/keep.py") == "base = 2\n"
    assert _tracked_at_head(repo, "foundry/select.py") == "PARENT_BIAS = 1.0\n"
    assert _tracked_at_head(repo, "personas/miner.yaml") == "name: miner\n"
    # They are still present as uncommitted working-tree changes.
    porcelain = _git(repo, "status", "--porcelain").stdout
    assert "foundry/select.py" in porcelain
    assert "personas/miner.yaml" in porcelain


def test_commit_paths_noops_when_targeted_paths_are_clean(repo):
    before = head(repo)
    # Only a NON-targeted path is dirty → nothing to commit for "anima/".
    (repo / "foundry" / "select.py").write_text("PARENT_BIAS = 5.0\n")
    committed, sha = _commit_paths(repo, "scoped", "anima/")
    assert not committed
    assert sha == before


def test_kernel_restore_does_not_carry_non_genome_leftovers(repo):
    """The full orchestrator scenario, reproduced with git plumbing.

    A mutator (1) committed an anima/ change AND a kernel cheat, and (2) left a
    non-genome edit (foundry/select.py) UNCOMMITTED. The orchestrator reverts the
    kernel and commits the revert. The committed genome must contain the pinned
    kernel + the anima/ work, but NEVER the uncommitted foundry/select.py edit.
    """
    pinned = head(repo)  # the clean, pinned kernel

    # (1) mutator's own commit: genome edit + kernel cheat.
    (repo / "anima" / "keep.py").write_text("base = 2  # mutation\n")
    (repo / "foundry" / "kernel" / "fitness.py").write_text("RULER = 999.0  # cheat\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "foundry-mutation: tweak + (sneaky) ruler")

    # (2) uncommitted non-genome leftover (revert_kernel won't undo this).
    (repo / "foundry" / "select.py").write_text("PARENT_BIAS = 99.0  # self-edit\n")

    # orchestrator: revert kernel to the pin (stages the kernel files).
    _git(repo, "checkout", pinned, "--", "foundry/kernel")

    # SCOPED restore commit (the fix) — kernel + genome body only.
    _commit_paths(repo, "foundry: restore pinned kernel", "foundry/kernel", "anima/")

    # Kernel is back to the pinned ruler...
    assert _tracked_at_head(repo, "foundry/kernel/fitness.py") == "RULER = 1.0\n"
    # ...the genome work survives...
    assert _tracked_at_head(repo, "anima/keep.py") == "base = 2  # mutation\n"
    # ...and the non-genome edit was NOT folded into the genome's code_ref.
    assert _tracked_at_head(repo, "foundry/select.py") == "PARENT_BIAS = 1.0\n"
    assert "foundry/select.py" in _git(repo, "status", "--porcelain").stdout


def test_commit_all_would_have_leaked_the_non_genome_edit(repo):
    """Characterize the pre-fix behaviour: `git add -A` DOES sweep the
    non-genome leftover into the restore commit (the bug this fix removes)."""
    pinned = head(repo)
    (repo / "foundry" / "kernel" / "fitness.py").write_text("RULER = 999.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "foundry-mutation: cheat")
    (repo / "foundry" / "select.py").write_text("PARENT_BIAS = 99.0\n")
    _git(repo, "checkout", pinned, "--", "foundry/kernel")

    _commit_all(repo, "foundry: restore pinned kernel")
    # The unscoped add -A leaked the non-genome edit into the genome commit.
    assert _tracked_at_head(repo, "foundry/select.py") == "PARENT_BIAS = 99.0\n"
