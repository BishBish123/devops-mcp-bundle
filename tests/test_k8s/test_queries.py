"""Unit tests for the Kubernetes query helpers using a mocked API client."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from kubernetes_asyncio.client.rest import ApiException

from devops_mcp_bundle.k8s import queries


def _ns(**kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


def _utc(secs_ago: int = 0) -> dt.datetime:
    return dt.datetime.now(dt.UTC) - dt.timedelta(seconds=secs_ago)


# ---------------------------------------------------------------------------
# CPU + memory parsers
# ---------------------------------------------------------------------------


class TestParseCpu:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("100m", 100),
            ("1", 1000),
            ("0.5", 500),
            ("1500000n", 2),  # 1500000 nanocores ≈ 2 millicores
            ("1500u", 2),  # 1500 microcores ≈ 2 millicores
            ("", 0),
        ],
    )
    def test_known_values(self, value: str, expected: int) -> None:
        assert queries._parse_cpu(value) == expected


class TestParseMemory:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("128Mi", 128 * 1024**2),
            ("1Gi", 1024**3),
            ("500K", 500_000),
            ("0", 0),
            ("", 0),
            ("1024", 1024),  # raw bytes
        ],
    )
    def test_known_values(self, value: str, expected: int) -> None:
        assert queries._parse_memory(value) == expected


# ---------------------------------------------------------------------------
# list_namespaces / list_pods
# ---------------------------------------------------------------------------


class TestListNamespaces:
    async def test_basic(self) -> None:
        api = AsyncMock()
        api.list_namespace.return_value = _ns(
            items=[
                _ns(
                    metadata=_ns(name="default", creation_timestamp=_utc(60)),
                    status=_ns(phase="Active"),
                ),
                _ns(
                    metadata=_ns(name="kube-system", creation_timestamp=None),
                    status=None,
                ),
            ]
        )
        result = await queries.list_namespaces(api)
        assert [n.name for n in result] == ["default", "kube-system"]
        assert result[0].phase == "Active"
        assert result[1].phase == "Unknown"
        assert result[0].age_seconds >= 60
        assert result[1].age_seconds == 0


class TestListPods:
    async def test_includes_restart_count_and_ready(self) -> None:
        api = AsyncMock()
        api.list_namespaced_pod.return_value = _ns(
            items=[
                _ns(
                    metadata=_ns(
                        namespace="default",
                        name="web-0",
                        creation_timestamp=_utc(30),
                    ),
                    spec=_ns(node_name="node-1"),
                    status=_ns(
                        phase="Running",
                        container_statuses=[
                            _ns(restart_count=2, ready=True),
                            _ns(restart_count=0, ready=True),
                        ],
                    ),
                ),
                _ns(
                    metadata=_ns(
                        namespace="default",
                        name="web-1",
                        creation_timestamp=_utc(30),
                    ),
                    spec=_ns(node_name=None),
                    status=_ns(
                        phase="CrashLoopBackOff",
                        container_statuses=[_ns(restart_count=5, ready=False)],
                    ),
                ),
            ]
        )
        result = await queries.list_pods(api, "default", label_selector="app=web")
        api.list_namespaced_pod.assert_awaited_once_with(
            namespace="default", label_selector="app=web", _request_timeout=30
        )
        assert [p.name for p in result] == ["web-0", "web-1"]
        assert result[0].restart_count == 2
        assert result[0].ready is True
        assert result[1].ready is False

    async def test_no_label_selector_defaults_to_empty(self) -> None:
        api = AsyncMock()
        api.list_namespaced_pod.return_value = _ns(items=[])
        await queries.list_pods(api, "default")
        api.list_namespaced_pod.assert_awaited_once_with(namespace="default", label_selector="", _request_timeout=30)


# ---------------------------------------------------------------------------
# pod_logs
# ---------------------------------------------------------------------------


class TestPodLogs:
    async def test_splits_lines(self) -> None:
        api = AsyncMock()
        api.read_namespaced_pod_log.return_value = "line one\nline two\nline three"
        out = await queries.pod_logs(api, "default", "web-0", tail=10)
        assert [line.line for line in out] == ["line one", "line two", "line three"]
        assert all(line.timestamp is None for line in out)

    async def test_extracts_rfc3339_timestamp(self) -> None:
        api = AsyncMock()
        api.read_namespaced_pod_log.return_value = (
            "2026-04-29T03:00:00Z hello world\n2026-04-29T03:00:01Z second line"
        )
        out = await queries.pod_logs(api, "default", "web-0", tail=10)
        assert out[0].timestamp == "2026-04-29T03:00:00Z"
        assert out[0].line == "hello world"
        assert out[1].timestamp == "2026-04-29T03:00:01Z"

    async def test_empty_returns_empty_list(self) -> None:
        api = AsyncMock()
        api.read_namespaced_pod_log.return_value = ""
        assert await queries.pod_logs(api, "default", "web-0") == []

    async def test_tail_zero_rejected(self) -> None:
        api = AsyncMock()
        with pytest.raises(ValueError, match="tail"):
            await queries.pod_logs(api, "default", "web-0", tail=0)

    async def test_pod_logs_rejects_oversized_tail(self) -> None:
        # Hard cap. A caller asking for `tail=1_000_000` would burn
        # agent context, force a multi-megabyte response shape through
        # MCP, and hit kube-apiserver rate limits in the middle of an
        # incident. Reject before the API client is even invoked.
        api = AsyncMock()
        with pytest.raises(ValueError, match=str(queries.MAX_POD_LOG_TAIL)):
            await queries.pod_logs(api, "default", "web-0", tail=queries.MAX_POD_LOG_TAIL + 1)
        api.read_namespaced_pod_log.assert_not_awaited()

    async def test_pod_logs_at_cap_allowed(self) -> None:
        # Exactly at the cap is fine — the rejection is strict-greater.
        api = AsyncMock()
        api.read_namespaced_pod_log.return_value = ""
        out = await queries.pod_logs(api, "default", "web-0", tail=queries.MAX_POD_LOG_TAIL)
        assert out == []
        api.read_namespaced_pod_log.assert_awaited_once()


# ---------------------------------------------------------------------------
# pod_events
# ---------------------------------------------------------------------------


class TestPodEvents:
    async def test_filters_by_involved_object(self) -> None:
        api = AsyncMock()
        api.list_namespaced_event.return_value = _ns(
            items=[
                _ns(
                    type="Warning",
                    reason="BackOff",
                    message="Back-off restarting failed container",
                    count=3,
                    last_timestamp=_utc(60),
                    event_time=None,
                    involved_object=_ns(kind="Pod", name="web-0"),
                ),
                _ns(
                    type="Normal",
                    reason="Scheduled",
                    message="Successfully assigned to node",
                    count=1,
                    last_timestamp=None,
                    event_time=_utc(120),
                    involved_object=_ns(kind="Pod", name="web-0"),
                ),
            ]
        )
        out = await queries.pod_events(api, "default", "web-0")
        api.list_namespaced_event.assert_awaited_once_with(
            namespace="default", field_selector="involvedObject.name=web-0", _request_timeout=30
        )
        assert [e.reason for e in out] == ["BackOff", "Scheduled"]
        assert out[0].type == "Warning"
        assert out[0].count == 3


# ---------------------------------------------------------------------------
# top_pods
# ---------------------------------------------------------------------------


class TestTopPods:
    async def test_aggregates_container_usage(self) -> None:
        api = AsyncMock()
        api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "web-0"},
                    "containers": [
                        {"usage": {"cpu": "200m", "memory": "256Mi"}},
                        {"usage": {"cpu": "100m", "memory": "128Mi"}},
                    ],
                }
            ]
        }
        out = await queries.top_pods(api, "default")
        assert out[0].name == "web-0"
        assert out[0].cpu_millicores == 300
        assert out[0].memory_bytes == 384 * 1024**2

    async def test_metrics_server_missing_returns_empty(self) -> None:
        # 404 from the metrics.k8s.io endpoint == metrics-server not
        # installed; the helper degrades to [] so the bench/server stays
        # functional when the cluster doesn't expose pod metrics.
        api = AsyncMock()
        api.list_namespaced_custom_object.side_effect = ApiException(status=404, reason="Not Found")
        assert await queries.top_pods(api, "default") == []

    async def test_rbac_denied_propagates(self) -> None:
        # An RBAC 403 is *not* a "metrics-server is missing" signal — it
        # means the service account is misconfigured and the operator
        # needs to know. The earlier blanket `except Exception` swallowed
        # this and returned [], hiding the misconfiguration.
        api = AsyncMock()
        api.list_namespaced_custom_object.side_effect = ApiException(status=403, reason="Forbidden")
        with pytest.raises(ApiException):
            await queries.top_pods(api, "default")

    async def test_unexpected_error_propagates(self) -> None:
        # Anything that isn't an ApiException(404) propagates so the
        # caller sees the actual failure mode.
        api = AsyncMock()
        api.list_namespaced_custom_object.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            await queries.top_pods(api, "default")


# ---------------------------------------------------------------------------
# recent_oomkills
# ---------------------------------------------------------------------------


class TestRecentOomkills:
    async def test_filters_by_reason_and_age(self) -> None:
        api = AsyncMock()
        api.list_namespaced_event.return_value = _ns(
            items=[
                _ns(  # in window, oom
                    type="Warning",
                    reason="OOMKilling",
                    message="...",
                    last_timestamp=_utc(60),
                    event_time=None,
                    involved_object=_ns(kind="Pod", name="web-0", field_path="containers/app"),
                ),
                _ns(  # out of window
                    type="Warning",
                    reason="OOMKilling",
                    message="...",
                    last_timestamp=_utc(60 * 60 * 2),
                    event_time=None,
                    involved_object=_ns(kind="Pod", name="web-1", field_path=""),
                ),
                _ns(  # in window, not oom
                    type="Warning",
                    reason="BackOff",
                    message="...",
                    last_timestamp=_utc(30),
                    event_time=None,
                    involved_object=_ns(kind="Pod", name="web-2", field_path=""),
                ),
            ]
        )
        out = await queries.recent_oomkills(api, "default", since_min=10)
        assert len(out) == 1
        assert out[0].pod == "web-0"
        assert out[0].reason == "OOMKilling"

    async def test_zero_since_rejected(self) -> None:
        api = AsyncMock()
        with pytest.raises(ValueError, match="since_min"):
            await queries.recent_oomkills(api, "default", since_min=0)


# ---------------------------------------------------------------------------
# FIX 4 — namespace_events respects limit + passes it to the API
# ---------------------------------------------------------------------------


class TestNamespaceEventsLimit:
    def _make_event(self, name: str, secs_ago: int = 30) -> object:
        return _ns(
            type="Warning",
            reason="BackOff",
            message="container back-off",
            count=1,
            last_timestamp=_utc(secs_ago),
            event_time=None,
            involved_object=_ns(kind="Pod", name=name),
        )

    async def test_namespace_events_respects_limit(self) -> None:
        # 5 events, limit=3 — upstream returns all 5, the post-fetch check
        # fires because 5 > 3 and the API was asked for at most 3.
        api = AsyncMock()
        api.list_namespaced_event.return_value = _ns(
            items=[self._make_event(f"pod-{i}") for i in range(5)]
        )
        with pytest.raises(ValueError, match="exceeds limit"):
            await queries.namespace_events(api, "default", limit=3)

    async def test_namespace_events_passes_limit_to_api(self) -> None:
        # The limit kwarg must flow through to list_namespaced_event so
        # the kube-apiserver can do server-side capping. If it is dropped,
        # the entire event retention window arrives before the Python-side
        # check can fire.
        api = AsyncMock()
        api.list_namespaced_event.return_value = _ns(items=[])
        await queries.namespace_events(api, "default", limit=50)
        call_kwargs = api.list_namespaced_event.call_args.kwargs
        assert call_kwargs.get("limit") == 50

    async def test_namespace_events_passes_request_timeout(self) -> None:
        api = AsyncMock()
        api.list_namespaced_event.return_value = _ns(items=[])
        await queries.namespace_events(api, "default")
        call_kwargs = api.list_namespaced_event.call_args.kwargs
        assert call_kwargs.get("_request_timeout") == 30

    async def test_namespace_events_limit_exceeds_max_rejected(self) -> None:
        api = AsyncMock()
        with pytest.raises(ValueError, match="MAX_K8S_EVENTS"):
            await queries.namespace_events(api, "default", limit=queries.MAX_K8S_EVENTS + 1)
