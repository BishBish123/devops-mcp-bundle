"""Edge-case tests for the CPU + memory + quantity parsers.

The "happy path" cases live in `test_queries.py`. This file exists for
the awkward inputs we've actually hit in real clusters: fractional
values, fractional memory units, and the rare-but-legal scientific
notation.
"""

from __future__ import annotations

import pytest

from devops_mcp_bundle.k8s import queries


class TestParseCpuEdge:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("0", 0),
            ("0.001", 1),  # 1 millicore
            ("0.25", 250),
            ("2", 2000),
            ("4", 4000),
            ("250m", 250),
            ("1500m", 1500),
        ],
    )
    def test_round_trip(self, value: str, expected: int) -> None:
        assert queries._parse_cpu(value) == expected

    def test_whitespace_tolerated(self) -> None:
        assert queries._parse_cpu("  100m  ") == 100

    def test_nanocore_zero(self) -> None:
        assert queries._parse_cpu("0n") == 0


class TestParseMemoryEdge:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("0", 0),
            ("0Mi", 0),
            ("1Ki", 1024),
            ("1Mi", 1024**2),
            ("1Gi", 1024**3),
            ("1Ti", 1024**4),
            ("1K", 1000),
            ("1M", 1_000_000),
            ("1G", 1_000_000_000),
            ("1T", 1_000_000_000_000),
            ("0.5Gi", 512 * 1024**2),
            ("1.5Mi", int(1.5 * 1024**2)),
        ],
    )
    def test_round_trip(self, value: str, expected: int) -> None:
        assert queries._parse_memory(value) == expected

    def test_raw_bytes_no_suffix(self) -> None:
        assert queries._parse_memory("65536") == 65536

    def test_whitespace_tolerated(self) -> None:
        assert queries._parse_memory("  128Mi  ") == 128 * 1024**2


class TestParseQuantity:
    """`_parse_quantity` lifts both parsers into a single comparable scalar."""

    def test_memory(self) -> None:
        assert queries._parse_quantity("128Mi") == float(128 * 1024**2)

    def test_cpu_millicores(self) -> None:
        assert queries._parse_quantity("250m") == 250.0

    def test_count(self) -> None:
        # ResourceQuota uses bare counts for things like `pods` or
        # `services` — no suffix, lift through float().
        assert queries._parse_quantity("10") == 10.0

    def test_blank(self) -> None:
        assert queries._parse_quantity("") == 0.0


class TestSecretKeyHeuristic:
    @pytest.mark.parametrize(
        "key",
        [
            "DB_PASSWORD",
            "api-key",
            "X_API_KEY",
            "auth_token",
            "ssh-private-key",
            "STRIPE_SECRET",
            "OAUTH_CREDENTIALS",
            "TLS_CERT",
        ],
    )
    def test_secret_keys(self, key: str) -> None:
        assert queries._looks_like_secret_key(key)

    @pytest.mark.parametrize("key", ["DB_HOST", "log_level", "MAX_CONN", "feature_flag"])
    def test_non_secret_keys(self, key: str) -> None:
        assert not queries._looks_like_secret_key(key)


class TestRedactSecretsFromLogs:
    def test_redacts_kv_assignment(self) -> None:
        out = queries.redact_secrets_from_logs(
            "starting up DB_PASSWORD=hunter2 log_level=info"
        )
        assert "hunter2" not in out
        assert "DB_PASSWORD=<redacted>" in out
        assert "log_level=info" in out

    def test_redacts_yaml_style(self) -> None:
        out = queries.redact_secrets_from_logs("auth_token: abc123 user: bob")
        assert "abc123" not in out
        assert "auth_token:<redacted>" in out
        # Non-secret keys pass through unchanged.
        assert "user:" in out and "bob" in out

    def test_leaves_normal_lines_alone(self) -> None:
        line = "Listening on port 8080"
        assert queries.redact_secrets_from_logs(line) == line
