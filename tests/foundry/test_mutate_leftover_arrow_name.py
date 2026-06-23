"""The leftover fold must not mistake an arrow-in-name file for a rename.

Git ALWAYS quotes a path containing the substring ``" -> "`` (it has spaces), so
an untracked / modified genome file literally named ``anima/a -> b.py`` is
emitted by ``status --porcelain`` as ``?? "anima/a -> b.py"`` — an UNTRACKED
entry, not a rename. ``_genome_leftovers`` used to detect renames with a bare
``" -> " in path`` substring test, which fired on that embedded arrow and split
the quoted name into ``b.py"`` (failing the ``anima/`` prefix test). The genome
file was then dropped from the fold: ``mutate_with_claude`` sees
``after == before``, reports ``changed=False``, and the whole eval window is
wasted on a "no variant" cycle — the same class the quotepath / special-char
fixes close for every other name, leaking through rename detection.

The fix detects renames by the porcelain STATUS CODE (``R``/``C``), not the
substring, and splits on the FIRST separator so a real rename whose NEW path is
itself quoted (and contains an arrow) still recovers the new path.
"""
import subprocess

import pytest

from foundry.mutate import _genome_leftovers


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


def test_untracked_arrow_named_anima_file_is_not_treated_as_rename(repo):
    # An untracked genome file whose name contains " -> ". git quotes it
    # (`?? "anima/a -> b.py"`); the old substring test mis-split it on the arrow.
    (repo / "anima" / "a -> b.py").write_text("x = 1\n")
    assert _genome_leftovers(repo) == ["anima/a -> b.py"]


def test_modified_tracked_arrow_named_anima_file_is_genome_content(repo):
    name = "anima/x -> y.py"
    (repo / name).write_text("v = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add arrow")
    (repo / name).write_text("v = 2\n")  # tracked modification, quoted path
    assert _genome_leftovers(repo) == [name]


def test_real_rename_to_arrow_named_anima_file_uses_the_new_path(repo):
    # A genuine rename whose NEW path contains " -> " (and so is quoted). The
    # status code is R, so the rename split applies and the new path wins.
    old = repo / "anima" / "plain.py"
    old.write_text("v = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add plain")
    _git(repo, "mv", "anima/plain.py", "anima/p -> q.py")
    assert _genome_leftovers(repo) == ["anima/p -> q.py"]


def test_arrow_named_junk_outside_anima_is_still_excluded(repo):
    (repo / "data").mkdir()
    (repo / "data" / "a -> b.json").write_text("{}\n")
    assert _genome_leftovers(repo) == []
