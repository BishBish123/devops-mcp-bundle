"""Tests for the K8s helpers added after the initial scaffold:
ConfigMap listing, namespace-wide events, and ResourceQuota headroom.

Same `AsyncMock` pattern as `test_queries.py` — no live cluster needed.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from devops_mcp_bundle.k8s import queries


def _ns(**kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


def _utc(secs_ago: int = 0) -> dt.datetime:
    return dt.datetime.now(dt.UTC) - dt.timedelta(seconds=secs_ago)


# ---------------------------------------------------------------------------
# list_configmaps
# ---------------------------------------------------------------------------


class TestListConfigmaps:
    async def test_keys_returned_values_dropped(self) -> None:
        api = AsyncMock()
        api.list_namespaced_config_map.return_value = _ns(
            items=[
                _ns(
                    metadata=_ns(namespace="prod", name="app-config"),
                    data={"LOG_LEVEL": "debug", "DB_PASSWORD": "hunter2"},
                    binary_data=None,
                ),
            ]
        )
        out = await queries.list_configmaps(api, "prod")
        assert out[0].name == "app-config"
        # Keys are sorted, values not surfaced.
        assert out[0].keys == ["DB_PASSWORD", "LOG_LEVEL"]
        # Only the secret-shaped key gets flagged.
        assert out[0].redacted_keys == ["DB_PASSWORD"]

    async def test_empty_data_handled(self) -> None:
        api = AsyncMock()
        api.list_namespaced_config_map.return_value = _ns(
            items=[
                _ns(
                    metadata=_ns(namespace="prod", name="empty"),
                    data=None,
                    binary_data=None,
                ),
            ]
        )
        out = await queries.list_configmaps(api, "prod")
        assert out[0].keys == []
        assert out[0].redacted_keys == []

    @pytest.mark.parametrize(
        "key",
        [
            # GCP-style service-account variants. People drop these into
            # ConfigMaps by mistake all the time.
            "SERVICE_ACCOUNT_JSON",
            "gcp_service_account",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "firebase_admin_sdk",
            "sa-key",
            "sa.json",
            # AWS variants.
            "AWS_SESSION_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            # mTLS material.
            "CLIENT_CERTIFICATE",
            "client_private_key",
        ],
    )
    async def test_redacts_compound_cloud_keys(self, key: str) -> None:
        api = AsyncMock()
        api.list_namespaced_config_map.return_value = _ns(
            items=[
                _ns(
                    metadata=_ns(namespace="prod", name="cloud-creds"),
                    data={key: "<value>", "LOG_LEVEL": "debug"},
                    binary_data=None,
                ),
            ]
        )
        out = await queries.list_configmaps(api, "prod")
        assert key in out[0].redacted_keys, f"{key!r} should be flagged as secret-shaped"
        assert "LOG_LEVEL" not in out[0].redacted_keys


# ---------------------------------------------------------------------------
# namespace_events
# ---------------------------------------------------------------------------


class TestNamespaceEvents:
    async def test_warnings_only_default(self) -> None:
        api = AsyncMock()
        api.list_namespaced_event.return_value = _ns(items=[])
        await queries.namespace_events(api, "prod")
        api.list_namespaced_event.assert_awaited_once_with(
            namespace="prod",
            field_selector="type=Warning",
            limit=queries.MAX_K8S_EVENTS,
            _request_timeout=(5, 30),
        )

    async def test_include_normal_events(self) -> None:
        api = AsyncMock()
        api.list_namespaced_event.return_value = _ns(items=[])
        await queries.namespace_events(api, "prod", only_warnings=False)
        api.list_namespaced_event.assert_awaited_once_with(
            namespace="prod",
            field_selector="",
            limit=queries.MAX_K8S_EVENTS,
            _request_timeout=(5, 30),
        )

    async def test_since_filter_drops_old_events(self) -> None:
        api = AsyncMock()
        api.list_namespaced_event.return_value = _ns(
            items=[
                _ns(
                    type="Warning",
                    reason="BackOff",
                    message="recent",
                    count=1,
                    last_timestamp=_utc(60),
                    event_time=None,
                    involved_object=_ns(kind="Pod", name="web-0"),
                ),
                _ns(
                    type="Warning",
                    reason="BackOff",
                    message="ancient",
                    count=1,
                    last_timestamp=_utc(60 * 60 * 5),
                    event_time=None,
                    involved_object=_ns(kind="Pod", name="web-1"),
                ),
            ]
        )
        out = await queries.namespace_events(api, "prod", since_min=10)
        assert [e.message for e in out] == ["recent"]

    async def test_zero_since_min_rejected(self) -> None:
        api = AsyncMock()
        with pytest.raises(ValueError, match="since_min"):
            await queries.namespace_events(api, "prod", since_min=0)

    async def test_namespace_events_default_bounded_lookback(self) -> None:
        # The default is now 60min, not unbounded. An ancient event must
        # be filtered out without the caller having to ask.
        api = AsyncMock()
        api.list_namespaced_event.return_value = _ns(
            items=[
                _ns(
                    type="Warning",
                    reason="BackOff",
                    message="recent",
                    count=1,
                    last_timestamp=_utc(60),
                    event_time=None,
                    involved_object=_ns(kind="Pod", name="web-0"),
                ),
                _ns(
                    type="Warning",
                    reason="BackOff",
                    message="ancient",
                    count=1,
                    # 5h ago — outside the new 60min default window.
                    last_timestamp=_utc(60 * 60 * 5),
                    event_time=None,
                    involved_object=_ns(kind="Pod", name="web-1"),
                ),
            ]
        )
        out = await queries.namespace_events(api, "prod")
        assert [e.message for e in out] == ["recent"]

    async def test_namespace_events_rejects_oversized_request(self) -> None:
        # A node rolling-restart on a busy `kube-system` can produce
        # tens of thousands of events. Refuse rather than try to hold
        # them all in memory and post-filter.
        api = AsyncMock()
        api.list_namespaced_event.return_value = _ns(
            items=[
                _ns(
                    type="Warning",
                    reason="BackOff",
                    message=f"e{i}",
                    count=1,
                    last_timestamp=_utc(60),
                    event_time=None,
                    involved_object=_ns(kind="Pod", name=f"web-{i}"),
                )
                for i in range(queries.MAX_K8S_EVENTS + 1)
            ]
        )
        with pytest.raises(ValueError, match="exceeds limit"):
            await queries.namespace_events(api, "prod")

    async def test_namespace_events_rejects_oversized_limit(self) -> None:
        api = AsyncMock()
        with pytest.raises(ValueError, match="MAX_K8S_EVENTS"):
            await queries.namespace_events(api, "prod", limit=queries.MAX_K8S_EVENTS + 1)


# ---------------------------------------------------------------------------
# resource_quotas
# ---------------------------------------------------------------------------


class TestResourceQuotas:
    async def test_headroom_computed(self) -> None:
        api = AsyncMock()
        api.list_namespaced_resource_quota.return_value = _ns(
            items=[
                _ns(
                    metadata=_ns(namespace="prod", name="default"),
                    spec=_ns(
                        hard={
                            "pods": "10",
                            "limits.cpu": "8",
                            "limits.memory": "16Gi",
                        }
                    ),
                    status=_ns(
                        used={
                            "pods": "9",
                            "limits.cpu": "4",
                            "limits.memory": "8Gi",
                        }
                    ),
                ),
            ]
        )
        [q] = await queries.resource_quotas(api, "prod")
        # 1 - 9/10 = 0.1 — pretty hot
        assert q.headroom["pods"] == pytest.approx(0.1)
        assert q.headroom["limits.cpu"] == pytest.approx(0.5)
        assert q.headroom["limits.memory"] == pytest.approx(0.5)

    async def test_unparseable_quota_skipped(self) -> None:
        api = AsyncMock()
        api.list_namespaced_resource_quota.return_value = _ns(
            items=[
                _ns(
                    metadata=_ns(namespace="prod", name="weird"),
                    spec=_ns(hard={"thingies": "not-a-number"}),
                    status=_ns(used={"thingies": "0"}),
                ),
            ]
        )
        [q] = await queries.resource_quotas(api, "prod")
        # Couldn't parse — skipped silently rather than crashing.
        assert "thingies" not in q.headroom

    async def test_zero_hard_means_no_headroom_entry(self) -> None:
        api = AsyncMock()
        api.list_namespaced_resource_quota.return_value = _ns(
            items=[
                _ns(
                    metadata=_ns(namespace="prod", name="zero"),
                    spec=_ns(hard={"pods": "0"}),
                    status=_ns(used={"pods": "0"}),
                ),
            ]
        )
        [q] = await queries.resource_quotas(api, "prod")
        assert "pods" not in q.headroom


# ---------------------------------------------------------------------------
# pod_logs redaction integration
# ---------------------------------------------------------------------------


class TestPodLogsRedaction:
    async def test_redacts_when_enabled(self) -> None:
        api = AsyncMock()
        api.read_namespaced_pod_log.return_value = "starting DB_PASSWORD=hunter2"
        out = await queries.pod_logs(api, "default", "web-0", redact_secrets=True)
        assert "hunter2" not in out[0].line
        assert "REDACTED" in out[0].line

    async def test_pass_through_when_disabled(self) -> None:
        api = AsyncMock()
        api.read_namespaced_pod_log.return_value = "starting DB_PASSWORD=hunter2"
        out = await queries.pod_logs(api, "default", "web-0", redact_secrets=False)
        assert "hunter2" in out[0].line
