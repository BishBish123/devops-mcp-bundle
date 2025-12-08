"""Integration tests for the Postgres query helpers.

Requires a running Postgres instance at POSTGRES_DSN (default
postgresql://bench:bench@localhost:5433/bench — same container the
vector-db-bench dev stack uses).

Marked `integration` so the suite skips on machines without Docker.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from devops_mcp_bundle.postgres import queries

pytestmark = pytest.mark.integration

DEFAULT_DSN = "postgresql://bench:bench@localhost:5433/bench"


def _dsn() -> str:
    return os.environ.get("POSTGRES_DSN", DEFAULT_DSN)


@pytest_asyncio.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    c = await asyncpg.connect(_dsn())
    try:
        yield c
    finally:
        await c.close()


@pytest_asyncio.fixture
async def fixture_table(conn: asyncpg.Connection) -> AsyncIterator[str]:
    """Create a temp table for tests, drop it after."""
    name = f"mcp_test_{uuid.uuid4().hex[:8]}"
    await conn.execute(f'CREATE TABLE "{name}" (id serial PRIMARY KEY, label text NOT NULL, n int)')
    await conn.execute(
        f"INSERT INTO \"{name}\" (label, n) SELECT 'row ' || g, g FROM generate_series(1, 50) g"
    )
    await conn.execute(f'CREATE INDEX ON "{name}" (n)')
    try:
        yield name
    finally:
        await conn.execute(f'DROP TABLE IF EXISTS "{name}"')


class TestListTables:
    async def test_includes_fixture(self, conn: asyncpg.Connection, fixture_table: str) -> None:
        tables = await queries.list_tables(conn, schema="public")
        names = {t.name for t in tables}
        assert fixture_table in names
        match = next(t for t in tables if t.name == fixture_table)
        # Newly-created table reports row_estimate=0 until ANALYZE; good
        # enough to assert size > 0.
        assert match.size_bytes > 0


class TestDescribeTable:
    async def test_columns_and_indexes(self, conn: asyncpg.Connection, fixture_table: str) -> None:
        ts = await queries.describe_table(conn, fixture_table)
        col_names = [c.name for c in ts.columns]
        assert col_names == ["id", "label", "n"]
        # Two indexes: PK + the explicit one on (n).
        assert any(ix.is_primary for ix in ts.indexes)
        assert any("(n)" in ix.definition for ix in ts.indexes)

    async def test_missing_table_raises(self, conn: asyncpg.Connection) -> None:
        with pytest.raises(LookupError):
            await queries.describe_table(conn, "definitely_does_not_exist")


class TestVacuumStatus:
    async def test_returns_status_for_existing_table(
        self, conn: asyncpg.Connection, fixture_table: str
    ) -> None:
        # Force stats so n_live_tup is populated.
        await conn.execute(f'ANALYZE "{fixture_table}"')
        vs = await queries.vacuum_status(conn, fixture_table)
        assert vs.name == fixture_table
        assert vs.n_live_tup >= 0


class TestRunSafeQuery:
    async def test_select_round_trip(self, conn: asyncpg.Connection, fixture_table: str) -> None:
        result = await queries.run_safe_query(
            conn, f'SELECT id, label FROM "{fixture_table}" ORDER BY id'
        )
        assert result.row_count == 50
        assert result.columns == ["id", "label"]
        assert result.rows[0] == [1, "row 1"]
        assert result.elapsed_ms >= 0

    async def test_row_cap_enforced(self, conn: asyncpg.Connection, fixture_table: str) -> None:
        result = await queries.run_safe_query(
            conn, f'SELECT id FROM "{fixture_table}" ORDER BY id', row_cap=5
        )
        assert result.row_count == 5

    async def test_invalid_args_rejected(self, conn: asyncpg.Connection) -> None:
        with pytest.raises(ValueError, match="timeout"):
            await queries.run_safe_query(conn, "SELECT 1", timeout_ms=0)
        with pytest.raises(ValueError, match="row_cap"):
            await queries.run_safe_query(conn, "SELECT 1", row_cap=0)


class TestSlowQueries:
    async def test_returns_list(self, conn: asyncpg.Connection) -> None:
        # Don't depend on pg_stat_statements being installed; the function
        # returns [] gracefully if the extension is absent. Just call it
        # and assert the shape.
        rows = await queries.slow_queries(conn, min_mean_ms=0.0, limit=5)
        assert isinstance(rows, list)


class TestListDatabases:
    async def test_basic(self, conn: asyncpg.Connection) -> None:
        dbs = await queries.list_databases(conn)
        assert any(db.name == "bench" for db in dbs)
