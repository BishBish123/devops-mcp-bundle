"""Unit tests for the helpers that don't require a live database.

`activity_snapshot` and `bloat_estimate` SQL is integration-tested in
`test_queries_integration.py`. The pieces verified here are the input
validation and the documented refusal of `kill_query`.
"""

from __future__ import annotations

import pytest

from devops_mcp_bundle.postgres import queries


class TestKillQueryRefusal:
    def test_returns_refusal_string(self) -> None:
        msg = queries.kill_query(42)
        assert "refused" in msg.lower()
        assert "42" in msg
        # Surfaces the SQL the user could run themselves.
        assert "pg_cancel_backend" in msg
        assert "pg_terminate_backend" in msg

    @pytest.mark.parametrize("pid", [0, -1, -100])
    def test_invalid_pid_rejected(self, pid: int) -> None:
        with pytest.raises(ValueError, match="pid"):
            queries.kill_query(pid)


class TestActivitySnapshotInputValidation:
    async def test_negative_min_runtime_rejected(self) -> None:
        # `conn` is unused before the validation check; `None` is fine.
        with pytest.raises(ValueError, match="min_runtime_ms"):
            await queries.activity_snapshot(None, min_runtime_ms=-1)  # type: ignore[arg-type]


class TestBloatEstimateInputValidation:
    async def test_negative_min_ratio_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_ratio"):
            await queries.bloat_estimate(None, min_ratio=-0.1)  # type: ignore[arg-type]
