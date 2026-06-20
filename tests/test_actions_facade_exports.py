"""The anima.actions package documents itself as the SINGLE IMPORT FAÇADE
over both primitive hierarchies (anima.action + anima.actions), and its
``__all__`` promises every listed name is importable as
``from anima.actions import X``.

Two regressions this guards:
  1. Every name in ``__all__`` must actually resolve via the PEP 562
     ``__getattr__`` — a stale/typo'd module path would make the documented
     import raise AttributeError only at call time (the same failure class as
     the shipped ``select_context_menu_entry`` import fix).
  2. The loot primitives (``loot_corpse``/``find_corpses``) and
     ``equip_shield_from_pack`` are real public actions used across
     combat_loop / planner / melee, yet were absent from the façade, so the
     documented ``from anima.actions import loot_corpse`` raised AttributeError.
"""

from __future__ import annotations

import importlib

import pytest

import anima.actions as actions


def test_every_all_name_resolves_through_the_facade() -> None:
    """No promised export may be unreachable via the façade __getattr__."""
    unresolved: list[tuple[str, str]] = []
    for name in actions.__all__:
        try:
            getattr(actions, name)
        except AttributeError as exc:  # pragma: no cover - failure path
            unresolved.append((name, str(exc)))
    assert not unresolved, f"façade __all__ names that do not resolve: {unresolved}"


@pytest.mark.parametrize(
    ("name", "module"),
    [
        ("loot_corpse", "anima.actions.loot"),
        ("find_corpses", "anima.actions.loot"),
        ("equip_shield_from_pack", "anima.actions.equip"),
    ],
)
def test_loot_and_shield_primitives_are_reexported(name: str, module: str) -> None:
    """These primitives must be importable from the package façade and be the
    *same* object the defining submodule exposes (not a shadow/duplicate)."""
    # Documented usage pattern — must not raise.
    via_facade = getattr(actions, name)
    via_module = getattr(importlib.import_module(module), name)
    assert via_facade is via_module
    assert via_facade.__module__ == module


def test_unknown_attribute_still_raises_attribute_error() -> None:
    """The façade must not start resolving arbitrary names after the fix."""
    with pytest.raises(AttributeError):
        actions.definitely_not_a_real_primitive  # noqa: B018


def test_from_import_syntax_works_for_newly_exported_names() -> None:
    """Exercise the literal ``from anima.actions import X`` form the package
    docstring advertises for the previously-missing names."""
    from anima.actions import (  # noqa: F401
        equip_shield_from_pack,
        find_corpses,
        loot_corpse,
    )

    assert callable(loot_corpse)
    assert callable(find_corpses)
    assert callable(equip_shield_from_pack)
