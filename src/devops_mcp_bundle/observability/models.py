"""Pydantic models for the observability MCP tools."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PromSample(BaseModel):
    """One (timestamp, value) point in a Prometheus series."""

    ts: float = Field(description="Unix epoch seconds.")
    value: float


class PromSeries(BaseModel):
    metric: dict[str, str]
    samples: list[PromSample]


class Alert(BaseModel):
    name: str
    state: str  # firing | pending
    severity: str | None
    summary: str | None
    started_at: str | None
    labels: dict[str, str]


class LogEntry(BaseModel):
    timestamp_ns: int
    line: str
    stream: dict[str, str]


class SLOStatus(BaseModel):
    service: str
    objective: float = Field(description="Target SLO, e.g. 0.999.")
    window: str
    actual: float
    error_budget_remaining: float
    burn_rate: float


class WindowDiff(BaseModel):
    """Side-by-side comparison of one PromQL across two time windows."""

    promql: str
    window_a_label: str
    window_b_label: str
    window_a_value: float | None
    window_b_value: float | None
    delta: float | None
    pct_change: float | None


class Target(BaseModel):
    """A scrape target as returned by `/api/v1/targets`."""

    job: str
    instance: str
    health: str  # up | down | unknown
    last_scrape: str | None
    last_error: str | None
    scrape_pool: str | None = None


class BurnRateWindow(BaseModel):
    """One window of a multiwindow burn-rate calculation."""

    window: str  # e.g. "1h", "5m"
    burn_rate: float
    threshold: float
    breaching: bool


class MultiWindowBurnRate(BaseModel):
    """Multi-window, multi-burn-rate SLO alert evaluation.

    Models the Google SRE workbook recipe: alert only when *both* a long
    window and a short window are burning above the same threshold. The
    long window catches sustained burns; the short window keeps the alert
    from firing on a stale incident that has since recovered.
    """

    objective: float
    long_window: BurnRateWindow
    short_window: BurnRateWindow
    page: bool
