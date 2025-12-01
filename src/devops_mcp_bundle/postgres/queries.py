"""Pure async functions wrapping the per-tool SQL.

Split out of `server.py` so they're testable without a running MCP server
— each function takes an explicit `asyncpg.Connection` and returns a
typed Pydantic model.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from devops_mcp_bundle.postgres.models import (
    ActivitySnapshot,
    BloatEstimate,
    ColumnInfo,
    DatabaseInfo,
    IndexInfo,
    QueryResult,
    SlowQuery,
    TableInfo,
    TableSchema,
    VacuumStatus,
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


async def slow_queries(
    conn: asyncpg.Connection, min_mean_ms: float = 100.0, limit: int = 20
) -> list[SlowQuery]:
    """Read pg_stat_statements. Returns [] if the extension is not installed."""
    has_ext = await conn.fetchval("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'")
    if not has_ext:
        return []
    rows = await conn.fetch(
        """
        SELECT query,
               calls,
               total_exec_time AS total_exec_time_ms,
               mean_exec_time AS mean_exec_time_ms,
               rows,
               shared_blks_hit,
               shared_blks_read
        FROM pg_stat_statements
        WHERE mean_exec_time >= $1
        ORDER BY mean_exec_time DESC
        LIMIT $2
        """,
        min_mean_ms,
        limit,
    )
    return [
        SlowQuery(
            query=r["query"],
            calls=int(r["calls"]),
            total_exec_time_ms=float(r["total_exec_time_ms"]),
            mean_exec_time_ms=float(r["mean_exec_time_ms"]),
            rows=int(r["rows"]),
            shared_blks_hit=int(r["shared_blks_hit"]),
            shared_blks_read=int(r["shared_blks_read"]),
        )
        for r in rows
    ]


async def vacuum_status(conn: asyncpg.Connection, qualified: str) -> VacuumStatus:
    if "." in qualified:
        schema, name = qualified.split(".", 1)
    else:
        schema, name = "public", qualified

    row = await conn.fetchrow(
        """
        SELECT last_vacuum::text,
               last_autovacuum::text,
               last_analyze::text,
               last_autoanalyze::text,
               n_dead_tup,
               n_live_tup
        FROM pg_stat_user_tables
        WHERE schemaname = $1 AND relname = $2
        """,
        schema,
        name,
    )
    if not row:
        raise LookupError(f"no stats for {schema}.{name}")
    return VacuumStatus(
        schema=schema,
        name=name,
        last_vacuum=row["last_vacuum"],
        last_autovacuum=row["last_autovacuum"],
        last_analyze=row["last_analyze"],
        last_autoanalyze=row["last_autoanalyze"],
        n_dead_tup=int(row["n_dead_tup"] or 0),
        n_live_tup=int(row["n_live_tup"] or 0),
        autovacuum_vacuum_scale_factor=None,
    )


async def activity_snapshot(
    conn: asyncpg.Connection, min_runtime_ms: float = 0.0, exclude_idle: bool = False
) -> list[ActivitySnapshot]:
    """Snapshot `pg_stat_activity` rows that are at least `min_runtime_ms` old.

    Use to answer "what's the database doing right now?". Defaults are
    permissive (returns idle sessions too) so the caller can decide how
    to filter; the most common useful filter is
    ``exclude_idle=True, min_runtime_ms=500`` for "what's stuck?".
    """
    if min_runtime_ms < 0:
        raise ValueError("min_runtime_ms must be non-negative")

    sql = """
        SELECT pid,
               datname,
               usename,
               application_name,
               state,
               wait_event_type,
               wait_event,
               backend_start::text,
               xact_start::text,
               query_start::text,
               LEFT(query, 4096) AS query,
               EXTRACT(EPOCH FROM (now() - COALESCE(query_start, now()))) * 1000.0
                   AS runtime_ms
        FROM pg_stat_activity
        WHERE pid <> pg_backend_pid()
    """
    if exclude_idle:
        sql += " AND state IS DISTINCT FROM 'idle'"
    sql += " AND EXTRACT(EPOCH FROM (now() - COALESCE(query_start, now()))) * 1000.0 >= $1"
    sql += " ORDER BY runtime_ms DESC"

    rows = await conn.fetch(sql, min_runtime_ms)
    return [
        ActivitySnapshot(
            pid=int(r["pid"]),
            datname=r["datname"],
            usename=r["usename"],
            application_name=r["application_name"],
            state=r["state"],
            wait_event_type=r["wait_event_type"],
            wait_event=r["wait_event"],
            backend_start=r["backend_start"],
            xact_start=r["xact_start"],
            query_start=r["query_start"],
            query=r["query"],
            runtime_ms=float(r["runtime_ms"] or 0.0),
        )
        for r in rows
    ]


async def bloat_estimate(
    conn: asyncpg.Connection, schema: str = "public", min_ratio: float = 0.0
) -> list[BloatEstimate]:
    """Estimate per-table bloat without `pgstattuple` (planner-stats only).

    The classical "ioguix bloat query" approximates the on-disk size a
    table *would* have if every row used its average width and the
    fillfactor were honoured. The difference between that and the actual
    `pg_class.relpages * 8 KB` is a bloat estimate. Quick and rough — use
    `pgstattuple` for an exact number.
    """
    if min_ratio < 0:
        raise ValueError("min_ratio must be non-negative")

    rows = await conn.fetch(
        """
        WITH constants AS (SELECT current_setting('block_size')::numeric AS bs),
        stats AS (
            SELECT n.nspname AS schema,
                   c.relname AS name,
                   c.relpages::bigint AS relpages,
                   c.reltuples,
                   GREATEST(SUM(s.avg_width)::numeric, 0) AS row_width
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_stats s
                ON s.schemaname = n.nspname AND s.tablename = c.relname
            WHERE c.relkind IN ('r', 'p') AND n.nspname = $1
            GROUP BY n.nspname, c.relname, c.relpages, c.reltuples
        )
        SELECT schema,
               name,
               (relpages * (SELECT bs FROM constants))::bigint AS real_size_bytes,
               GREATEST(
                   (relpages * (SELECT bs FROM constants))
                   - (reltuples * (row_width + 24) / NULLIF((SELECT bs FROM constants), 0))
                       * (SELECT bs FROM constants),
                   0
               )::bigint AS bloat_size_bytes
        FROM stats
        ORDER BY bloat_size_bytes DESC
        """,
        schema,
    )
    out: list[BloatEstimate] = []
    for r in rows:
        real = int(r["real_size_bytes"] or 0)
        bloat = int(r["bloat_size_bytes"] or 0)
        ratio = (bloat / real) if real > 0 else 0.0
        if ratio < min_ratio:
            continue
        out.append(
            BloatEstimate(
                schema=r["schema"],
                name=r["name"],
                real_size_bytes=real,
                bloat_size_bytes=bloat,
                bloat_ratio=ratio,
            )
        )
    return out
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
