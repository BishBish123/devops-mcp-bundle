"""Unit tests for the observability query helpers using a mocked HTTP transport."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
import pytest

from devops_mcp_bundle.observability import queries

PROM = "http://prom"
LOKI = "http://loki"


def _client_with(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


def _prom_response(result_type: str, result: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "success", "data": {"resultType": result_type, "result": result}}


# ---------------------------------------------------------------------------
# _check / error envelope
# ---------------------------------------------------------------------------


class TestCheckEnvelope:
    async def test_error_envelope_raises(self) -> None:
        async with _client_with(
            lambda req: httpx.Response(200, json={"status": "error", "error": "boom"})
        ) as c:
            with pytest.raises(RuntimeError, match="prom_query failed: boom"):
                await queries.prom_query(c, PROM, "up")

    async def test_http_error_propagates(self) -> None:
        async with _client_with(lambda req: httpx.Response(500)) as c:
            with pytest.raises(httpx.HTTPStatusError):
                await queries.prom_query(c, PROM, "up")


# ---------------------------------------------------------------------------
# prom_query / prom_range
# ---------------------------------------------------------------------------


class TestPromQuery:
    async def test_vector_result(self) -> None:
        body = _prom_response(
            "vector",
            [
                {"metric": {"job": "api"}, "value": [1700000000.0, "0.99"]},
                {"metric": {"job": "web"}, "value": [1700000000.0, "0.95"]},
            ],
        )
        async with _client_with(lambda req: httpx.Response(200, json=body)) as c:
            series = await queries.prom_query(c, PROM, "up")
        assert [s.metric["job"] for s in series] == ["api", "web"]
        assert series[0].samples[0].value == 0.99

    async def test_matrix_result(self) -> None:
        body = _prom_response(
            "matrix",
            [
                {
                    "metric": {"job": "api"},
                    "values": [[1700000000.0, "1"], [1700000015.0, "0.5"]],
                }
            ],
        )
        async with _client_with(lambda req: httpx.Response(200, json=body)) as c:
            series = await queries.prom_range(c, PROM, "up", "now-1h", "now")
        assert len(series[0].samples) == 2

    async def test_blank_query_rejected(self) -> None:
        async with _client_with(lambda req: httpx.Response(200)) as c:
            with pytest.raises(ValueError, match="promql"):
                await queries.prom_query(c, PROM, "  ")


# ---------------------------------------------------------------------------
# prom_alerts
# ---------------------------------------------------------------------------


class TestPromAlerts:
    async def test_normalises_alert_shape(self) -> None:
        body = {
            "status": "success",
            "data": {
                "alerts": [
                    {
                        "state": "firing",
                        "labels": {"alertname": "HighErrors", "severity": "page"},
                        "annotations": {"summary": "5xx surge"},
                        "activeAt": "2026-04-29T03:00:00Z",
                    },
                    {
                        "state": "pending",
                        "labels": {"alertname": "BudgetBurn"},
                        "annotations": {},
                    },
                ]
            },
        }
        async with _client_with(lambda req: httpx.Response(200, json=body)) as c:
            alerts = await queries.prom_alerts(c, PROM)
        assert [a.name for a in alerts] == ["HighErrors", "BudgetBurn"]
        assert alerts[0].severity == "page"
        assert alerts[0].summary == "5xx surge"
        assert alerts[1].severity is None


# ---------------------------------------------------------------------------
# loki_query
# ---------------------------------------------------------------------------


class TestLokiQuery:
    async def test_returns_log_entries_sorted_descending(self) -> None:
        body = {
            "status": "success",
            "data": {
                "result": [
                    {
                        "stream": {"app": "api"},
                        "values": [
                            ["1700000000000000000", "older"],
                            ["1700000005000000000", "newer"],
                        ],
                    }
                ]
            },
        }
        async with _client_with(lambda req: httpx.Response(200, json=body)) as c:
            entries = await queries.loki_query(c, LOKI, '{app="api"}')
        # Sorted descending so newest is first.
        assert [e.line for e in entries] == ["newer", "older"]
        assert entries[0].timestamp_ns == 1_700_000_005_000_000_000
        assert entries[0].stream == {"app": "api"}

    async def test_invalid_inputs_rejected(self) -> None:
        async with _client_with(lambda req: httpx.Response(200)) as c:
            with pytest.raises(ValueError, match="logql"):
                await queries.loki_query(c, LOKI, "  ")
            with pytest.raises(ValueError, match="limit"):
                await queries.loki_query(c, LOKI, '{app="api"}', limit=0)


class TestParseDuration:
    @pytest.mark.parametrize(
        ("s", "secs"),
        [
            ("30s", 30),
            ("5m", 300),
            ("2h", 7200),
            ("1d", 86400),
        ],
    )
    def test_known(self, s: str, secs: int) -> None:
        assert queries._parse_duration(s) == dt.timedelta(seconds=secs)

    @pytest.mark.parametrize("s", ["", "5x", "abc", "h"])
    def test_invalid_rejected(self, s: str) -> None:
        with pytest.raises(ValueError):
            queries._parse_duration(s)


# ---------------------------------------------------------------------------
# slo_status / compare_windows
# ---------------------------------------------------------------------------


class TestSloStatus:
    async def test_basic_attainment(self) -> None:
        # success rate = 990/1000 = 99% — under 99.9% objective.
        def handler(req: httpx.Request) -> httpx.Response:
            promql = req.url.params["query"]
            value = 990.0 if "success" in promql else 1000.0
            return httpx.Response(
                200,
                json=_prom_response("vector", [{"metric": {}, "value": [0, str(value)]}]),
            )

        async with _client_with(handler) as c:
            slo = await queries.slo_status(
                c,
                PROM,
                service="api",
                objective=0.999,
                success_query="success",
                total_query="total",
            )
        assert slo.actual == pytest.approx(0.99)
        # Burn rate: error_rate / allowed_error = 0.01 / 0.001 = 10
        assert slo.burn_rate == pytest.approx(10.0)
        assert slo.error_budget_remaining == pytest.approx(-9.0)

    async def test_invalid_objective_rejected(self) -> None:
        async with _client_with(lambda req: httpx.Response(200)) as c:
            with pytest.raises(ValueError, match="objective"):
                await queries.slo_status(c, PROM, "x", 1.5, "a", "b")

    async def test_zero_total_means_zero_actual(self) -> None:
        body = _prom_response("vector", [{"metric": {}, "value": [0, "0"]}])
        async with _client_with(lambda req: httpx.Response(200, json=body)) as c:
            slo = await queries.slo_status(c, PROM, "x", 0.99, "a", "b")
        assert slo.actual == 0.0


class TestCompareWindows:
    async def test_delta_and_pct(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            q = req.url.params["query"]
            value = 110.0 if q == "now" else 100.0
            return httpx.Response(
                200,
                json=_prom_response("vector", [{"metric": {}, "value": [0, str(value)]}]),
            )

        async with _client_with(handler) as c:
            wd = await queries.compare_windows(c, PROM, "now", "before")
        assert wd.window_a_value == 110.0
        assert wd.window_b_value == 100.0
        assert wd.delta == 10.0
        assert wd.pct_change == pytest.approx(10.0)

    async def test_division_by_zero_pct_is_none(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            q = req.url.params["query"]
            value = 5.0 if q == "now" else 0.0
            return httpx.Response(
                200,
                json=_prom_response("vector", [{"metric": {}, "value": [0, str(value)]}]),
            )

        async with _client_with(handler) as c:
            wd = await queries.compare_windows(c, PROM, "now", "before")
        assert wd.pct_change is None


# ---------------------------------------------------------------------------
# Convenience: prom_query produces the right URL params
# ---------------------------------------------------------------------------


class TestPromTargets:
    async def test_normalises_active_targets(self) -> None:
        body = {
            "status": "success",
            "data": {
                "activeTargets": [
                    {
                        "labels": {"job": "api", "instance": "10.0.0.1:9090"},
                        "health": "up",
                        "lastScrape": "2026-04-29T03:00:00Z",
                        "lastError": "",
                        "scrapePool": "api",
                    },
                    {
                        "labels": {"job": "web", "instance": "10.0.0.2:9090"},
                        "health": "down",
                        "lastScrape": "2026-04-29T03:00:00Z",
                        "lastError": "connection refused",
                    },
                ]
            },
        }
        async with _client_with(lambda req: httpx.Response(200, json=body)) as c:
            targets = await queries.prom_targets(c, PROM)
        assert [t.health for t in targets] == ["up", "down"]
        assert targets[0].last_error is None  # empty string normalised to None
        assert targets[1].last_error == "connection refused"
        assert targets[0].scrape_pool == "api"

    async def test_invalid_state_rejected(self) -> None:
        async with _client_with(lambda req: httpx.Response(200)) as c:
            with pytest.raises(ValueError, match="state must be"):
                await queries.prom_targets(c, PROM, state="bogus")

    async def test_state_param_forwarded(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(
                200,
                json={"status": "success", "data": {"activeTargets": []}},
            )

        async with _client_with(handler) as c:
            await queries.prom_targets(c, PROM, state="any")
        assert captured[0].url.params["state"] == "any"


class TestMultiWindowBurnRate:
    async def test_pages_only_when_both_windows_breach(self) -> None:
        # 16x burn on the long window, 16x burn on the short window — page.
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_prom_response("vector", [{"metric": {}, "value": [0, "16"]}]),
            )

        async with _client_with(handler) as c:
            r = await queries.multi_window_burn_rate(
                c, PROM, objective=0.999, long_burn_query="long", short_burn_query="short"
            )
        assert r.page is True
        assert r.long_window.breaching is True
        assert r.short_window.breaching is True

    async def test_does_not_page_when_short_window_recovered(self) -> None:
        # Long window still hot from a past incident, short window cooled.
        def handler(req: httpx.Request) -> httpx.Response:
            q = req.url.params["query"]
            value = "16" if q == "long" else "1"
            return httpx.Response(
                200,
                json=_prom_response("vector", [{"metric": {}, "value": [0, value]}]),
            )

        async with _client_with(handler) as c:
            r = await queries.multi_window_burn_rate(
                c, PROM, objective=0.999, long_burn_query="long", short_burn_query="short"
            )
        assert r.page is False
        assert r.long_window.breaching is True
        assert r.short_window.breaching is False

    async def test_invalid_objective_rejected(self) -> None:
        async with _client_with(lambda req: httpx.Response(200)) as c:
            with pytest.raises(ValueError, match="objective"):
                await queries.multi_window_burn_rate(c, PROM, 1.5, "a", "b")

    async def test_negative_threshold_rejected(self) -> None:
        async with _client_with(lambda req: httpx.Response(200)) as c:
            with pytest.raises(ValueError, match="thresholds"):
                await queries.multi_window_burn_rate(c, PROM, 0.999, "a", "b", long_threshold=-1)


class TestEscapeLogqlLabel:
    @pytest.mark.parametrize(
        ("raw", "escaped"),
        [
            ("simple", "simple"),
            ('quote"inside', 'quote\\"inside'),
            ("back\\slash", "back\\\\slash"),
            ("line\nbreak", "line\\nbreak"),
            ("tab\there", "tab\\there"),
            # Empty values stay empty — labels can match the empty string.
            ("", ""),
        ],
    )
    def test_escape(self, raw: str, escaped: str) -> None:
        assert queries.escape_logql_label(raw) == escaped


class TestRenderLogql:
    def test_simple_template(self) -> None:
        out = queries.render_logql('{{app="{app}"}}', app="api")
        assert out == '{app="api"}'

    def test_injected_quote_is_escaped(self) -> None:
        # Without escaping, this would close the matcher and inject `} |= "x"`.
        evil = 'api"} |= "x'
        out = queries.render_logql('{{app="{app}"}}', app=evil)
        assert out == '{app="api\\"} |= \\"x"}'
        # Every `"` and `}` from the payload is preceded by a backslash —
        # the matcher's own boundary `"}` is the only un-escaped pair.
        idx = out.rfind('"}')
        assert idx == len(out) - 2  # boundary is the last two chars
        # And the payload's `"` is escaped (preceded by `\`).
        assert '\\"' in out

    def test_multiple_placeholders(self) -> None:
        out = queries.render_logql('{{app="{app}", env="{env}"}}', app="api", env="prod")
        assert out == '{app="api", env="prod"}'


class TestRequestParams:
    async def test_prom_range_includes_step(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=_prom_response("matrix", []))

        async with _client_with(handler) as c:
            await queries.prom_range(c, PROM, "up", "now-5m", "now", step="60s")
        assert captured[0].url.params["step"] == "60s"
        assert captured[0].url.params["query"] == "up"

    async def test_loki_query_uses_query_range_endpoint(self) -> None:
        captured: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=json.loads('{"status":"success","data":{"result":[]}}'))

        async with _client_with(handler) as c:
            await queries.loki_query(c, LOKI, '{app="api"}')
        assert captured[0].url.path.endswith("/loki/api/v1/query_range")
        assert captured[0].url.params["direction"] == "backward"
