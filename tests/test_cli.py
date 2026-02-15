"""Smoke tests for the top-level `devops-mcp` CLI.

The CLI is a glorified config-merger; integration coverage is the
typer.testing CliRunner exercising each subcommand with `--help` and
the install path with `--dry-run`. We don't write to the user's home
directory in tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devops_mcp_bundle.cli import app

runner = CliRunner()


class TestVersion:
    def test_prints_version(self) -> None:
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        # Matches "devops-mcp-bundle X.Y.Z" — don't pin to current version.
        assert "devops-mcp-bundle" in result.stdout


class TestListServers:
    def test_lists_three_servers(self) -> None:
        result = runner.invoke(app, ["list-servers"])
        assert result.exit_code == 0
        assert "postgres-dba" in result.stdout
        assert "k8s-inspector" in result.stdout
        assert "observability" in result.stdout


class TestListSkills:
    def test_lists_at_least_three_skills(self) -> None:
        result = runner.invoke(app, ["list-skills"])
        assert result.exit_code == 0
        # The three originals; redis-memory-pressure-triage may or may
        # not be picked up depending on whether the test runs from the
        # source tree (it does), but the originals must always appear.
        assert "postgres-slow-query-triage" in result.stdout
        assert "k8s-pod-incident-playbook" in result.stdout
        assert "deploy-postmortem" in result.stdout


class TestInstallDryRun:
    def test_renders_three_server_entries(self, tmp_path: Path) -> None:
        config = tmp_path / "mcp.json"
        result = runner.invoke(
            app,
            [
                "install",
                "--config",
                str(config),
                "--pgvector-dsn",
                "postgresql://u:p@h/db",
                "--prometheus-url",
                "http://prom:9090",
                "--loki-url",
                "http://loki:3100",
                "--kubeconfig",
                str(tmp_path / "kubeconfig"),
                "--dry-run",
            ],
        )
        assert result.exit_code == 0
        # Find the JSON envelope in the output.
        # Rich's print_json wraps colours we don't want to pin on.
        # Instead, smoke-check the key tokens are present.
        assert "postgres-dba" in result.stdout
        assert "POSTGRES_DSN" in result.stdout
        assert "PROMETHEUS_URL" in result.stdout
        # Dry-run must not have created the file.
        assert not config.exists()

    def test_writes_when_not_dry_run(self, tmp_path: Path) -> None:
        config = tmp_path / "mcp.json"
        result = runner.invoke(
            app,
            [
                "install",
                "--config",
                str(config),
                "--pgvector-dsn",
                "postgresql://u:p@h/db",
            ],
        )
        assert result.exit_code == 0
        assert config.exists()
        body = json.loads(config.read_text(encoding="utf-8"))
        assert "mcpServers" in body
        servers = body["mcpServers"]
        assert set(servers) == {"postgres-dba", "k8s-inspector", "observability"}
        # Only the postgres entry got the DSN.
        assert "POSTGRES_DSN" in servers["postgres-dba"]["env"]
        assert "POSTGRES_DSN" not in servers["k8s-inspector"]["env"]

    def test_idempotent_preserves_existing_keys(self, tmp_path: Path) -> None:
        config = tmp_path / "mcp.json"
        # Pre-existing config with an unrelated entry the install
        # should preserve.
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "some-other-server": {"command": "x", "args": []},
                    },
                    "myCustomKey": 42,
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(app, ["install", "--config", str(config)])
        assert result.exit_code == 0
        body = json.loads(config.read_text(encoding="utf-8"))
        assert body["myCustomKey"] == 42
        assert "some-other-server" in body["mcpServers"]
        assert "postgres-dba" in body["mcpServers"]


@pytest.mark.parametrize("cmd", ["version", "list-servers", "list-skills"])
def test_help_for_each_subcommand(cmd: str) -> None:
    """Every subcommand should have --help that exits clean."""
    result = runner.invoke(app, [cmd, "--help"])
    assert result.exit_code == 0
