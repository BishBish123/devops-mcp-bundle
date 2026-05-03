"""FastMCP entry point for the Postgres DBA server.

Reads `POSTGRES_DSN` from the environment (or `--dsn` on the command line).
Stdio transport by default; SSE/HTTP available via `--http`.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
import typer
from fastmcp import FastMCP

from devops_mcp_bundle.postgres import queries
from devops_mcp_bundle.postgres.models import (
    ActivitySnapshot,
    BloatEstimate,
    DatabaseInfo,
    QueryResult,
    SlowQuery,
    StatementClass,
    TableInfo,
    TableSchema,
    VacuumStatus,
)
from devops_mcp_bundle.postgres.safety import classify_sql

mcp: FastMCP = FastMCP(
    name="postgres-dba",
    instructions=(
        "Postgres DBA tools — read-only by default. Use list_databases / "
        "list_tables / describe_table to discover schema, slow_queries to "
        "find performance hotspots from pg_stat_statements, and "
        "run_safe_query for ad-hoc SELECTs (parser-validated, server-side "
        "read-only enforced)."
    ),
)


def _dsn() -> str:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError(
            "POSTGRES_DSN env var is required (e.g. postgresql://bench:bench@localhost:5433/bench)"
        )
    return dsn


@asynccontextmanager
async def _connect() -> AsyncIterator[asyncpg.Connection]:
    """Open one read-only connection per tool call (simple + safe).

    Sets `default_transaction_read_only = on` so server-side will refuse
    any write — second line of defence behind `is_read_only_sql`.
    """
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("SET default_transaction_read_only = on")
        await conn.execute("SET statement_timeout = 10000")
        yield conn
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# MCP tool surface — every function below becomes a callable tool.
# ---------------------------------------------------------------------------


@mcp.tool
async def list_databases() -> list[DatabaseInfo]:
    """List non-template databases on the server with size + owner."""
    async with _connect() as conn:
        return await queries.list_databases(conn)


@mcp.tool
async def list_tables(schema: str = "public") -> list[TableInfo]:
    """List tables in a schema with row estimate and on-disk size."""
    async with _connect() as conn:
        return await queries.list_tables(conn, schema=schema)


@mcp.tool
async def describe_table(qualified_name: str) -> TableSchema:
    """Return columns + indexes for `schema.table` (or just `table` for public)."""
    async with _connect() as conn:
        return await queries.describe_table(conn, qualified_name)


@mcp.tool
async def slow_queries(min_mean_ms: float = 100.0, limit: int = 20) -> list[SlowQuery]:
    """Top-N slow queries from pg_stat_statements (returns [] if extension missing)."""
    async with _connect() as conn:
        return await queries.slow_queries(conn, min_mean_ms=min_mean_ms, limit=limit)


@mcp.tool
async def vacuum_status(qualified_name: str) -> VacuumStatus:
    """When was a table last vacuumed/analyzed and how much dead-tuple buildup is there?"""
    async with _connect() as conn:
        return await queries.vacuum_status(conn, qualified_name)


@mcp.tool
async def activity_snapshot(
    min_runtime_ms: float = 0.0, exclude_idle: bool = False
) -> list[ActivitySnapshot]:
    """Return active queries from `pg_stat_activity` ordered by runtime."""
    async with _connect() as conn:
        return await queries.activity_snapshot(
            conn, min_runtime_ms=min_runtime_ms, exclude_idle=exclude_idle
        )


@mcp.tool
async def bloat_estimate(schema: str = "public", min_ratio: float = 0.0) -> list[BloatEstimate]:
    """Estimate dead-tuple bloat per table (planner-stats only, no extension)."""
    async with _connect() as conn:
        return await queries.bloat_estimate(conn, schema=schema, min_ratio=min_ratio)


@mcp.tool
def kill_query(pid: int) -> str:
    """Refuse a backend kill request — documents the read-only contract."""
    return queries.kill_query(pid)


@mcp.tool
def classify_statement(sql: str) -> StatementClass:
    """Classify a SQL statement without executing it.

    Useful when an agent wants to *explain* to the user why a candidate
    query is or isn't allowed before sending it to `run_safe_query`.
    """
    c = classify_sql(sql)
    return StatementClass(
        is_read_only=c.is_read_only, leading_keyword=c.leading_keyword, reason=c.reason
    )


@mcp.tool
async def run_safe_query(sql: str, timeout_ms: int = 5000, row_cap: int = 1000) -> QueryResult:
    """Run a parser-validated, read-only SELECT.

    Refuses anything that is not a single SELECT/EXPLAIN/SHOW/WITH/VALUES
    statement before contacting the database. Server-side enforces
    `default_transaction_read_only` as a second layer.
    """
    classification = classify_sql(sql)
    if not classification.is_read_only:
        raise ValueError(f"SQL refused: {classification.reason}")
    async with _connect() as conn:
        return await queries.run_safe_query(conn, sql, timeout_ms=timeout_ms, row_cap=row_cap)


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


_cli = typer.Typer(name="mcp-postgres-dba", add_completion=False)


def _warn_missing_env() -> None:
    """Print a stderr warning if POSTGRES_DSN is unset.

    Non-fatal: the MCP client may still be expected to connect (so the
    error surfaces through the protocol, not as a process exit). Prints
    before FastMCP's banner so a user running the server interactively
    sees the warning even if the banner is delayed by import latency.
    """
    if not os.environ.get("POSTGRES_DSN"):
        print(
            "warning: POSTGRES_DSN not set; tool calls will fail until configured",
            file=sys.stderr,
            flush=True,
        )


@_cli.command()
def run(
    transport: str = typer.Option("stdio", help="MCP transport: stdio | http"),
    host: str = typer.Option("127.0.0.1", help="HTTP bind host."),
    port: int = typer.Option(8080, help="HTTP bind port."),
) -> None:
    """Run the Postgres DBA MCP server."""
    _warn_missing_env()
    if transport == "stdio":
        asyncio.run(mcp.run_stdio_async())
    elif transport == "http":
        asyncio.run(mcp.run_http_async(host=host, port=port))
    else:
        raise typer.BadParameter(f"unknown transport {transport!r}")


def main() -> None:  # pragma: no cover - thin wrapper
    _cli()


if __name__ == "__main__":  # pragma: no cover
    main()
