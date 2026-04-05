"""Tests for supervisor — fix attempt persistence and progressive timeout."""
import json
from unittest.mock import patch

import pytest

_REASON = "missing_resource (fail rate 100%)"


@pytest.fixture
def tmp_improvements(tmp_path):
    """Create a temp improvements.jsonl file."""
    return tmp_path / "improvements.jsonl"


def _targeted(proc: str, reason: str = _REASON,
              success: bool = False, changed: bool = False) -> dict:
    return {
        "action": f"targeted_fix:{proc}",
        "reason": reason,
        "success": success,
        "code_changed": changed,
    }


class TestLoadFixAttempts:
    def test_counts_failed_targeted_fixes(self, tmp_improvements):
        from tools.supervisor import _load_fix_attempts
        entries = [
            _targeted("craft_blacksmith"),
            _targeted("craft_blacksmith"),
            _targeted("mine_ore", reason="too_far (fail rate 90%)"),
            {"action": "auto_recover", "reason": "stuck",
             "success": True, "code_changed": False},
        ]
        with open(tmp_improvements, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        with patch("tools.supervisor.IMPROVEMENTS_LOG", tmp_improvements):
            attempts = _load_fix_attempts()

        assert attempts["craft_blacksmith:missing_resource"] == 2
        assert attempts["mine_ore:too_far"] == 1
        assert "auto_recover" not in str(attempts)

    def test_resets_on_success(self, tmp_improvements):
        from tools.supervisor import _load_fix_attempts
        entries = [
            _targeted("craft_blacksmith"),
            _targeted("craft_blacksmith", success=True, changed=True),
            _targeted("craft_blacksmith"),
        ]
        with open(tmp_improvements, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        with patch("tools.supervisor.IMPROVEMENTS_LOG", tmp_improvements):
            attempts = _load_fix_attempts()

        assert attempts["craft_blacksmith:missing_resource"] == 1

    def test_empty_file(self, tmp_improvements):
        from tools.supervisor import _load_fix_attempts
        tmp_improvements.write_text("")
        with patch("tools.supervisor.IMPROVEMENTS_LOG", tmp_improvements):
            attempts = _load_fix_attempts()
        assert attempts == {}

    def test_missing_file(self, tmp_path):
        from tools.supervisor import _load_fix_attempts
        missing = tmp_path / "nonexistent.jsonl"
        with patch("tools.supervisor.IMPROVEMENTS_LOG", missing):
            attempts = _load_fix_attempts()
        assert attempts == {}


class TestProgressiveTimeout:
    def test_timeout_increases(self):
        from tools.supervisor import _get_timeout
        assert _get_timeout(0) == 300
        assert _get_timeout(1) == 450
        assert _get_timeout(2) == 600

    def test_timeout_caps_at_max(self):
        from tools.supervisor import _get_timeout
        assert _get_timeout(5) == 600
        assert _get_timeout(100) == 600


class TestWriteSupervisorHints:
    def test_writes_skip(self, tmp_path):
        from tools.supervisor import _write_skip_hint
        hints_file = tmp_path / "supervisor_hints.json"
        with patch("tools.supervisor.HINTS_FILE", hints_file):
            _write_skip_hint(
                "craft_blacksmith", "missing_resource", ttl_hours=1,
            )

        data = json.loads(hints_file.read_text())
        assert "craft_blacksmith" in data["skip_procedures"]
        hint = data["skip_procedures"]["craft_blacksmith"]
        assert hint["reason"] == "missing_resource"
        assert "until" in hint

    def test_appends_to_existing(self, tmp_path):
        from tools.supervisor import _write_skip_hint
        hints_file = tmp_path / "supervisor_hints.json"
        hints_file.write_text(json.dumps({
            "skip_procedures": {
                "mine_ore": {"until": 9999999999, "reason": "too_far"}
            }
        }))
        with patch("tools.supervisor.HINTS_FILE", hints_file):
            _write_skip_hint(
                "craft_blacksmith", "missing_resource", ttl_hours=1,
            )

        data = json.loads(hints_file.read_text())
        assert "mine_ore" in data["skip_procedures"]
        assert "craft_blacksmith" in data["skip_procedures"]
