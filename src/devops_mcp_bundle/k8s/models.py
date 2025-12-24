"""Pydantic models for the Kubernetes Inspector tools."""

from __future__ import annotations

from pydantic import BaseModel, Field


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


class Event(BaseModel):
    type: str = Field(description="Normal | Warning")
    reason: str
    message: str
    count: int
    last_seen: str | None
    involved_object: str


class PodMetric(BaseModel):
    name: str
    cpu_millicores: int
    memory_bytes: int


class OOMKill(BaseModel):
    namespace: str
    pod: str
    container: str
    timestamp: str
    reason: str


class ConfigMapInfo(BaseModel):
    """A ConfigMap with secret-shaped keys redacted.

    The helper that builds these (`list_configmaps`) deliberately drops
    the values: a ConfigMap is the wrong place to put secrets, but
    plenty of teams do, and we'd rather not surface "DB_PASSWORD" to a
    chat agent.
    """

    namespace: str
    name: str
    keys: list[str]
    redacted_keys: list[str] = Field(
        default_factory=list,
        description="Keys whose names matched a secret-ish heuristic.",
    )


class ResourceQuotaInfo(BaseModel):
    """Subset of a `ResourceQuota` plus computed headroom.

    `usage / hard` lets the agent flag "you're at 95% of pod count
    quota" without having to do the math itself.
    """

    namespace: str
    name: str
    hard: dict[str, str]
    used: dict[str, str]
    headroom: dict[str, float] = Field(
        description="Per-resource (1 - used/hard), 0..1; missing if hard is unset.",
    )
