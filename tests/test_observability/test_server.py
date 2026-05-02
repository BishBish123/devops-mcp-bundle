"""Tests for the FastMCP-decorated tools in observability.server.

These exercise the MCP tool surface itself — that the decorated tool
forwards every parameter through to the helper. The helper logic is
covered exhaustively in test_queries.py; here we just make sure the
MCP wrapper doesn't drop or rename arguments on the way through.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from devops_mcp_bundle.observability import queries, server


@asynccontextmanager
async def _client_ctx(handler: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as c:
        yield c


def _prom_response(value: str) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [0, value]}],
        },
    }


class TestMultiWindowBurnRateTool:
    async def test_forwards_ticket_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Page-tier queries return below threshold, ticket-tier above —
        # so a successful forward of `ticket_long_burn_query` /
        # `ticket_short_burn_query` is observable in the result shape:
        # ``ticket=True`` and the two ticket-window blocks populated.
        seen_queries: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            q = req.url.params["query"]
            seen_queries.append(q)
            value = "10" if q.startswith("t_") else "1"
            return httpx.Response(200, json=_prom_response(value))

        monkeypatch.setattr(server, "_prom_url", lambda: "http://prom")
        monkeypatch.setattr(server, "_client", lambda: _client_ctx(handler))

        result = await server.multi_window_burn_rate(
            objective=0.999,
            long_burn_query="long",
            short_burn_query="short",
            ticket_long_burn_query="t_long",
            ticket_short_burn_query="t_short",
        )

        # Ticket tier fired; ticket-window blocks present.
        assert result.ticket is True
        assert result.page is False
        assert result.ticket_long_window is not None
        assert result.ticket_short_window is not None
        assert result.ticket_long_window.breaching is True
        assert result.ticket_short_window.breaching is True
        # All four queries reached Prometheus (page + ticket).
        assert set(seen_queries) == {"long", "short", "t_long", "t_short"}

    async def test_skips_ticket_when_only_one_query_supplied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Documented behaviour: if either ticket query is None, the
        # whole ticket tier is skipped — verifies the MCP wrapper
        # doesn't accidentally fill in a placeholder for the missing
        # half.
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_prom_response("1"))

        monkeypatch.setattr(server, "_prom_url", lambda: "http://prom")
        monkeypatch.setattr(server, "_client", lambda: _client_ctx(handler))

        result = await server.multi_window_burn_rate(
            objective=0.999,
            long_burn_query="long",
            short_burn_query="short",
            ticket_long_burn_query="only_long",  # no short → tier skipped
        )
        assert result.ticket is False
        assert result.ticket_long_window is None
        assert result.ticket_short_window is None


# ---------------------------------------------------------------------------
# FIX 7 — render_logql / escape_logql_label MCP tool wrappers
# ---------------------------------------------------------------------------


class TestEscapeLogqlLabelTool:
    def test_escape_delegates_to_queries_implementation(self) -> None:
        # The MCP tool is a thin wrapper — verify it returns the same
        # result as the underlying helper.
        raw = 'some"value\nwith\ttabs'
        assert server.escape_logql_label(raw) == queries.escape_logql_label(raw)

    def test_escape_handles_injection_attempt(self) -> None:
        evil = 'api"} |= "injected'
        result = server.escape_logql_label(evil)
        # The injected `"` must be escaped so it can't break the matcher.
        assert '\\"' in result
        assert "injected" in result  # content preserved, just escaped

    def test_escape_plain_value_unchanged(self) -> None:
        assert server.escape_logql_label("simple-value") == "simple-value"


class TestRenderLogqlTool:
    def test_render_substitutes_labels(self) -> None:
        result = server.render_logql(
            '{{app="{app}", env="{env}"}}',
            labels={"app": "api", "env": "prod"},
        )
        assert result == '{app="api", env="prod"}'

    def test_render_escapes_values(self) -> None:
        result = server.render_logql(
            '{{app="{app}"}} |= "{needle}"',
            labels={"app": "api", "needle": 'oh "no"'},
        )
        # The needle's quote must be escaped.
        assert '\\"' in result
        assert "oh" in result

    def test_render_rejects_invalid_key(self) -> None:
        with pytest.raises(ValueError, match="LogQL label key"):
            server.render_logql('{{ns="{ns}"}}}', labels={"ns}": "evil"})

    def test_render_empty_labels(self) -> None:
        # A template with no placeholders: doubled braces are literals,
        # so no substitution occurs and the result is the single-brace form.
        tmpl = '{{app="static"}}'
        assert server.render_logql(tmpl, labels={}) == '{app="static"}'
