"""The leftover fold must see genome files whose names contain SPECIAL chars.

``-c core.quotepath=false`` only suppresses NON-ASCII byte escaping; git still
wraps a path in double-quotes (with C escapes) when it contains a space, tab,
quote, or backslash. ``anima/new feature.py`` is emitted as
``"anima/new feature.py"`` and ``anima/with\\ttab.py`` as ``"anima/with\\ttab.py"``.
The bare ``path.startswith("anima/")`` test then fails on the leading ``"``, so a
mutation whose only edit is such a genome file is dropped from the fold:
``mutate_with_claude`` sees ``after == before``, reports ``changed=False``, and
the whole eval window is wasted on a "no variant" cycle — the same class
commit 55d0273 fixed for non-ASCII names but never closed for special chars.

The fix unquotes git's C-quoted porcelain paths before the anima/ prefix test.
"""
import subprocess

import pytest

from foundry.mutate import _genome_leftovers, _git_unquote


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


def test_unquote_roundtrips_special_and_plain_paths():
    assert _git_unquote("anima/plain.py") == "anima/plain.py"
    assert _git_unquote('"anima/has space.py"') == "anima/has space.py"
    assert _git_unquote('"anima/with\\ttab.py"') == "anima/with\ttab.py"
    # quotepath=false keeps UTF-8 raw inside the quotes (space forces quoting):
    assert _git_unquote('"anima/crafté x.py"') == "anima/crafté x.py"


def test_untracked_space_named_anima_file_is_genome_content(repo):
    # git quotes this path (space) even under quotepath=false → leading '"'.
    (repo / "anima" / "new feature.py").write_text("x = 1\n")
    assert _genome_leftovers(repo) == ["anima/new feature.py"]


def test_modified_tracked_space_named_anima_file_is_genome_content(repo):
    name = "anima/big change.py"
    (repo / name).write_text("v = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add")
    (repo / name).write_text("v = 2\n")  # tracked modification, quoted path
    assert _genome_leftovers(repo) == [name]


def test_renamed_space_named_anima_file_uses_the_new_path(repo):
    old = repo / "anima" / "old name.py"
    old.write_text("v = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add")
    _git(repo, "mv", "anima/old name.py", "anima/new name.py")
    # The " -> " separator is unquoted; the new (quoted) path is unwrapped.
    assert _genome_leftovers(repo) == ["anima/new name.py"]


def test_space_named_junk_outside_anima_is_still_excluded(repo):
    (repo / "data").mkdir()
    (repo / "data" / "cli loc.json").write_text("{}\n")
    assert _genome_leftovers(repo) == []
