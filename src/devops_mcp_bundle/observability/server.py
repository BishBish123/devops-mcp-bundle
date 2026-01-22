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
    PromSeries,
    SLOStatus,
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
