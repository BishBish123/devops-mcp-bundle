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
