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
    """A scrape target as returned by `/api/v1/targets`.

    `health` is preserved verbatim from Prometheus (`up`, `down`,
    `unknown`) for active targets, or set to `"dropped"` for entries
    that came from `droppedTargets`. `origin` tells the caller which
    bucket the entry was in — relevant when `state="any"` returns the
    union of both lists.
    """

    job: str
    instance: str
    health: str  # up | down | unknown | dropped
    last_scrape: str | None
    last_error: str | None
    scrape_pool: str | None = None
    origin: str = "active"  # "active" | "dropped"


class BurnRateWindow(BaseModel):
    """One window of a multiwindow burn-rate calculation."""

    window: str  # e.g. "1h", "5m"
    burn_rate: float
    threshold: float
    breaching: bool


class MultiWindowBurnRate(BaseModel):
    """Multi-window, multi-burn-rate SLO alert evaluation.

    Models the Google SRE workbook recipe with both severity tiers:

    * **Page tier** — fast burn. Default: 14.4x over both 1h and 5m
      windows. Wakes the on-call.
    * **Ticket tier** — slow burn. Default: 6x over both 6h and 30m
      windows. Files a ticket; doesn't page.

    Both tiers fire only when *both* of their windows breach (the
    long-window catches sustained burns; the short-window keeps the
    alert from firing on a stale incident that has since recovered).
    `page` and `ticket` are independent; an incident might breach the
    ticket tier without ever reaching page-tier severity.
    """

    objective: float
    long_window: BurnRateWindow
    short_window: BurnRateWindow
    page: bool
    ticket_long_window: BurnRateWindow | None = None
    ticket_short_window: BurnRateWindow | None = None
    ticket: bool = False
