"""Pydantic models for the Kubernetes Inspector tools."""

from __future__ import annotations

from pydantic import BaseModel


class Namespace(BaseModel):
    name: str
    phase: str
    age_seconds: int


class Pod(BaseModel):
    namespace: str
    name: str
    phase: str
    node: str | None
    age_seconds: int
    restart_count: int
    ready: bool


class PodSpec(BaseModel):
    namespace: str
    name: str
    phase: str
    node: str | None
    containers: list[dict[str, object]]
    conditions: list[dict[str, object]]
    labels: dict[str, str]
    creation_timestamp: str | None


class LogLine(BaseModel):
    timestamp: str | None
    line: str
