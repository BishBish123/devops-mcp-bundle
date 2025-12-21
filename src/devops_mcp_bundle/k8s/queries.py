"""Pure async functions wrapping kubernetes_asyncio API calls.

Split out of `server.py` so they're testable with a mocked client. The
server module is the thin FastMCP wrapper; the query module does the
actual API call + response shaping.

Read-only by design: every function here only calls `list_*`, `read_*`,
or watches that don't mutate state. There is intentionally no helper
for `delete`, `patch`, or `exec`.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any, cast

from devops_mcp_bundle.k8s.models import (
    Event,
    LogLine,
    Namespace,
    OOMKill,
    Pod,
    PodMetric,
    PodSpec,
)

if TYPE_CHECKING:  # pragma: no cover
    from kubernetes_asyncio.client import (
        CoreV1Api,
        CustomObjectsApi,
    )


def _age_seconds(creation_timestamp: dt.datetime | None) -> int:
    if creation_timestamp is None:
        return 0
    now = dt.datetime.now(dt.UTC)
    if creation_timestamp.tzinfo is None:
        creation_timestamp = creation_timestamp.replace(tzinfo=dt.UTC)
    return max(0, int((now - creation_timestamp).total_seconds()))


def _ts_iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.isoformat()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def list_namespaces(api: CoreV1Api) -> list[Namespace]:
    resp = await api.list_namespace()
    return [
        Namespace(
            name=ns.metadata.name,
            phase=(ns.status.phase if ns.status else "Unknown"),
            age_seconds=_age_seconds(ns.metadata.creation_timestamp),
        )
        for ns in resp.items
    ]


async def list_pods(api: CoreV1Api, namespace: str, label_selector: str | None = None) -> list[Pod]:
    resp = await api.list_namespaced_pod(namespace=namespace, label_selector=label_selector or "")
    return [_to_pod(p) for p in resp.items]


def _to_pod(p: Any) -> Pod:
    statuses = (p.status.container_statuses if p.status else None) or []
    restarts = sum(int(getattr(s, "restart_count", 0) or 0) for s in statuses)
    ready = bool(statuses) and all(getattr(s, "ready", False) for s in statuses)
    return Pod(
        namespace=p.metadata.namespace,
        name=p.metadata.name,
        phase=(p.status.phase if p.status else "Unknown"),
        node=(p.spec.node_name if p.spec else None),
        age_seconds=_age_seconds(p.metadata.creation_timestamp),
        restart_count=restarts,
        ready=ready,
    )


async def describe_pod(api: CoreV1Api, namespace: str, name: str) -> PodSpec:
    p = await api.read_namespaced_pod(name=name, namespace=namespace)
    containers: list[dict[str, object]] = [
        {
            "name": c.name,
            "image": c.image,
            "resources": getattr(c.resources, "to_dict", lambda: {})() if c.resources else {},
        }
        for c in (p.spec.containers if p.spec else [])
    ]
    conditions = [
        {
            "type": c.type,
            "status": c.status,
            "reason": getattr(c, "reason", None),
            "message": getattr(c, "message", None),
        }
        for c in ((p.status.conditions or []) if p.status else [])
    ]
    return PodSpec(
        namespace=p.metadata.namespace,
        name=p.metadata.name,
        phase=(p.status.phase if p.status else "Unknown"),
        node=(p.spec.node_name if p.spec else None),
        containers=containers,
        conditions=conditions,
        labels=dict(p.metadata.labels or {}),
        creation_timestamp=_ts_iso(p.metadata.creation_timestamp),
    )


async def pod_logs(
    api: CoreV1Api,
    namespace: str,
    name: str,
    container: str | None = None,
    tail: int = 200,
    redact_secrets: bool = True,
) -> list[LogLine]:
    if tail <= 0:
        raise ValueError("tail must be positive")
    # `container` is optional in the API even though the stub types it as
    # `str`; pass `""` to mean "default container".
    text = await api.read_namespaced_pod_log(
        name=name, namespace=namespace, container=container or "", tail_lines=tail
    )
    if not text:
        return []
    out: list[LogLine] = []
    for raw in text.splitlines():
        ts: str | None = None
        line = raw
        # If the cluster gave us RFC3339 timestamps via `--timestamps`, split
        # them out; otherwise leave the line whole.
        first, _, rest = raw.partition(" ")
        if first.endswith("Z") and "T" in first:
            try:
                dt.datetime.fromisoformat(first.replace("Z", "+00:00"))
                ts, line = first, rest
            except ValueError:
                pass
        if redact_secrets:
            line = redact_secrets_from_logs(line)
        out.append(LogLine(timestamp=ts, line=line))
    return out


async def pod_events(api: CoreV1Api, namespace: str, name: str) -> list[Event]:
    resp = await api.list_namespaced_event(
        namespace=namespace,
        field_selector=f"involvedObject.name={name}",
    )
    return [
        Event(
            type=e.type or "Normal",
            reason=e.reason or "",
            message=e.message or "",
            count=int(e.count or 0),
            last_seen=_ts_iso(e.last_timestamp or e.event_time),
            involved_object=f"{e.involved_object.kind}/{e.involved_object.name}",
        )
        for e in resp.items
    ]


async def top_pods(api: CustomObjectsApi, namespace: str) -> list[PodMetric]:
    """Read pod metrics from `metrics.k8s.io/v1beta1` (requires metrics-server).

    Returns [] when metrics-server is not installed (the API call raises),
    so callers can degrade gracefully.
    """
    try:
        resp = await api.list_namespaced_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=namespace,
            plural="pods",
        )
    except Exception:
        # metrics-server is genuinely optional; missing or transient errors
        # should leave the bench/server functional with empty metrics.
        return []
    metrics: list[PodMetric] = []
    for item in cast(dict[str, Any], resp).get("items", []):
        name = item["metadata"]["name"]
        cpu_m = 0
        mem = 0
        for c in item.get("containers", []):
            usage = c.get("usage", {})
            cpu_m += _parse_cpu(usage.get("cpu", "0"))
            mem += _parse_memory(usage.get("memory", "0"))
        metrics.append(PodMetric(name=name, cpu_millicores=cpu_m, memory_bytes=mem))
    return metrics


_MEMORY_UNITS: dict[str, int] = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}


def _parse_cpu(value: str) -> int:
    """Convert a Kubernetes CPU quantity (`100m`, `1`, `0.5`) to millicores."""
    s = value.strip()
    if not s:
        return 0
    if s.endswith("n"):  # nanocores
        return max(0, round(int(s[:-1]) / 1_000_000))
    if s.endswith("u"):  # microcores
        return max(0, round(int(s[:-1]) / 1_000))
    if s.endswith("m"):
        return int(s[:-1])
    return round(float(s) * 1000)


def _parse_memory(value: str) -> int:
    """Convert a Kubernetes memory quantity (`128Mi`, `1Gi`) to bytes."""
    s = value.strip()
    if not s:
        return 0
    for suffix, factor in _MEMORY_UNITS.items():
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * factor)
    return int(float(s))


_SECRET_KEY_HINTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "access_key",
    "private_key",
    "credential",
    "auth",
    "ssh",
    "cert",
)


def _looks_like_secret_key(key: str) -> bool:
    k = key.lower().replace("-", "").replace("_", "")
    return any(hint.replace("_", "") in k for hint in _SECRET_KEY_HINTS)


async def namespace_events(
    api: CoreV1Api,
    namespace: str,
    only_warnings: bool = True,
    since_min: int | None = None,
) -> list[Event]:
    """Return cluster events in `namespace`, by default Warning-only.

    `pod_events` is scoped to one object; this helper is the cluster-wide
    sweep — useful for "what's been going wrong in `prod` for the last
    hour?". `only_warnings=False` includes Normal events too.
    """
    if since_min is not None and since_min <= 0:
        raise ValueError("since_min must be positive when set")

    field_selector = "type=Warning" if only_warnings else ""
    resp = await api.list_namespaced_event(
        namespace=namespace, field_selector=field_selector
    )
    cutoff = (
        dt.datetime.now(dt.UTC) - dt.timedelta(minutes=since_min)
        if since_min
        else None
    )
    out: list[Event] = []
    for e in resp.items:
        when = e.last_timestamp or e.event_time
        if cutoff is not None:
            if when is None:
                continue
            ts = when if when.tzinfo else when.replace(tzinfo=dt.UTC)
            if ts < cutoff:
                continue
        out.append(
            Event(
                type=e.type or "Normal",
                reason=e.reason or "",
                message=e.message or "",
                count=int(e.count or 0),
                last_seen=_ts_iso(when),
                involved_object=f"{e.involved_object.kind}/{e.involved_object.name}",
            )
        )
    return out


def _parse_quantity(value: str) -> float:
    """Parse any Kubernetes quantity (CPU, memory, count) to a float.

    `_parse_cpu` and `_parse_memory` produce integers in their native
    units; for headroom we only need a comparable scalar, so we lower
    everything through this generic parser.
    """
    s = value.strip()
    if not s:
        return 0.0
    # Try cpu / memory first (they have well-known suffixes).
    for suffix in _MEMORY_UNITS:
        if s.endswith(suffix):
            return float(_parse_memory(s))
    if s.endswith(("m", "n", "u")):
        return float(_parse_cpu(s))
    return float(s)


def redact_secrets_from_logs(line: str) -> str:
    """Best-effort masking for `key=value` and `key: value` shaped secrets.

    Not a security boundary — anyone calling `kubectl logs` directly
    sees everything. This helper exists so a chat agent doesn't echo a
    bearer token *back to its own context window* and then quote it in
    the report. Conservative: leave the line alone unless we recognise
    the key as secret-shaped.

    Handles two shapes:

    * ``key=value`` — single token, partitioned in place.
    * ``key: value`` — two tokens (the colon stays attached to the key);
      we look ahead one token to redact the value.
    """
    if "=" not in line and ":" not in line:
        return line
    tokens = line.split()
    out: list[str] = []
    skip_next = False
    for i, tok in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        # `key=value` — partition once.
        if "=" in tok:
            k, _, v = tok.partition("=")
            if v and _looks_like_secret_key(k):
                out.append(f"{k}=<redacted>")
                continue
        # `key:` plus following value token.
        if tok.endswith(":") and len(tok) > 1:
            k = tok[:-1]
            if _looks_like_secret_key(k) and i + 1 < len(tokens):
                out.append(f"{k}:<redacted>")
                skip_next = True
                continue
        # `key:value` glued together.
        if ":" in tok and not tok.endswith(":"):
            k, _, v = tok.partition(":")
            if v and _looks_like_secret_key(k):
                out.append(f"{k}:<redacted>")
                continue
        out.append(tok)
    return " ".join(out)


async def recent_oomkills(api: CoreV1Api, namespace: str, since_min: int = 60) -> list[OOMKill]:
    """Return Warning events whose reason contains 'OOMKill' within `since_min`."""
    if since_min <= 0:
        raise ValueError("since_min must be positive")
    resp = await api.list_namespaced_event(namespace=namespace, field_selector="type=Warning")
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=since_min)
    out: list[OOMKill] = []
    for e in resp.items:
        reason = (e.reason or "").lower()
        if "oom" not in reason and "outofmemory" not in reason:
            continue
        when = e.last_timestamp or e.event_time
        if when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        if when < cutoff:
            continue
        out.append(
            OOMKill(
                namespace=namespace,
                pod=e.involved_object.name,
                container=getattr(e.involved_object, "field_path", "") or "",
                timestamp=_ts_iso(when) or "",
                reason=e.reason or "OOMKilled",
            )
        )
    return out
