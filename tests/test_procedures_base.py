"""Tests for Procedure base class, ProcedureResult, FailureReason, ActionLog (Step 3)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from anima.memory.database import MemoryDB
from anima.procedures.base import (
    FailureReason,
    Procedure,
    ProcedureRegistry,
    ProcedureResult,
)


# --- Test Procedure implementations ---

class SuccessProcedure(Procedure):
    name = "test_success"
    description = "Always succeeds"

    async def can_start(self, ctx):
        return True

    async def execute(self, ctx):
        return ProcedureResult(
            success=True,
            message="done",
            next_suggestion="continue_test",
            details={"ore_count": 5},
        )


class FailProcedure(Procedure):
    name = "test_fail"
    description = "Always fails"

    async def can_start(self, ctx):
        return False

    async def execute(self, ctx):
        return ProcedureResult(
            success=False,
            reason=FailureReason.MISSING_RESOURCE,
            message="no pickaxe",
        )


class CrashProcedure(Procedure):
    name = "test_crash"
    description = "Raises exception"

    async def can_start(self, ctx):
        return True

    async def execute(self, ctx):
        raise RuntimeError("boom")


# --- Fixtures ---

def _make_ctx(memory_db=None):
    ctx = MagicMock()
    ctx.perception.self_state.x = 100
    ctx.perception.self_state.y = 200
    ctx.memory_db = memory_db
    ctx.persona.name = "TestAgent"
    return ctx


@pytest.fixture
async def db(tmp_path: Path):
    memory_db = MemoryDB(tmp_path / "test.db")
    await memory_db.init()
    yield memory_db
    await memory_db.close()


# --- Tests ---

class TestProcedureResult:
    def test_success_result(self):
        r = ProcedureResult(success=True, message="ok")
        assert r.success
        assert r.reason is None
        assert r.next_suggestion is None

    def test_failure_result(self):
        r = ProcedureResult(
            success=False,
            reason=FailureReason.BLOCKED,
            message="path blocked",
        )
        assert not r.success
        assert r.reason == FailureReason.BLOCKED

    def test_all_failure_reasons(self):
        for reason in FailureReason:
            r = ProcedureResult(success=False, reason=reason)
            assert r.reason.value  # all have string values

    def test_continuation_hint(self):
        r = ProcedureResult(success=True, next_suggestion="mine_ore")
        assert r.next_suggestion == "mine_ore"


class TestProcedureRun:
    @pytest.mark.asyncio
    async def test_success_run(self):
        proc = SuccessProcedure()
        ctx = _make_ctx()
        result = await proc.run(ctx)
        assert result.success
        assert result.message == "done"
        assert result.next_suggestion == "continue_test"

    @pytest.mark.asyncio
    async def test_fail_run(self):
        proc = FailProcedure()
        ctx = _make_ctx()
        result = await proc.run(ctx)
        assert not result.success
        assert result.reason == FailureReason.MISSING_RESOURCE

    @pytest.mark.asyncio
    async def test_crash_handled(self):
        proc = CrashProcedure()
        ctx = _make_ctx()
        result = await proc.run(ctx)
        assert not result.success
        assert result.reason == FailureReason.PERMANENT
        assert "boom" in result.message

    @pytest.mark.asyncio
    async def test_diagnose_can_start(self):
        proc = SuccessProcedure()
        ctx = _make_ctx()
        assert await proc.diagnose(ctx) is None

    @pytest.mark.asyncio
    async def test_diagnose_cannot_start(self):
        proc = FailProcedure()
        ctx = _make_ctx()
        reason = await proc.diagnose(ctx)
        assert reason is not None
        assert "test_fail" in reason


class TestProcedureRegistry:
    def test_register_and_get(self):
        reg = ProcedureRegistry()
        proc = SuccessProcedure()
        reg.register(proc)
        assert reg.get("test_success") is proc
        assert reg.get("nonexistent") is None

    def test_all_procedures(self):
        reg = ProcedureRegistry()
        reg.register(SuccessProcedure())
        reg.register(FailProcedure())
        assert len(reg.all_procedures) == 2

    @pytest.mark.asyncio
    async def test_available(self):
        reg = ProcedureRegistry()
        reg.register(SuccessProcedure())
        reg.register(FailProcedure())
        ctx = _make_ctx()
        available = await reg.available(ctx)
        assert len(available) == 1
        assert available[0].name == "test_success"

    def test_describe_all(self):
        reg = ProcedureRegistry()
        reg.register(SuccessProcedure())
        desc = reg.describe_all()
        assert "test_success" in desc
        assert "Always succeeds" in desc


class TestActionLog:
    @pytest.mark.asyncio
    async def test_action_log_recorded(self, db: MemoryDB):
        proc = SuccessProcedure()
        ctx = _make_ctx(memory_db=db)
        await proc.run(ctx)

        rows = await db.db.execute_fetchall(
            "SELECT * FROM action_logs WHERE agent = ?", ("TestAgent",)
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["procedure"] == "test_success"
        assert row["result"] == "success"
        assert row["location_x"] == 100
        assert row["location_y"] == 200
        assert row["duration_ms"] >= 0
        details = json.loads(row["details"])
        assert details["ore_count"] == 5

    @pytest.mark.asyncio
    async def test_action_log_failure(self, db: MemoryDB):
        proc = FailProcedure()
        ctx = _make_ctx(memory_db=db)
        await proc.run(ctx)

        rows = await db.db.execute_fetchall(
            "SELECT * FROM action_logs WHERE agent = ?", ("TestAgent",)
        )
        assert len(rows) == 1
        assert rows[0]["result"] == "missing_resource"

    @pytest.mark.asyncio
    async def test_action_log_crash(self, db: MemoryDB):
        proc = CrashProcedure()
        ctx = _make_ctx(memory_db=db)
        await proc.run(ctx)

        rows = await db.db.execute_fetchall(
            "SELECT * FROM action_logs WHERE agent = ?", ("TestAgent",)
        )
        assert len(rows) == 1
        assert rows[0]["result"] == "permanent"
        assert "boom" in rows[0]["message"]

    @pytest.mark.asyncio
    async def test_action_log_no_db(self):
        """ActionLog should not crash when memory_db is None."""
        proc = SuccessProcedure()
        ctx = _make_ctx(memory_db=None)
        result = await proc.run(ctx)
        assert result.success  # should complete without error

    @pytest.mark.asyncio
    async def test_wal_mode(self, db: MemoryDB):
        """Verify WAL mode is enabled."""
        cursor = await db.db.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        assert row[0] == "wal"
