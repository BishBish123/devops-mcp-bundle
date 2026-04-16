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
import re
from typing import TYPE_CHECKING, Any, cast

from devops_mcp_bundle.k8s.models import (
    ConfigMapInfo,
    Event,
    LogLine,
    Namespace,
    OOMKill,
    Pod,
    PodMetric,
    PodSpec,
    ResourceQuotaInfo,
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


async def list_configmaps(api: CoreV1Api, namespace: str) -> list[ConfigMapInfo]:
    """List ConfigMaps in `namespace`, returning only key names (no values).

    A ConfigMap is the wrong place to put a secret — but the cluster
    will happily accept one. The agent doesn't need the value to triage
    "is the right config mounted?", just whether the key is present.
    Keys that *look* like secrets are reported in `redacted_keys` so a
    reviewer can spot accidental sensitive data without it ever
    crossing the wire to the LLM.
    """
    resp = await api.list_namespaced_config_map(namespace=namespace)
    out: list[ConfigMapInfo] = []
    for cm in resp.items:
        keys = list((cm.data or {}).keys()) + list((cm.binary_data or {}).keys())
        keys.sort()
        redacted = [k for k in keys if _looks_like_secret_key(k)]
        out.append(
            ConfigMapInfo(
                namespace=cm.metadata.namespace,
                name=cm.metadata.name,
                keys=keys,
                redacted_keys=redacted,
            )
        )
    return out


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
    resp = await api.list_namespaced_event(namespace=namespace, field_selector=field_selector)
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=since_min) if since_min else None
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


async def resource_quotas(api: CoreV1Api, namespace: str) -> list[ResourceQuotaInfo]:
    """List ResourceQuotas in `namespace` with computed per-resource headroom."""
    resp = await api.list_namespaced_resource_quota(namespace=namespace)
    out: list[ResourceQuotaInfo] = []
    for q in resp.items:
        hard = {k: str(v) for k, v in (q.spec.hard or {}).items()} if q.spec else {}
        used = {k: str(v) for k, v in (q.status.used or {}).items()} if q.status else {}
        headroom: dict[str, float] = {}
        for k, hard_val in hard.items():
            try:
                h = _parse_quantity(hard_val)
                u = _parse_quantity(used.get(k, "0"))
            except ValueError:
                continue
            if h <= 0:
                continue
            headroom[k] = max(0.0, 1.0 - (u / h))
        out.append(
            ResourceQuotaInfo(
                namespace=q.metadata.namespace,
                name=q.metadata.name,
                hard=hard,
                used=used,
                headroom=headroom,
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


# Secret-key alternation: covers password / pwd / passwd, secret, token,
# api_key / apiKey / api-key, access_key, auth / authorization, bearer,
# credentials, private_key, client_secret. Case-insensitive at use site.
_SECRET_KEY_PATTERN = (
    r"(?:p(?:ass)?w(?:or)?d|pass(?:wd)?|secret|tokens?|"  # noqa: S105 — regex
    r"api[_-]?keys?|access[_-]?keys?|"
    r"auth(?:oriz(?:ation|ed))?|bearer|cred(?:ential)?s?|"
    r"priv(?:ate)?[_-]?keys?|client[_-]?secrets?)"
)

# Capture the *full* compound key (so `DB_PASSWORD` and `apiKey` are matched
# as units), the literal operator + spacing, the value, and any matching
# pair of surrounding quotes. The `(?:^|(?<=[...]))` anchor allows the key
# to start at the beginning of the line, after whitespace, or after one of
# the common delimiters that bound a key in a log line — but *not* after
# another letter (so we don't mistake "the password algorithm" for a kv
# pair, since there's no `:` or `=` directly after the word).
_REDACT_KV_RE = re.compile(
    r"(?ix)"
    r"(?:^|(?<=[\s_\-/.,;\[\]\{\}\(\)]))"
    rf"(?P<key>[A-Za-z0-9_-]*?{_SECRET_KEY_PATTERN})"
    r"(?P<sep>\s*[:=]\s*)"
    # Don't re-match when the bearer pass already handled the value — the
    # remaining `Bearer <REDACTED>` is preserved verbatim.
    r"(?!Bearer\b)"
    r"(?P<q>[\"']?)"
    r"(?P<val>[^\s\"']+)"
    r"(?P=q)"
)

# `Authorization: Bearer <token>` and standalone `Bearer <token>`.
# Optional `Authorization` header prefix is consumed as part of the match so
# the kv regex can't separately fire on it and clobber `Bearer`.
_REDACT_BEARER_RE = re.compile(
    r"(?i)"
    r"(?P<prefix>(?:Authorization\s*[:=]\s*)?)"
    r"\b(?P<scheme>Bearer)\s+(?P<token>[A-Za-z0-9._\-+/=]+)"
)


def redact_secrets_from_logs(line: str) -> str:
    """Best-effort masking for `key=value` and `key: value` shaped secrets.

    Not a security boundary — anyone calling `kubectl logs` directly
    sees everything. This helper exists so a chat agent doesn't echo a
    bearer token *back to its own context window* and then quote it in
    the report. Conservative: leave the line alone unless we recognise
    the key as secret-shaped *and* a real value follows the operator.

    Handles, case-insensitively, with optional spaces around the op and
    optional surrounding quotes:

    * ``key=value``, ``key = value``
    * ``key: value``, ``key:value``
    * ``Bearer <token>`` / ``Authorization: Bearer <token>``

    Does *not* redact `if password is None:` (no value after the op) or
    `the password algorithm is bcrypt` (no op directly after the key).
    """
    if "=" not in line and ":" not in line and "bearer" not in line.lower():
        return line

    def _kv_sub(m: re.Match[str]) -> str:
        q = m.group("q")
        return f"{m.group('key')}{m.group('sep')}{q}<REDACTED>{q}"

    def _bearer_sub(m: re.Match[str]) -> str:
        return f"{m.group('prefix')}{m.group('scheme')} <REDACTED>"

    out = _REDACT_BEARER_RE.sub(_bearer_sub, line)
    out = _REDACT_KV_RE.sub(_kv_sub, out)
    return out


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
