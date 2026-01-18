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
