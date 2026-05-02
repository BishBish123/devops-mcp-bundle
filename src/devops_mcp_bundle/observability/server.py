"""FastMCP entry point for the observability MCP server.

Reads `PROMETHEUS_URL` and `LOKI_URL` from the environment.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import typer
from fastmcp import FastMCP

from devops_mcp_bundle.observability import queries
from devops_mcp_bundle.observability.models import (
    Alert,
    LogEntry,
    MultiWindowBurnRate,
    PromSeries,
    SLOStatus,
    Target,
    WindowDiff,
)

mcp: FastMCP = FastMCP(
    name="observability",
    instructions=(
        "Prometheus + Loki query tools. prom_query / prom_range / prom_alerts "
        "for metrics, loki_query for logs, slo_status + compare_windows for "
        "higher-level analysis built on top of caller-supplied PromQL."
    ),
)


def _prom_url() -> str:
    url = os.environ.get("PROMETHEUS_URL")
    if not url:
        raise RuntimeError("PROMETHEUS_URL env var is required")
    return url.rstrip("/")


def _loki_url() -> str:
    url = os.environ.get("LOKI_URL")
    if not url:
        raise RuntimeError("LOKI_URL env var is required")
    return url.rstrip("/")


@asynccontextmanager
async def _client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        yield client


@mcp.tool
async def prom_query(promql: str) -> list[PromSeries]:
    """Run an instant PromQL query."""
    async with _client() as c:
        return await queries.prom_query(c, _prom_url(), promql)


@mcp.tool
async def prom_range(promql: str, start: str, end: str, step: str = "15s") -> list[PromSeries]:
    """Run a range PromQL query (RFC3339 or Unix epoch start/end)."""
    async with _client() as c:
        return await queries.prom_range(c, _prom_url(), promql, start, end, step)


@mcp.tool
async def prom_alerts() -> list[Alert]:
    """List firing/pending alerts from Prometheus."""
    async with _client() as c:
        return await queries.prom_alerts(c, _prom_url())


@mcp.tool
async def prom_targets(state: str = "active", limit: int | None = None) -> list[Target]:
    """List Prometheus scrape targets (active|dropped|any) with health + last error.

    Pass ``limit`` to truncate to the first N entries (capped at
    ``MAX_PROM_TARGETS``). With no limit the result is bounded by
    ``MAX_PROM_TARGETS`` and exceeding it raises — large clusters with a
    busy `kubernetes-pods` discovery can produce tens of thousands of
    dropped entries.
    """
    async with _client() as c:
        return await queries.prom_targets(c, _prom_url(), state=state, limit=limit)


@mcp.tool
async def multi_window_burn_rate(
    objective: float,
    long_burn_query: str,
    short_burn_query: str,
    long_window: str = "1h",
    short_window: str = "5m",
    long_threshold: float = 14.4,
    short_threshold: float = 14.4,
    ticket_long_burn_query: str | None = None,
    ticket_short_burn_query: str | None = None,
    ticket_long_window: str = "6h",
    ticket_short_window: str = "30m",
    ticket_long_threshold: float = 6.0,
    ticket_short_threshold: float = 6.0,
) -> MultiWindowBurnRate:
    """Evaluate a Google SRE-workbook two-tier burn-rate alert.

    Page tier (always evaluated): fires when *both* the long and short
    page windows exceed the page threshold (14.4x default for a 99.9 %
    SLO). Defaults match the workbook: 1h + 5m, threshold 14.4.

    Ticket tier (optional): pass ``ticket_long_burn_query`` and
    ``ticket_short_burn_query`` to enable it. Fires when *both* the
    long and short ticket windows exceed the ticket threshold (6x
    default). Defaults: 6h + 30m, threshold 6.0. If either ticket
    query is omitted, the ticket tier is skipped and the result has
    ``ticket=False`` with no ticket-window data.
    """
    async with _client() as c:
        return await queries.multi_window_burn_rate(
            c,
            _prom_url(),
            objective=objective,
            long_burn_query=long_burn_query,
            short_burn_query=short_burn_query,
            long_window=long_window,
            short_window=short_window,
            long_threshold=long_threshold,
            short_threshold=short_threshold,
            ticket_long_burn_query=ticket_long_burn_query,
            ticket_short_burn_query=ticket_short_burn_query,
            ticket_long_window=ticket_long_window,
            ticket_short_window=ticket_short_window,
            ticket_long_threshold=ticket_long_threshold,
            ticket_short_threshold=ticket_short_threshold,
        )


@mcp.tool
async def loki_query(logql: str, since: str = "1h", limit: int = 100) -> list[LogEntry]:
    """Run a LogQL query against Loki, looking back `since`."""
    async with _client() as c:
        return await queries.loki_query(c, _loki_url(), logql, since=since, limit=limit)


@mcp.tool
async def slo_status(
    service: str,
    objective: float,
    success_query: str,
    total_query: str,
    window: str = "30d",
) -> SLOStatus:
    """Compute SLO attainment + burn rate from PromQL the caller supplies."""
    async with _client() as c:
        return await queries.slo_status(
            c, _prom_url(), service, objective, success_query, total_query, window
        )


@mcp.tool
async def compare_windows(
    promql_a: str,
    promql_b: str,
    label_a: str = "now",
    label_b: str = "before",
) -> WindowDiff:
    """Compute delta + pct change between two PromQL expressions."""
    async with _client() as c:
        return await queries.compare_windows(c, _prom_url(), promql_a, promql_b, label_a, label_b)


@mcp.tool
def escape_logql_label(value: str) -> str:
    """Escape a string value for safe use inside a LogQL label matcher.

    Replaces ``"``, ``\\``, newlines, carriage returns, and tabs with their
    backslash-escape equivalents so the result can be embedded inside
    ``{label="<value>"}`` without breaking out of the matcher. This is the
    only correct way to put an untrusted dynamic value into a LogQL query.

    Example::

        label = escape_logql_label('my"value')
        query = f'{{app="{label}"}}'
        # -> '{app="my\\"value"}'
    """
    return queries.escape_logql_label(value)


@mcp.tool
def render_logql(template: str, labels: dict[str, str]) -> str:
    r"""Render a LogQL template with escaped label values.

    Every value in ``labels`` is run through :func:`escape_logql_label`
    before substitution, so callers can pass untrusted values without
    worrying about injection. Template placeholders use Python
    ``str.format`` syntax: ``{name}`` for each key in ``labels``.

    **Critical**: LogQL uses literal ``{`` and ``}`` for label-matcher
    blocks. Because `str.format` reserves single braces for placeholders,
    every literal brace in the template **must be doubled**: ``{{`` for a
    literal ``{``, ``}}`` for a literal ``}``.

    Example::

        query = render_logql(
            '{{app="{app}", container="{container}"}} |= "{needle}"',
            labels={"app": "api", "container": "web", "needle": 'oh "no"'},
        )
        # -> '{app="api", container="web"} |= "oh \\"no\\""'

    Raises ``ValueError`` if any key is not a valid LogQL label identifier
    (``[a-zA-Z_][a-zA-Z0-9_]*``).
    """
    return queries.render_logql(template, **labels)


_cli = typer.Typer(name="mcp-observability", add_completion=False)


@_cli.command()
def run(
    transport: str = typer.Option("stdio", help="MCP transport: stdio | http"),
    host: str = typer.Option("127.0.0.1", help="HTTP bind host."),
    port: int = typer.Option(8082, help="HTTP bind port."),
) -> None:
    """Run the observability MCP server."""
    if transport == "stdio":
        asyncio.run(mcp.run_stdio_async())
    elif transport == "http":
        asyncio.run(mcp.run_http_async(host=host, port=port))
    else:
        raise typer.BadParameter(f"unknown transport {transport!r}")


def main() -> None:  # pragma: no cover
    _cli()


if __name__ == "__main__":  # pragma: no cover
    main()
