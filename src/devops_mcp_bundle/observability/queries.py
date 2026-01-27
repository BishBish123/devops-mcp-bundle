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
    Alert,
    BurnRateWindow,
    LogEntry,
    MultiWindowBurnRate,
    PromSample,
    PromSeries,
    SLOStatus,
    WindowDiff,
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


async def prom_alerts(client: httpx.AsyncClient, prom_url: str) -> list[Alert]:
    """List firing/pending alerts from Prometheus."""
    resp = await client.get(f"{prom_url}/api/v1/alerts")
    body = _check(resp, "prom_alerts")
    out: list[Alert] = []
    for a in body["data"].get("alerts", []):
        labels = dict(a.get("labels", {}))
        out.append(
            Alert(
                name=labels.get("alertname", ""),
                state=a.get("state", ""),
                severity=labels.get("severity"),
                summary=(a.get("annotations") or {}).get("summary"),
                started_at=a.get("activeAt"),
                labels=labels,
            )
        )
    return out


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


# ---------------------------------------------------------------------------
# Composite tools
# ---------------------------------------------------------------------------


async def slo_status(
    client: httpx.AsyncClient,
    prom_url: str,
    service: str,
    objective: float,
    success_query: str,
    total_query: str,
    window: str = "30d",
) -> SLOStatus:
    """Compute SLO actual + burn rate from caller-provided PromQL.

    `success_query` should evaluate to a number (rate of successful events
    over `window`); `total_query` likewise for total events. Common pattern:

        success_query = 'sum(rate(http_requests_total{code!~"5..", job="api"}[30d]))'
        total_query   = 'sum(rate(http_requests_total{job="api"}[30d]))'
    """
    if not 0 < objective < 1:
        raise ValueError("objective must be between 0 and 1 (e.g. 0.999)")

    success = await _instant_scalar(client, prom_url, success_query)
    total = await _instant_scalar(client, prom_url, total_query)
    actual = 0.0 if total <= 0 else success / total

    error_rate = 1.0 - actual
    allowed_error = 1.0 - objective
    error_budget_remaining = 1.0 - (error_rate / allowed_error) if allowed_error > 0 else 1.0
    burn_rate = error_rate / allowed_error if allowed_error > 0 else 0.0
    return SLOStatus(
        service=service,
        objective=objective,
        window=window,
        actual=actual,
        error_budget_remaining=error_budget_remaining,
        burn_rate=burn_rate,
    )


async def compare_windows(
    client: httpx.AsyncClient,
    prom_url: str,
    promql_a: str,
    promql_b: str,
    label_a: str = "now",
    label_b: str = "before",
) -> WindowDiff:
    """Run two PromQL expressions, return their delta + percent change.

    Useful for "is this metric different than it was an hour ago?" — the
    caller supplies both PromQL queries (typically one with `[5m] offset 1h`
    or similar) so the tool stays neutral about windowing semantics.
    """
    a = await _instant_scalar(client, prom_url, promql_a)
    b = await _instant_scalar(client, prom_url, promql_b)
    delta = a - b
    pct = (delta / b * 100.0) if b != 0 else None
    return WindowDiff(
        promql=f"a={promql_a!r} b={promql_b!r}",
        window_a_label=label_a,
        window_b_label=label_b,
        window_a_value=a,
        window_b_value=b,
        delta=delta,
        pct_change=pct,
    )


async def _instant_scalar(client: httpx.AsyncClient, prom_url: str, promql: str) -> float:
    series = await prom_query(client, prom_url, promql)
    if not series or not series[0].samples:
        return 0.0
    return float(series[0].samples[-1].value)


# Default burn-rate thresholds from the SRE workbook (page-worthy fast burns).
# 14.4x for 1h means: at this rate, the 30-day error budget is consumed in
# (30d / 14.4) ≈ 50h. Couple it with a 5m window so transient blips don't
# fire the page.
_DEFAULT_LONG_THRESHOLD = 14.4
_DEFAULT_SHORT_THRESHOLD = 14.4


async def multi_window_burn_rate(
    client: httpx.AsyncClient,
    prom_url: str,
    objective: float,
    long_burn_query: str,
    short_burn_query: str,
    long_window: str = "1h",
    short_window: str = "5m",
    long_threshold: float = _DEFAULT_LONG_THRESHOLD,
    short_threshold: float = _DEFAULT_SHORT_THRESHOLD,
) -> MultiWindowBurnRate:
    """Evaluate a two-window burn-rate alert.

    Returns ``page=True`` only when *both* windows exceed their thresholds —
    the canonical Google SRE workbook recipe. Caller supplies the PromQL
    for each burn rate (typically ``error_rate / (1 - objective)`` over the
    matching window); the helper just compares the values to thresholds.
    """
    if not 0 < objective < 1:
        raise ValueError("objective must be between 0 and 1 (e.g. 0.999)")
    if long_threshold <= 0 or short_threshold <= 0:
        raise ValueError("thresholds must be positive")

    long_val = await _instant_scalar(client, prom_url, long_burn_query)
    short_val = await _instant_scalar(client, prom_url, short_burn_query)
    long_breach = long_val >= long_threshold
    short_breach = short_val >= short_threshold
    return MultiWindowBurnRate(
        objective=objective,
        long_window=BurnRateWindow(
            window=long_window,
            burn_rate=long_val,
            threshold=long_threshold,
            breaching=long_breach,
        ),
        short_window=BurnRateWindow(
            window=short_window,
            burn_rate=short_val,
            threshold=short_threshold,
            breaching=short_breach,
        ),
        page=long_breach and short_breach,
    )




