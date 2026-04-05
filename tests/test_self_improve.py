"""Tests for self_improve.detect_problems — DB-based failure detection."""
from tools.self_improve import detect_problems


class TestDetectProblemsFromDB:
    def test_high_fail_rate_detected(self):
        """Procedure with >80% fail rate and >10 attempts → HIGH problem."""
        data = {
            "counts": {},
            "recent_lines": 100,
            "db_stats": {
                "craft_blacksmith:missing_resource": {"count": 54, "avg_ms": 8000},
                "craft_blacksmith:success": {"count": 0, "avg_ms": 0},
            },
        }
        problems = detect_problems(data)
        names = [p["name"] for p in problems]
        assert "db_procedure_failing" in names
        match = [p for p in problems if p["name"] == "db_procedure_failing"][0]
        assert match["severity"] in ("HIGH", "CRITICAL")

    def test_moderate_fail_rate_ignored(self):
        """Procedure with 50% fail rate → no problem."""
        data = {
            "counts": {},
            "recent_lines": 100,
            "db_stats": {
                "mine_ore:too_far": {"count": 5, "avg_ms": 3000},
                "mine_ore:success": {"count": 5, "avg_ms": 5000},
            },
        }
        problems = detect_problems(data)
        names = [p["name"] for p in problems]
        assert "db_procedure_failing" not in names

    def test_low_sample_ignored(self):
        """Procedure with 100% fail but <5 attempts → no problem."""
        data = {
            "counts": {},
            "recent_lines": 100,
            "db_stats": {
                "sell_to_vendor:vendor_refused": {"count": 3, "avg_ms": 2000},
            },
        }
        problems = detect_problems(data)
        names = [p["name"] for p in problems]
        assert "db_procedure_failing" not in names

    def test_critical_at_zero_success_high_count(self):
        """0% success with >20 attempts → CRITICAL."""
        data = {
            "counts": {},
            "recent_lines": 100,
            "db_stats": {
                "craft_blacksmith:missing_resource": {"count": 30, "avg_ms": 8000},
            },
        }
        problems = detect_problems(data)
        match = [p for p in problems if p["name"] == "db_procedure_failing"]
        assert len(match) == 1
        assert match[0]["severity"] == "CRITICAL"
