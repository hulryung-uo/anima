"""The leftover fold must see genome files with NON-ASCII names.

Git's default porcelain output C-quotes any path containing non-ASCII bytes
(``core.quotepath`` is true by default): ``anima/föö.py`` is emitted as
``"anima/f\303\266\303\266.py"`` — a leading double-quote plus octal escapes.
``_genome_leftovers`` parses ``status --porcelain`` line-by-line and keeps a
path only when it ``startswith("anima/")``, so a quoted path (leading ``"``)
was silently skipped. A mutation whose only edit is such a file then folds to
nothing: ``mutate_with_claude`` sees ``after == before``, reports
``changed=False``, and the whole eval window is wasted on a "no variant" cycle.

The fix passes ``-c core.quotepath=false`` so paths are raw UTF-8 and the
``anima/`` prefix test matches.
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


def test_untracked_non_ascii_anima_file_is_genome_content(repo):
    # An untracked genome file whose name has non-ASCII bytes — git would
    # C-quote it in default porcelain output, hiding it from the anima/ prefix.
    (repo / "anima" / "crafté.py").write_text("x = 1\n")
    assert _genome_leftovers(repo) == ["anima/crafté.py"]


def test_modified_tracked_non_ascii_anima_file_is_genome_content(repo):
    name = "anima/föö.py"
    (repo / name).write_text("v = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add fö")
    (repo / name).write_text("v = 2\n")  # tracked modification, quoted path
    assert _genome_leftovers(repo) == [name]


def test_non_ascii_junk_outside_anima_is_still_excluded(repo):
    # The quotepath fix must not start folding non-genome junk: a non-ASCII
    # name OUTSIDE anima/ is still not genome content.
    (repo / "data").mkdir()
    (repo / "data" / "clilöc.json").write_text("{}\n")
    assert _genome_leftovers(repo) == []
