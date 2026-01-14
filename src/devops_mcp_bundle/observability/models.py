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
