"""Pure async functions wrapping Prometheus + Loki HTTP APIs.

Every function takes an `httpx.AsyncClient` so the tests can pump in a
mock transport. The server module wires these into FastMCP tools and
manages the client lifecycle.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from devops_mcp_bundle.observability.models import (
    LogEntry,
    PromSample,
    PromSeries,
)


def _check(resp: httpx.Response, what: str) -> dict[str, Any]:
    """Parse a Prometheus-style `{status, data, error}` envelope."""
    resp.raise_for_status()
    body: dict[str, Any] = resp.json()
    if body.get("status") != "success":
        raise RuntimeError(f"{what} failed: {body.get('error') or body}")
    return body


# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------


async def prom_query(client: httpx.AsyncClient, prom_url: str, promql: str) -> list[PromSeries]:
    """Run an instant Prometheus query."""
    if not promql.strip():
        raise ValueError("promql must not be blank")
    resp = await client.get(f"{prom_url}/api/v1/query", params={"query": promql})
    body = _check(resp, "prom_query")
    return _parse_prom_data(body["data"])


async def prom_range(
    client: httpx.AsyncClient,
    prom_url: str,
    promql: str,
    start: str,
    end: str,
    step: str = "15s",
) -> list[PromSeries]:
    """Run a range Prometheus query. `start`/`end` are RFC3339 or Unix epoch."""
    if not promql.strip():
        raise ValueError("promql must not be blank")
    resp = await client.get(
        f"{prom_url}/api/v1/query_range",
        params={"query": promql, "start": start, "end": end, "step": step},
    )
    body = _check(resp, "prom_range")
    return _parse_prom_data(body["data"])


def _parse_prom_data(data: dict[str, Any]) -> list[PromSeries]:
    """Normalise Prometheus `vector` and `matrix` result types into PromSeries."""
    rtype = data.get("resultType")
    series: list[PromSeries] = []
    for r in data.get("result", []):
        metric = dict(r.get("metric", {}))
        if rtype == "vector":
            ts, val = r["value"]
            samples = [PromSample(ts=float(ts), value=float(val))]
        elif rtype == "matrix":
            samples = [PromSample(ts=float(t), value=float(v)) for t, v in r["values"]]
        elif rtype == "scalar":
            ts, val = r if isinstance(r, list) else r["value"]
            samples = [PromSample(ts=float(ts), value=float(val))]
        else:
            samples = []
        series.append(PromSeries(metric=metric, samples=samples))
    return series


# ---------------------------------------------------------------------------
# Loki
# ---------------------------------------------------------------------------


async def loki_query(
    client: httpx.AsyncClient,
    loki_url: str,
    logql: str,
    since: str = "1h",
    limit: int = 100,
) -> list[LogEntry]:
    """Run a LogQL query against Loki's `/loki/api/v1/query_range` endpoint."""
    if not logql.strip():
        raise ValueError("logql must not be blank")
    if limit <= 0:
        raise ValueError("limit must be positive")

    end = dt.datetime.now(dt.UTC)
    start = end - _parse_duration(since)
    resp = await client.get(
        f"{loki_url}/loki/api/v1/query_range",
        params={
            "query": logql,
            "start": str(int(start.timestamp() * 1_000_000_000)),
            "end": str(int(end.timestamp() * 1_000_000_000)),
            "limit": str(limit),
            "direction": "backward",
        },
    )
    body = _check(resp, "loki_query")
    out: list[LogEntry] = []
    for stream in body["data"].get("result", []):
        labels = dict(stream.get("stream", {}))
        for entry in stream.get("values", []):
            ts_ns, line = entry
            out.append(LogEntry(timestamp_ns=int(ts_ns), line=line, stream=labels))
    out.sort(key=lambda e: e.timestamp_ns, reverse=True)
    return out[:limit]


def _parse_duration(s: str) -> dt.timedelta:
    """Parse `1h`, `30m`, `15s`, `2d` style durations into a timedelta."""
    s = s.strip()
    if not s:
        raise ValueError("duration must not be blank")
    suffix = s[-1]
    try:
        n = int(s[:-1])
    except ValueError as e:
        raise ValueError(f"invalid duration {s!r}") from e
    match suffix:
        case "s":
            return dt.timedelta(seconds=n)
        case "m":
            return dt.timedelta(minutes=n)
        case "h":
            return dt.timedelta(hours=n)
        case "d":
            return dt.timedelta(days=n)
        case _:
            raise ValueError(f"unknown duration suffix in {s!r}")


_DEFAULT_LONG_THRESHOLD = 14.4
_DEFAULT_SHORT_THRESHOLD = 14.4




