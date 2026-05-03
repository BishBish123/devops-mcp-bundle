"""`devops-mcp` — top-level CLI for inspecting + installing the bundle."""

from __future__ import annotations

import json
import os
import shutil
from importlib import resources
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import anyio
import asyncpg
import httpx
import typer
from rich.console import Console
from rich.table import Table

from devops_mcp_bundle import __version__


def _redact_dsn(dsn: str) -> str:
    """Return `dsn` with the password component replaced by '***'.

    Works for both DSNs (``postgresql://user:pass@host/db``) and plain
    HTTP/HTTPS URLs (``http://user:pass@host``). If there is no userinfo
    or the userinfo has no password the string is returned unchanged.
    If ``dsn`` is not parseable as a URL it is returned unchanged.

    >>> _redact_dsn("postgresql://alice:hunter2@db:5432/prod")
    'postgresql://alice:***@db:5432/prod'
    >>> _redact_dsn("http://prometheus:9090")
    'http://prometheus:9090'
    >>> _redact_dsn("not-a-url")
    'not-a-url'
    """
    try:
        parsed = urlparse(dsn)
    except Exception:
        return dsn
    if not parsed.password:
        return dsn
    # Rebuild netloc with the password masked.
    user = parsed.username or ""
    host = parsed.hostname or ""
    port_part = f":{parsed.port}" if parsed.port else ""
    masked_netloc = f"{user}:***@{host}{port_part}"
    return urlunparse(parsed._replace(netloc=masked_netloc))


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
    skills_root = _find_skills_root()
    if skills_root is None:
        console.print("[yellow]skills/ not found in this install — see the source repo.[/]")
        return
    table = Table(title="Skills")
    table.add_column("Name", style="bold")
    table.add_column("Description")
    table.add_column("Requires")
    for skill_dir in sorted(skills_root.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        text = skill_md.read_text(encoding="utf-8")
        # Cheap frontmatter parse — all our skills use the same shape.
        desc = ""
        requires = ""
        if text.startswith("---"):
            front = text.split("---", 2)[1]
            for line in front.splitlines():
                if line.startswith("description:"):
                    desc = line.partition(":")[2].strip()
                elif line.startswith("requires_external_tooling:"):
                    requires = line.partition(":")[2].strip()
        table.add_row(skill_dir.name, desc, requires)
    console.print(table)


def _find_skills_root() -> Path | None:
    """Locate the SKILL.md tree, preferring the packaged copy.

    The wheel ships skills at ``devops_mcp_bundle/skills/`` (see the
    hatch ``force-include`` block in ``pyproject.toml``). For editable
    installs and source-tree runs that path doesn't exist; fall back
    to the repo's top-level ``skills/`` directory two parents up from
    this file.
    """
    try:
        packaged = resources.files("devops_mcp_bundle").joinpath("skills")
        # `resources.files` always returns a Traversable, but the
        # `skills/` subpath only exists when hatch's force-include
        # actually populated it (i.e. installed-from-wheel, not
        # editable). Convert to a real path so the rest of the
        # function can use Pathlib uniformly.
        if packaged.is_dir():
            return Path(str(packaged))
    except (ModuleNotFoundError, FileNotFoundError):  # pragma: no cover
        pass

    # Editable / source-tree fallback: <repo>/skills/.
    source_root = Path(__file__).resolve().parent.parent.parent / "skills"
    if source_root.is_dir():
        return source_root

    return None


@app.command()
def install(
    config: Path = typer.Option(
        Path.home() / ".config/claude/mcp.json",
        help="Path to the Claude Code MCP config file (will be created if missing).",
    ),
    postgres_dsn: str | None = typer.Option(
        None,
        "--postgres-dsn",
        "--pgvector-dsn",
        help=(
            "POSTGRES_DSN to bake into the postgres-dba server entry. "
            "Defaults to $POSTGRES_DSN if set. (`--pgvector-dsn` is the "
            "deprecated alias kept for backwards compatibility — the "
            "server is generic Postgres DBA, not pgvector-specific.)"
        ),
    ),
    prometheus_url: str | None = typer.Option(
        None,
        help=(
            "PROMETHEUS_URL to bake into the observability server entry. "
            "Defaults to $PROMETHEUS_URL if set."
        ),
    ),
    loki_url: str | None = typer.Option(
        None,
        help=(
            "LOKI_URL to bake into the observability server entry. Defaults to $LOKI_URL if set."
        ),
    ),
    kubeconfig: Path | None = typer.Option(
        None,
        help=(
            "KUBECONFIG to use for the k8s-inspector server entry. Defaults to $KUBECONFIG if set."
        ),
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the merged config, don't write."),
    validate: bool = typer.Option(
        False,
        "--validate",
        help=(
            "Probe each backend before writing: SELECT 1 against POSTGRES_DSN, "
            "GET /-/healthy on PROMETHEUS_URL, GET /ready on LOKI_URL. "
            "Skips probes for env vars that aren't set. Unreachable backends "
            "are reported but do not block the install — pair with "
            "--strict-validate to fail loudly instead."
        ),
    ),
    strict_validate: bool = typer.Option(
        False,
        "--strict-validate",
        help=(
            "When combined with --validate, exit non-zero if any probed "
            "backend is unreachable. Without this flag --validate is "
            "advisory: the config still gets written so the install can "
            "complete before the backend comes up."
        ),
    ),
) -> None:
    """Wire every server into Claude Code's mcp.json (idempotent).

    Flag values fall back to the matching environment variable so the
    common case ("I already exported these for the stdio smoke test") is
    a single `devops-mcp install` with no flags. Pass an explicit flag to
    override the env var for that one server.
    """
    config = config.expanduser()
    existing = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    servers = existing.setdefault("mcpServers", {})

    # Fall back to the env vars an operator likely already exported when
    # running the stdio smoke test. The previous behaviour wrote `env: {}`
    # for every server when called with no flags, leaving the user to
    # hand-edit mcp.json. The flag still wins when both are present.
    postgres_dsn = postgres_dsn or os.environ.get("POSTGRES_DSN") or None
    prometheus_url = prometheus_url or os.environ.get("PROMETHEUS_URL") or None
    loki_url = loki_url or os.environ.get("LOKI_URL") or None
    if kubeconfig is None and os.environ.get("KUBECONFIG"):
        kubeconfig = Path(os.environ["KUBECONFIG"])

    common_env: dict[str, str] = {}
    if postgres_dsn:
        common_env["POSTGRES_DSN"] = postgres_dsn
    if prometheus_url:
        common_env["PROMETHEUS_URL"] = prometheus_url
    if loki_url:
        common_env["LOKI_URL"] = loki_url
    if kubeconfig:
        common_env["KUBECONFIG"] = str(kubeconfig.expanduser())

    validation_results: list[tuple[str, str, bool, str | None]] = []
    if validate:
        validation_results = _validate_backends(
            postgres_dsn=postgres_dsn,
            prometheus_url=prometheus_url,
            loki_url=loki_url,
        )
        _print_validation_table(validation_results)

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
    # Claude Code on macOS does NOT read this path — it stores MCP servers
    # in ~/.claude.json under the `mcpServers` field, managed via the
    # `claude mcp` CLI. This file is the portable / non-Claude-Code layout
    # (works for any client that reads ~/.config/claude/mcp.json). Surface
    # the canonical Claude Code workflow on stderr so users on macOS know
    # they probably want `claude mcp add-json` instead.
    console.print(
        "[yellow]hint:[/] for Claude Code, prefer "
        "[cyan]claude mcp add-json[/] (per-server) or merge this file into "
        "[cyan]~/.claude.json[/] under the [cyan]mcpServers[/] field — "
        "Claude Code does not read this path directly.",
        style="dim",
    )

    if validate and validation_results:
        _print_validation_summary(validation_results)
        unreachable = [r for r in validation_results if r[2] is False]
        if unreachable and strict_validate:
            raise typer.Exit(code=1)


def _validate_backends(
    *,
    postgres_dsn: str | None,
    prometheus_url: str | None,
    loki_url: str | None,
) -> list[tuple[str, str, bool, str | None]]:
    """Probe each configured backend; return per-backend status tuples.

    Each tuple is ``(name, target, reachable, error)`` — ``error`` is
    ``None`` when ``reachable`` is True. Backends whose URL/DSN was not
    provided are not included; callers print a status table and decide
    whether to treat unreachable as fatal (see ``--strict-validate``).

    Prometheus exposes /-/healthy + /-/ready (200 = up); Loki exposes
    /ready (200 = up). Postgres is probed with a `SELECT 1` via asyncpg
    so we exercise the same driver the postgres-dba server uses.
    """
    results: list[tuple[str, str, bool, str | None]] = []
    if postgres_dsn:

        async def _probe_pg() -> None:
            conn = await asyncpg.connect(postgres_dsn, timeout=5)
            try:
                await conn.fetchval("SELECT 1")
            finally:
                await conn.close()

        safe_dsn = _redact_dsn(postgres_dsn)
        try:
            anyio.run(_probe_pg)
            results.append(("postgres", safe_dsn, True, None))
        except Exception as e:
            results.append(("postgres", safe_dsn, False, str(e)))

    if prometheus_url:
        safe_prom = _redact_dsn(prometheus_url)
        try:
            r = httpx.get(f"{prometheus_url.rstrip('/')}/-/healthy", timeout=5.0)
            r.raise_for_status()
            results.append(("prometheus", safe_prom, True, None))
        except Exception as e:
            results.append(("prometheus", safe_prom, False, str(e)))

    if loki_url:
        safe_loki = _redact_dsn(loki_url)
        try:
            r = httpx.get(f"{loki_url.rstrip('/')}/ready", timeout=5.0)
            r.raise_for_status()
            results.append(("loki", safe_loki, True, None))
        except Exception as e:
            results.append(("loki", safe_loki, False, str(e)))

    return results


def _print_validation_table(results: list[tuple[str, str, bool, str | None]]) -> None:
    """Render the per-backend status table seen above the install summary."""
    if not results:
        return
    table = Table(title="Backend validation")
    table.add_column("Backend", style="bold")
    table.add_column("Target")
    table.add_column("Status")
    table.add_column("Error")
    for name, target, reachable, error in results:
        status = "[green]reachable[/]" if reachable else "[red]unreachable[/]"
        table.add_row(name, target, status, error or "")
    console.print(table)


def _print_validation_summary(results: list[tuple[str, str, bool, str | None]]) -> None:
    """Print the one-line summary that follows the install confirmation."""
    if not results:
        return
    reachable = [r for r in results if r[2]]
    unreachable = [r for r in results if not r[2]]
    detail = ""
    if unreachable:
        bits = [f"{name} at {target}" for name, target, _, _ in unreachable]
        detail = f" (unreachable: {', '.join(bits)} — start them before running tools)"
    console.print(
        f"validation: {len(reachable)}/{len(results)} backends reachable; "
        f"install completed{detail}."
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
