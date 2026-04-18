"""Tests for tools/diagnose.py constraint detection.

Focus: DEADLOCK must not fire when the agent has bank gold — vendor
purchases debit the bank, not the backpack, so a fresh non-trivial
bank_balance means the buy-tools path can still make progress.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))


def _write_state(tmp_path: Path, *, gold: int, bank: dict | None,
                 inv: list[dict], age_s: float = 0.0) -> Path:
    state_file = tmp_path / "data" / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({
        "ts": time.time() - age_s,
        "status": {
            "gold": gold,
            "bank_balance": bank,
            "weight": 50,
            "weight_max": 400,
            "x": 2503, "y": 559, "z": 0,
        },
        "inventory": inv,
    }))
    return state_file


@pytest.fixture
def patched_diagnose(tmp_path, monkeypatch):
    import diagnose
    state_file = tmp_path / "data" / "state.json"
    monkeypatch.setattr(diagnose, "STATE_FILE", state_file)
    return diagnose, state_file


def test_deadlock_suppressed_when_bank_gold_is_fresh(patched_diagnose, tmp_path):
    diagnose, _ = patched_diagnose
    _write_state(
        tmp_path,
        gold=0,
        bank={"amount": 25500, "ts": time.time() - 30},
        inv=[{"name": "dagger"}],
    )

    constraints = diagnose._detect_constraints()

    assert "DEADLOCK" not in constraints
    assert "no_gold" not in constraints


def test_deadlock_fires_when_bank_balance_missing(patched_diagnose, tmp_path):
    diagnose, _ = patched_diagnose
    _write_state(
        tmp_path,
        gold=0,
        bank=None,
        inv=[{"name": "dagger"}],
    )

    constraints = diagnose._detect_constraints()

    assert constraints.get("no_gold")
    assert constraints.get("DEADLOCK")


def test_deadlock_fires_when_bank_balance_is_stale(patched_diagnose, tmp_path):
    diagnose, _ = patched_diagnose
    _write_state(
        tmp_path,
        gold=0,
        bank={"amount": 25500, "ts": time.time() - 1200},
        inv=[{"name": "dagger"}],
    )

    constraints = diagnose._detect_constraints()

    assert constraints.get("no_gold")
    assert constraints.get("DEADLOCK")


def test_deadlock_fires_when_bank_balance_is_too_small(patched_diagnose, tmp_path):
    diagnose, _ = patched_diagnose
    _write_state(
        tmp_path,
        gold=0,
        bank={"amount": 3, "ts": time.time() - 10},
        inv=[{"name": "dagger"}],
    )

    constraints = diagnose._detect_constraints()

    assert constraints.get("no_gold")
    assert constraints.get("DEADLOCK")
