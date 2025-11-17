"""Pure async functions wrapping the per-tool SQL.

Split out of `server.py` so they're testable without a running MCP server
— each function takes an explicit `asyncpg.Connection` and returns a
typed Pydantic model.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from devops_mcp_bundle.postgres.models import (
    ColumnInfo,
    DatabaseInfo,
    IndexInfo,
    QueryResult,
    TableInfo,
    TableSchema,
)

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg


async def list_databases(conn: asyncpg.Connection) -> list[DatabaseInfo]:
    rows = await conn.fetch(
        """
        SELECT datname,
               pg_get_userbyid(datdba) AS owner,
               pg_encoding_to_char(encoding) AS encoding,
               pg_database_size(datname) AS size_bytes
        FROM pg_database
        WHERE datistemplate = false
          AND datname <> 'postgres'
        ORDER BY datname
        """
    )
    return [
        DatabaseInfo(
            name=r["datname"],
            owner=r["owner"],
            encoding=r["encoding"],
            size_bytes=int(r["size_bytes"]),
        )
        for r in rows
    ]


async def list_tables(conn: asyncpg.Connection, schema: str = "public") -> list[TableInfo]:
    rows = await conn.fetch(
        """
        SELECT n.nspname AS schema,
               c.relname AS name,
               c.reltuples::bigint AS row_estimate,
               pg_total_relation_size(c.oid) AS size_bytes
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p')
          AND n.nspname = $1
        ORDER BY c.relname
        """,
        schema,
    )
    return [
        TableInfo(
            schema=r["schema"],
            name=r["name"],
            row_estimate=int(r["row_estimate"]),
            size_bytes=int(r["size_bytes"]),
        )
        for r in rows
    ]


async def describe_table(conn: asyncpg.Connection, qualified: str) -> TableSchema:
    if "." in qualified:
        schema, name = qualified.split(".", 1)
    else:
        schema, name = "public", qualified

    cols = await conn.fetch(
        """
        SELECT column_name, data_type, is_nullable = 'YES' AS nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = $1 AND table_name = $2
        ORDER BY ordinal_position
        """,
        schema,
        name,
    )
    if not cols:
        raise LookupError(f"table {schema}.{name!r} not found")
    columns = [
        ColumnInfo(
            name=c["column_name"],
            data_type=c["data_type"],
            is_nullable=c["nullable"],
            default=c["column_default"],
        )
        for c in cols
    ]

    idx_rows = await conn.fetch(
        """
        SELECT i.relname AS index_name,
               pg_get_indexdef(ix.indexrelid) AS definition,
               ix.indisunique AS is_unique,
               ix.indisprimary AS is_primary
        FROM pg_class t
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_index ix ON ix.indrelid = t.oid
        JOIN pg_class i ON i.oid = ix.indexrelid
        WHERE n.nspname = $1 AND t.relname = $2
        ORDER BY i.relname
        """,
        schema,
        name,
    )
    indexes = [
        IndexInfo(
            name=ix["index_name"],
            definition=ix["definition"],
            is_unique=ix["is_unique"],
            is_primary=ix["is_primary"],
        )
        for ix in idx_rows
    ]
    return TableSchema(schema=schema, name=name, columns=columns, indexes=indexes)

async def run_safe_query(
    conn: asyncpg.Connection, sql: str, timeout_ms: int = 5000, row_cap: int = 1000
) -> QueryResult:
    """Run a read-only query with a hard timeout + row cap.

    Caller is responsible for SQL safety (call `is_read_only_sql` first).
    The connection should also be configured with
    `default_transaction_read_only=on` so the database refuses writes
    even if the parser is somehow fooled.

    Implementation notes:

    - `timeout_ms` is enforced via `SET LOCAL statement_timeout` *inside*
      an explicit transaction. asyncpg autocommits each `execute()` if no
      transaction is open, which would discard `SET LOCAL` immediately
      and leave the caller's timeout silently ignored.
    - `row_cap` is enforced by truncating the result set in Python rather
      than rewriting the SQL. The naive `f"{sql} LIMIT N"` pattern breaks
      `SHOW`, breaks user-supplied `LIMIT`/`FETCH`, and is bypassed by a
      trailing `--` comment.
    """
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be positive")
    if row_cap <= 0:
        raise ValueError("row_cap must be positive")

    start = time.perf_counter()
    async with conn.transaction(readonly=True):
        await conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
        rows = await conn.fetch(sql)
    elapsed = (time.perf_counter() - start) * 1000.0

    if not rows:
        return QueryResult(columns=[], rows=[], row_count=0, elapsed_ms=elapsed)
    capped = rows[:row_cap]
    columns = list(capped[0].keys())
    return QueryResult(
        columns=columns,
        rows=[[r[c] for c in columns] for r in capped],
        row_count=len(capped),
        elapsed_ms=elapsed,
    )
