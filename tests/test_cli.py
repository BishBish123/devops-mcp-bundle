"""Smoke tests for the top-level `devops-mcp` CLI.

The CLI is a glorified config-merger; integration coverage is the
typer.testing CliRunner exercising each subcommand with `--help` and
the install path with `--dry-run`. We don't write to the user's home
directory in tests.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devops_mcp_bundle import cli as cli_module
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

    def test_finds_packaged_skills_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate a wheel install: SKILL.md files live next to the
        # package code at `devops_mcp_bundle/skills/<name>/SKILL.md`,
        # not in a sibling `skills/` directory at repo root. The
        # discovery helper has to find them via importlib.resources,
        # falling back to the source tree only when packaged copy is
        # absent. Override the helper to point at a fake packaged tree.
        fake_skill_dir = tmp_path / "fake-skill"
        fake_skill_dir.mkdir()
        (fake_skill_dir / "SKILL.md").write_text(
            "---\nname: fake-skill\ndescription: smoke test only\n---\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(cli_module, "_find_skills_root", lambda: tmp_path)
        result = runner.invoke(app, ["list-skills"])
        assert result.exit_code == 0
        assert "fake-skill" in result.stdout
        assert "smoke test only" in result.stdout

    def test_returns_friendly_message_when_skills_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pinch off both discovery branches; the command must still
        # exit clean and tell the user what to do.
        monkeypatch.setattr(cli_module, "_find_skills_root", lambda: None)
        result = runner.invoke(app, ["list-skills"])
        assert result.exit_code == 0
        assert "skills/ not found" in result.stdout


class TestWheelBundlesSkills:
    """Integration: build the wheel and assert SKILL.md files ship inside.

    This is the regression for the original "skills missing from wheel"
    bug — a wheel with no SKILL.md files would still pass the unit
    tests above, but `pip install devops-mcp-bundle` followed by
    `devops-mcp list-skills` would print the not-found warning. The
    only way to catch that is to actually build the wheel.
    """

    @pytest.mark.integration
    def test_wheel_contains_each_skill(self) -> None:
        uv = shutil.which("uv")
        if uv is None:
            pytest.skip("uv not on PATH")

        repo_root = Path(__file__).resolve().parent.parent
        out_dir = repo_root / "dist"
        before = set(out_dir.glob("*.whl")) if out_dir.exists() else set()
        result = subprocess.run(
            [uv, "build", "--wheel"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        wheels = sorted(out_dir.glob("*.whl"), key=lambda p: p.stat().st_mtime)
        assert wheels, "uv build produced no wheel"
        wheel = wheels[-1]
        names = zipfile.ZipFile(wheel).namelist()
        for skill in (
            "postgres-slow-query-triage",
            "k8s-pod-incident-playbook",
            "deploy-postmortem",
            "redis-memory-pressure-triage",
        ):
            expected = f"devops_mcp_bundle/skills/{skill}/SKILL.md"
            assert expected in names, f"wheel missing {expected}"
        # Tidy: don't leave a fresh wheel lying around if we created it.
        new = set(wheels) - before
        for w in new:
            w.unlink(missing_ok=True)


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


class TestInstallEnvFallback:
    """`install` with no flags should fall back to the standard env vars.

    Before this fallback the no-flag invocation wrote `env: {}` for
    every server, leaving the user to hand-edit mcp.json — the
    standalone-stdio smoke test exports the same vars, so picking them
    up automatically removes a step.
    """

    def test_picks_up_env_vars_when_no_flags(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = tmp_path / "mcp.json"
        kubeconfig = tmp_path / "kubeconfig"
        monkeypatch.setenv("POSTGRES_DSN", "postgresql://envuser:envpw@h/db")
        monkeypatch.setenv("PROMETHEUS_URL", "http://envprom:9090")
        monkeypatch.setenv("LOKI_URL", "http://envloki:3100")
        monkeypatch.setenv("KUBECONFIG", str(kubeconfig))

        result = runner.invoke(app, ["install", "--config", str(config)])
        assert result.exit_code == 0, result.output
        body = json.loads(config.read_text(encoding="utf-8"))
        servers = body["mcpServers"]
        assert servers["postgres-dba"]["env"]["POSTGRES_DSN"] == "postgresql://envuser:envpw@h/db"
        assert servers["observability"]["env"]["PROMETHEUS_URL"] == "http://envprom:9090"
        assert servers["observability"]["env"]["LOKI_URL"] == "http://envloki:3100"
        assert servers["k8s-inspector"]["env"]["KUBECONFIG"] == str(kubeconfig)

    def test_explicit_flag_overrides_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = tmp_path / "mcp.json"
        monkeypatch.setenv("POSTGRES_DSN", "postgresql://envuser:envpw@h/db")
        result = runner.invoke(
            app,
            [
                "install",
                "--config",
                str(config),
                "--pgvector-dsn",
                "postgresql://flaguser:flagpw@h/db",
            ],
        )
        assert result.exit_code == 0, result.output
        body = json.loads(config.read_text(encoding="utf-8"))
        # Flag wins over env var.
        assert (
            body["mcpServers"]["postgres-dba"]["env"]["POSTGRES_DSN"]
            == "postgresql://flaguser:flagpw@h/db"
        )


class TestInstallValidate:
    """`--validate` probes each configured backend before writing mcp.json."""

    def test_validate_skips_backends_with_no_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No env vars + no flags => nothing to probe => succeed silently.
        for var in ("POSTGRES_DSN", "PROMETHEUS_URL", "LOKI_URL", "KUBECONFIG"):
            monkeypatch.delenv(var, raising=False)
        config = tmp_path / "mcp.json"
        result = runner.invoke(app, ["install", "--config", str(config), "--validate"])
        assert result.exit_code == 0, result.output
        # Without any env, every server entry has env: {} — install still writes.
        body = json.loads(config.read_text(encoding="utf-8"))
        assert set(body["mcpServers"]) == {"postgres-dba", "k8s-inspector", "observability"}

    def test_validate_fails_loud_when_backend_unreachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Point at an obviously-unreachable URL — no port should be open
        # on the loopback higher than 1.
        for var in ("POSTGRES_DSN", "LOKI_URL", "KUBECONFIG"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("PROMETHEUS_URL", "http://127.0.0.1:1")
        config = tmp_path / "mcp.json"
        result = runner.invoke(app, ["install", "--config", str(config), "--validate"])
        # Validate failure short-circuits before writing.
        assert result.exit_code != 0
        assert not config.exists()


@pytest.mark.parametrize("cmd", ["version", "list-servers", "list-skills"])
def test_help_for_each_subcommand(cmd: str) -> None:
    """Every subcommand should have --help that exits clean."""
    result = runner.invoke(app, [cmd, "--help"])
    assert result.exit_code == 0
