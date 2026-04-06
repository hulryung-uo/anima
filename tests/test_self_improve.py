"""Tests for self_improve.detect_problems — DB-based failure detection."""
from tools.self_improve import detect_problems


class TestDetectProblemsFromDB:
    def test_high_fail_rate_detected(self):
        """Procedure with >80% fail rate and some successes → HIGH problem."""
        data = {
            "counts": {},
            "recent_lines": 100,
            "db_stats": {
                "craft_blacksmith:missing_resource": {"count": 12, "avg_ms": 8000},
                "craft_blacksmith:success": {"count": 2, "avg_ms": 5000},
            },
        }
        problems = detect_problems(data)
        names = [p["name"] for p in problems]
        assert "db_procedure_failing" in names
        match = [p for p in problems if p["name"] == "db_procedure_failing"][0]
        assert match["severity"] == "HIGH"

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

    def test_moving_not_working_detected(self):
        """Many procedures selected but no results → HIGH."""
        data = {
            "counts": {
                "planner_selected": 71,
                "procedure_result": 0,
                "walk_confirmed": 2840,
            },
            "recent_lines": 500,
            "db_stats": {},
        }
        problems = detect_problems(data)
        names = [p["name"] for p in problems]
        assert "moving_not_working" in names

    def test_moving_not_working_normal_ignored(self):
        """Normal operation with results → no problem."""
        data = {
            "counts": {
                "planner_selected": 50,
                "procedure_result": 45,
                "walk_confirmed": 1000,
            },
            "recent_lines": 500,
            "db_stats": {},
        }
        problems = detect_problems(data)
        names = [p["name"] for p in problems]
        assert "moving_not_working" not in names

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
