"""Unit tests for the helpers that don't require a live database."""

from __future__ import annotations

import pytest

from devops_mcp_bundle.postgres import queries


class TestActivitySnapshotInputValidation:
    async def test_negative_min_runtime_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_runtime_ms"):
            await queries.activity_snapshot(None, min_runtime_ms=-1)  # type: ignore[arg-type]


class TestBloatEstimateInputValidation:
    async def test_negative_min_ratio_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_ratio"):
            await queries.bloat_estimate(None, min_ratio=-0.1)  # type: ignore[arg-type]
