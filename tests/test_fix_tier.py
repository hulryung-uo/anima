"""Tests for the micro-fix tier helpers."""
from unittest.mock import patch, MagicMock

import pytest

from tools.fix_tier import (
    TIER_QUICK, TIER_DEEP,
    build_quick_prompt, is_too_complex, call_quick_fix,
)


class TestConstants:
    def test_tier_quick_value(self):
        assert TIER_QUICK == "quick"

    def test_tier_deep_value(self):
        assert TIER_DEEP == "deep"


class TestBuildQuickPrompt:
    def test_includes_problem_and_diagnostic(self):
        p = build_quick_prompt("DIAG TEXT", "craft_blacksmith failing")
        assert "DIAG TEXT" in p
        assert "craft_blacksmith failing" in p
        assert "MICRO-FIX" in p
        assert "ESCALATE" in p

    def test_prompt_is_short_enough(self):
        p = build_quick_prompt("x" * 100, "y")
        # Should be a small prompt, definitely under 4KB
        assert len(p) < 4096

    def test_includes_commit_instruction(self):
        p = build_quick_prompt("diag", "problem")
        assert "git commit" in p.lower() or "Micro-fix" in p

    def test_includes_test_instruction(self):
        p = build_quick_prompt("diag", "problem")
        assert "pytest" in p or "uv run" in p


class TestIsTooComplex:
    def test_explicit_escalate_detected(self):
        assert is_too_complex("ESCALATE: needs multi-file refactor") is True

    def test_escalate_case_insensitive(self):
        assert is_too_complex("escalate: deep analysis needed") is True

    def test_no_changes_with_architectural_words(self):
        text = "I looked at this but it requires deeper analysis across modules"
        assert is_too_complex(text) is True

    def test_successful_fix_not_too_complex(self):
        text = "Done. Committed 1-line fix: add cooldown check at line 42."
        assert is_too_complex(text) is False

    def test_empty_output_is_too_complex(self):
        assert is_too_complex("") is True

    def test_too_complex_phrase_detected(self):
        assert is_too_complex("This is too complex for a quick fix.") is True

    def test_requires_deeper_analysis_detected(self):
        assert is_too_complex("This requires deeper analysis.") is True

    def test_architectural_concerns_detected(self):
        text = "No changes made. The issue is architectural and spans multiple files."
        assert is_too_complex(text) is True

    def test_clean_successful_output_not_complex(self):
        text = "Applied fix: changed threshold from 5 to 10 in mine_ore.py. Tests pass."
        assert is_too_complex(text) is False


class TestCallQuickFix:
    def test_returns_tuple_on_success(self):
        """Mocked subprocess.run returns cleanly, git HEAD changes."""
        with patch("tools.fix_tier.subprocess.run") as mock_run, \
             patch("tools.fix_tier._current_sha") as mock_sha:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="Done. Committed fix.", stderr="",
            )
            mock_sha.side_effect = ["abc1234", "def5678"]  # changed
            success, committed, output = call_quick_fix(
                "diag", "problem", timeout=60,
            )
            assert success is True
            assert committed is True
            assert "Committed fix" in output

    def test_returns_not_committed_when_sha_unchanged(self):
        with patch("tools.fix_tier.subprocess.run") as mock_run, \
             patch("tools.fix_tier._current_sha") as mock_sha:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="No changes applied.", stderr="",
            )
            mock_sha.side_effect = ["abc", "abc"]  # unchanged
            success, committed, output = call_quick_fix(
                "diag", "problem", timeout=60,
            )
            assert success is True
            assert committed is False

    def test_timeout_handled(self):
        import subprocess
        with patch("tools.fix_tier.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd="claude", timeout=60,
            )
            success, committed, output = call_quick_fix(
                "diag", "problem", timeout=60,
            )
            assert success is False
            assert committed is False
            assert "timed out" in output.lower()

    def test_nonzero_returncode_is_failure(self):
        with patch("tools.fix_tier.subprocess.run") as mock_run, \
             patch("tools.fix_tier._current_sha") as mock_sha:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="error occurred",
            )
            mock_sha.side_effect = ["abc", "abc"]
            success, committed, output = call_quick_fix(
                "diag", "problem", timeout=60,
            )
            assert success is False
            assert committed is False

    def test_uses_haiku_model_by_default(self):
        with patch("tools.fix_tier.subprocess.run") as mock_run, \
             patch("tools.fix_tier._current_sha") as mock_sha:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="done", stderr="",
            )
            mock_sha.side_effect = ["a", "a"]
            call_quick_fix("diag", "problem", timeout=60)
            call_args = mock_run.call_args
            cmd = call_args[0][0]
            assert any("haiku" in arg.lower() for arg in cmd)

    def test_custom_model_passed_through(self):
        with patch("tools.fix_tier.subprocess.run") as mock_run, \
             patch("tools.fix_tier._current_sha") as mock_sha:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="done", stderr="",
            )
            mock_sha.side_effect = ["a", "a"]
            call_quick_fix("diag", "problem", timeout=60, model="custom-model-123")
            call_args = mock_run.call_args
            cmd = call_args[0][0]
            assert "custom-model-123" in cmd

    def test_stderr_appended_to_output(self):
        with patch("tools.fix_tier.subprocess.run") as mock_run, \
             patch("tools.fix_tier._current_sha") as mock_sha:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="stdout text", stderr="stderr text",
            )
            mock_sha.side_effect = ["a", "a"]
            _, _, output = call_quick_fix("diag", "problem", timeout=60)
            assert "stdout text" in output
            assert "stderr text" in output
