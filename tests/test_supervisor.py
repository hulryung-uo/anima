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
        assert _get_timeout(0) == 400
        assert _get_timeout(1) == 600
        assert _get_timeout(2) == 900

    def test_timeout_caps_at_max(self):
        from tools.supervisor import _get_timeout
        assert _get_timeout(5) == 900
        assert _get_timeout(100) == 900


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


class TestLogImprovementOutcome:
    def test_auto_recover_writes_restart_only_outcome(self, tmp_path, monkeypatch):
        from tools import supervisor
        log = tmp_path / "improvements.jsonl"
        monkeypatch.setattr(supervisor, "IMPROVEMENTS_LOG", log)
        monkeypatch.setattr(supervisor, "get_git_head", lambda: "abc1234")

        supervisor._log_improvement(
            "auto_recover", "stuck loop", success=True, code_changed=False,
        )
        entry = json.loads(log.read_text().strip())
        assert entry["outcome"] == "restart_only"
        assert entry["code_changed"] is False

    def test_targeted_fix_with_commit_writes_code_fix_outcome(
        self, tmp_path, monkeypatch,
    ):
        from tools import supervisor
        log = tmp_path / "improvements.jsonl"
        monkeypatch.setattr(supervisor, "IMPROVEMENTS_LOG", log)
        monkeypatch.setattr(supervisor, "get_git_head", lambda: "abc1234")

        supervisor._log_improvement(
            "targeted_fix:mine_ore", "bad", success=True, code_changed=True,
        )
        entry = json.loads(log.read_text().strip())
        assert entry["outcome"] == "code_fix"

    def test_failed_claude_writes_failed_outcome(self, tmp_path, monkeypatch):
        from tools import supervisor
        log = tmp_path / "improvements.jsonl"
        monkeypatch.setattr(supervisor, "IMPROVEMENTS_LOG", log)
        monkeypatch.setattr(supervisor, "get_git_head", lambda: "abc1234")

        supervisor._log_improvement(
            "targeted_fix:x", "bad", success=False, code_changed=False,
        )
        entry = json.loads(log.read_text().strip())
        assert entry["outcome"] == "failed"

    def test_skip_writes_skipped_outcome(self, tmp_path, monkeypatch):
        from tools import supervisor
        log = tmp_path / "improvements.jsonl"
        monkeypatch.setattr(supervisor, "IMPROVEMENTS_LOG", log)
        monkeypatch.setattr(supervisor, "get_git_head", lambda: "abc1234")

        supervisor._log_improvement(
            "skip:craft_blacksmith", "give up", success=False, code_changed=False,
        )
        entry = json.loads(log.read_text().strip())
        assert entry["outcome"] == "skipped"


class TestDegradationDetection:
    def _entry(self, outcome: str) -> dict:
        return {
            "ts": "2026-04-18 00:00:00", "action": "auto_recover",
            "reason": "x", "success": True, "outcome": outcome,
            "code_changed": outcome == "code_fix", "commit": "abc",
            "output_preview": "",
        }

    def test_streak_counts_restart_only_entries(self, tmp_path, monkeypatch):
        from tools import supervisor
        log = tmp_path / "improvements.jsonl"
        with open(log, "w") as f:
            for _ in range(4):
                f.write(json.dumps(self._entry("restart_only")) + "\n")
        monkeypatch.setattr(supervisor, "IMPROVEMENTS_LOG", log)
        assert supervisor._count_unproductive_streak() == 4

    def test_streak_resets_on_code_fix(self, tmp_path, monkeypatch):
        from tools import supervisor
        log = tmp_path / "improvements.jsonl"
        with open(log, "w") as f:
            for _ in range(3):
                f.write(json.dumps(self._entry("restart_only")) + "\n")
            f.write(json.dumps(self._entry("code_fix")) + "\n")
            f.write(json.dumps(self._entry("restart_only")) + "\n")
        monkeypatch.setattr(supervisor, "IMPROVEMENTS_LOG", log)
        assert supervisor._count_unproductive_streak() == 1

    def test_degradation_threshold_triggers_alert_file(self, tmp_path, monkeypatch):
        from tools import supervisor
        log = tmp_path / "improvements.jsonl"
        flag = tmp_path / "supervisor_alert.flag"
        with open(log, "w") as f:
            for _ in range(5):
                f.write(json.dumps(self._entry("restart_only")) + "\n")
        monkeypatch.setattr(supervisor, "IMPROVEMENTS_LOG", log)
        monkeypatch.setattr(supervisor, "ALERT_FLAG", flag)

        triggered = supervisor._check_degradation()
        assert triggered is True
        assert flag.exists()
        data = json.loads(flag.read_text())
        assert data["streak"] == 5

    def test_no_alert_below_threshold(self, tmp_path, monkeypatch):
        from tools import supervisor
        log = tmp_path / "improvements.jsonl"
        flag = tmp_path / "supervisor_alert.flag"
        with open(log, "w") as f:
            for _ in range(4):
                f.write(json.dumps(self._entry("restart_only")) + "\n")
        monkeypatch.setattr(supervisor, "IMPROVEMENTS_LOG", log)
        monkeypatch.setattr(supervisor, "ALERT_FLAG", flag)

        assert supervisor._check_degradation() is False
        assert not flag.exists()
