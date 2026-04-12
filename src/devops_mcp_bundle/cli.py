"""`devops-mcp` — top-level CLI for inspecting + installing the bundle."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from devops_mcp_bundle import __version__

app = typer.Typer(
    name="devops-mcp",
    help="DevOps MCP bundle: 3 servers + a Claude Code Skills pack.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()


_SERVERS: dict[str, dict[str, str]] = {
    "postgres-dba": {
        "command": "mcp-postgres-dba",
        "env": "POSTGRES_DSN",
        "description": "Read-only Postgres DBA — slow queries, schema, safe SELECT.",
    },
    "k8s-inspector": {
        "command": "mcp-k8s-inspector",
        "env": "KUBECONFIG (or in-cluster service account)",
        "description": "Read-only Kubernetes inspector — pods, logs, events, OOMs.",
    },
    "observability": {
        "command": "mcp-observability",
        "env": "PROMETHEUS_URL, LOKI_URL",
        "description": "Prometheus + Loki query tools, SLO + window-compare helpers.",
    },
}


@app.command()
def version() -> None:
    """Print the installed bundle version."""
    console.print(f"devops-mcp-bundle {__version__}")


@app.command()
def list_servers() -> None:
    """Show every server in the bundle and its required env vars."""
    table = Table(title="DevOps MCP bundle servers")
    table.add_column("Name", style="bold")
    table.add_column("Command")
    table.add_column("Env")
    table.add_column("Description")
    for name, info in _SERVERS.items():
        table.add_row(name, info["command"], info["env"], info["description"])
    console.print(table)


@app.command()
def list_skills() -> None:
    """List the Claude Code Skills shipped in this bundle."""
    skills_root = Path(__file__).resolve().parent.parent.parent / "skills"
    if not skills_root.exists():
        console.print("[yellow]skills/ not found in this install — see the source repo.[/]")
        return
    table = Table(title="Skills")
    table.add_column("Name", style="bold")
    table.add_column("Description")
    for skill_dir in sorted(skills_root.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        text = skill_md.read_text(encoding="utf-8")
        # Cheap frontmatter parse — all our skills use the same shape.
        desc = ""
        if text.startswith("---"):
            front = text.split("---", 2)[1]
            for line in front.splitlines():
                if line.startswith("description:"):
                    desc = line.partition(":")[2].strip()
                    break
        table.add_row(skill_dir.name, desc)
    console.print(table)


@app.command()
def install(
    config: Path = typer.Option(
        Path.home() / ".config/claude/mcp.json",
        help="Path to the Claude Code MCP config file (will be created if missing).",
    ),
    pgvector_dsn: str | None = typer.Option(
        None, help="POSTGRES_DSN to bake into the postgres-dba server entry."
    ),
    prometheus_url: str | None = typer.Option(
        None, help="PROMETHEUS_URL to bake into the observability server entry."
    ),
    loki_url: str | None = typer.Option(
        None, help="LOKI_URL to bake into the observability server entry."
    ),
    kubeconfig: Path | None = typer.Option(
        None, help="KUBECONFIG to use for the k8s-inspector server entry."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the merged config, don't write."),
) -> None:
    """Wire every server into Claude Code's mcp.json (idempotent)."""
    config = config.expanduser()
    existing = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    servers = existing.setdefault("mcpServers", {})

    common_env: dict[str, str] = {}
    if pgvector_dsn:
        common_env["POSTGRES_DSN"] = pgvector_dsn
    if prometheus_url:
        common_env["PROMETHEUS_URL"] = prometheus_url
    if loki_url:
        common_env["LOKI_URL"] = loki_url
    if kubeconfig:
        common_env["KUBECONFIG"] = str(kubeconfig.expanduser())

    for name, info in _SERVERS.items():
        servers[name] = {
            "command": _command_path(info["command"]),
            "args": [],
            "env": {k: v for k, v in common_env.items() if k in _required_env_for(name)},
        }

    if dry_run:
        console.print_json(data=existing)
        return

    config.parent.mkdir(parents=True, exist_ok=True)
    backup = config.with_suffix(config.suffix + ".bak")
    if config.exists():
        backup.write_text(config.read_text(encoding="utf-8"), encoding="utf-8")
    config.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    console.print(
        f"[green]wrote[/] {len(_SERVERS)} server entries to {config} (backup at {backup.name})"
    )


def _command_path(name: str) -> str:
    """Return the absolute path to a console-script entry, or just its name."""
    found = shutil.which(name)
    return found or name


def _required_env_for(server: str) -> set[str]:
    return {
        "postgres-dba": {"POSTGRES_DSN"},
        "k8s-inspector": {"KUBECONFIG"},
        "observability": {"PROMETHEUS_URL", "LOKI_URL"},
    }[server]


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()


# Keep mypy happy on the unused-os imports if linting reorders.
_ = os
