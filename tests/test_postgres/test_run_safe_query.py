"""Unit tests for `run_safe_query` — no live DB needed.

The integration suite covers the round-trip + DB-side timeout +
read-only enforcement. These tests pin the *cursor* contract: the
helper must pull at most ``row_cap + 1`` rows from the server,
regardless of how big the underlying result set is, and must flag
``truncated=True`` when the cap is exceeded. The original
implementation called `await conn.fetch(sql)` which materialised
every row before truncating Python-side — a memory bomb on tables
with millions of rows.
"""

from __future__ import annotations

from typing import Any

import pytest

from devops_mcp_bundle.postgres import queries


class _FakeRecord(dict[str, Any]):
    """A mapping that mimics asyncpg.Record's keys()/__getitem__ surface."""


class _FakeCursor:
    def __init__(self, total_rows: int, columns: list[str]) -> None:
        self._total = total_rows
        self._columns = columns
        # Records the n passed to fetch() so tests can assert the
        # cursor was asked for *exactly* row_cap + 1 — proxy for
        # "the helper didn't pull the whole table".
        self.fetch_calls: list[int] = []

    async def fetch(self, n: int) -> list[_FakeRecord]:
        self.fetch_calls.append(n)
        capped = min(n, self._total)
        return [_FakeRecord((c, i) for i, c in enumerate(self._columns)) for _ in range(capped)]


class _FakeTxn:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> _FakeTxn:
        self.entered = True
        return self

    async def __aexit__(self, *args: object) -> None:
        self.exited = True


class _FakeConn:
    """Minimal asyncpg.Connection stand-in for run_safe_query.

    `transaction(readonly=True)` returns an async context manager;
    `execute()` is awaited for the SET LOCAL; `cursor()` returns a
    cursor that we hand-roll above. The helper never touches anything
    else.
    """

    def __init__(self, total_rows: int, columns: list[str]) -> None:
        self._cursor = _FakeCursor(total_rows, columns)
        self.execute_sql: list[str] = []
        self.cursor_sql: list[str] = []
        self.txn = _FakeTxn()

    def transaction(self, *, readonly: bool = False) -> _FakeTxn:
        assert readonly is True, "run_safe_query must use a read-only transaction"
        return self.txn

    async def execute(self, sql: str) -> None:
        self.execute_sql.append(sql)

    async def cursor(self, sql: str) -> _FakeCursor:
        self.cursor_sql.append(sql)
        return self._cursor


class TestRunSafeQueryUsesCursor:
    async def test_uses_cursor_with_row_cap_plus_one(self) -> None:
        # Underlying table is huge (10k rows); the helper must ask
        # the cursor for *only* row_cap + 1 = 6 rows. Anything more
        # is the old fetch-everything-then-truncate bug.
        conn = _FakeConn(total_rows=10_000, columns=["id"])
        await queries.run_safe_query(conn, "SELECT id FROM big", row_cap=5)  # type: ignore[arg-type]
        assert conn._cursor.fetch_calls == [6]

    async def test_truncates_large_result_and_flags_it(self) -> None:
        # 100 rows back, cap = 10 → response holds 10 rows, truncated=True.
        conn = _FakeConn(total_rows=100, columns=["id"])
        result = await queries.run_safe_query(conn, "SELECT id FROM t", row_cap=10)  # type: ignore[arg-type]
        assert result.row_count == 10
        assert result.truncated is True
        assert len(result.rows) == 10

    async def test_no_truncation_flag_when_under_cap(self) -> None:
        # 3 rows back, cap = 10 → all rows returned, truncated=False.
        conn = _FakeConn(total_rows=3, columns=["id"])
        result = await queries.run_safe_query(conn, "SELECT id FROM small", row_cap=10)  # type: ignore[arg-type]
        assert result.row_count == 3
        assert result.truncated is False

    async def test_empty_result_is_not_truncated(self) -> None:
        conn = _FakeConn(total_rows=0, columns=["id"])
        result = await queries.run_safe_query(conn, "SELECT id FROM empty", row_cap=10)  # type: ignore[arg-type]
        assert result.row_count == 0
        assert result.truncated is False
        assert result.rows == []
        assert result.columns == []

    async def test_set_local_statement_timeout_issued(self) -> None:
        # The cursor only bounds row count; the timeout still has to
        # come through `SET LOCAL statement_timeout` — pin it so a
        # future refactor that drops the execute() can't regress.
        conn = _FakeConn(total_rows=1, columns=["id"])
        await queries.run_safe_query(
            conn,  # type: ignore[arg-type]
            "SELECT 1",
            timeout_ms=2500,
        )
        assert any("statement_timeout" in s and "2500" in s for s in conn.execute_sql)

    @pytest.mark.parametrize("bad", [0, -1, -1000])
    async def test_invalid_timeout_rejected(self, bad: int) -> None:
        conn = _FakeConn(total_rows=0, columns=["id"])
        with pytest.raises(ValueError, match="timeout_ms"):
            await queries.run_safe_query(conn, "SELECT 1", timeout_ms=bad)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [0, -1, -100])
    async def test_invalid_row_cap_rejected(self, bad: int) -> None:
        conn = _FakeConn(total_rows=0, columns=["id"])
        with pytest.raises(ValueError, match="row_cap"):
            await queries.run_safe_query(conn, "SELECT 1", row_cap=bad)  # type: ignore[arg-type]


class TestRunSafeQueryUpperBounds:
    """Hard ceilings on ``timeout_ms`` and ``row_cap``.

    The lower bound (``> 0``) was already enforced; without an upper
    bound a caller could pass ``timeout_ms=3_600_000`` (a one-hour
    ``SET LOCAL statement_timeout``) or ``row_cap=5_000_000`` (which
    the server-side cursor honours by materialising 5M+1 asyncpg
    Records in agent memory before truncation). Both protect the agent
    process from misuse — the database has its own protections.
    """

    async def test_timeout_above_max_rejected(self) -> None:
        conn = _FakeConn(total_rows=0, columns=["id"])
        with pytest.raises(ValueError, match="MAX_QUERY_TIMEOUT_MS"):
            await queries.run_safe_query(
                conn,  # type: ignore[arg-type]
                "SELECT 1",
                timeout_ms=queries.MAX_QUERY_TIMEOUT_MS + 1,
            )

    async def test_row_cap_above_max_rejected(self) -> None:
        conn = _FakeConn(total_rows=0, columns=["id"])
        with pytest.raises(ValueError, match="MAX_QUERY_ROW_CAP"):
            await queries.run_safe_query(
                conn,  # type: ignore[arg-type]
                "SELECT 1",
                row_cap=queries.MAX_QUERY_ROW_CAP + 1,
            )

    async def test_timeout_at_max_accepted(self) -> None:
        # Boundary: exactly MAX is fine.
        conn = _FakeConn(total_rows=1, columns=["id"])
        await queries.run_safe_query(
            conn,  # type: ignore[arg-type]
            "SELECT 1",
            timeout_ms=queries.MAX_QUERY_TIMEOUT_MS,
        )

    async def test_row_cap_at_max_accepted(self) -> None:
        conn = _FakeConn(total_rows=1, columns=["id"])
        await queries.run_safe_query(
            conn,  # type: ignore[arg-type]
            "SELECT 1",
            row_cap=queries.MAX_QUERY_ROW_CAP,
        )
