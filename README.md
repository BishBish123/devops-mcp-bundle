# devops-mcp-bundle

> Three Model Context Protocol servers for DevOps/SRE — and a Claude Code Skills pack that composes them. **Read-only by design**, installable in one command.

[![ci](https://github.com/BishBish123/devops-mcp-bundle/actions/workflows/ci.yml/badge.svg)](https://github.com/BishBish123/devops-mcp-bundle/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](pyproject.toml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![mcp](https://img.shields.io/badge/mcp-spec--conformant-success)]()

---

## What's in here

| Server | What it does | Read-only |
| --- | --- | --- |
| **postgres-dba** | Slow queries (pg_stat_statements), schema, vacuum status, parser-validated `run_safe_query` | ✅ |
| **k8s-inspector** | Pods, logs, events, OOM kills, top metrics — no `delete`/`patch`/`exec` helpers exist | ✅ |
| **observability** | PromQL (instant + range), LogQL, alerts, SLO burn rate, before/after window compare | ✅ |

Three Claude Code Skills compose the bundle's own servers into actual
workflows:

| Skill | Triggers when... |
| --- | --- |
| `postgres-slow-query-triage` | "why is the DB slow?", "triage queries on `db_main`" |
| `k8s-pod-incident-playbook` | "my pod is crash-looping", "investigate `web-0` in `prod`" |
| `deploy-postmortem` | "did the deploy go badly?", "postmortem for `api` after the rollout" |

A fourth, **companion** skill ships in `skills/` but depends on
external tooling the bundle doesn't provide:

| Skill | Triggers when... | Requires |
| --- | --- | --- |
| `redis-memory-pressure-triage` | "Redis is OOM-ing", "what's eating Redis memory on `cache-1`?" | `redis-cli` on the user's PATH, or a community-built Redis MCP server |

Each skill has a companion `EVAL.md` documenting the pass/fail
conditions and transcript greps you can run to verify the agent did
the right thing — see `skills/<name>/EVAL.md`.

## Prerequisites

| Tool | Version | Why |
| --- | --- | --- |
| **Python** | 3.11 or 3.12 (3.13 not yet supported) | The MCP SDK + asyncpg + kubernetes-asyncio stacks haven't all caught up to 3.13 yet; `requires-python = ">=3.11,<3.13"` enforces this in `pyproject.toml`. |
| **uv** | ≥ 0.4 | Used by `make install`, `uv tool install`, and the CI matrix. Install with `curl -LsSf https://astral.sh/uv/install.sh \| sh`. |
| **Docker** | any recent | Only required for `make up` (Postgres on `:5433`) and `make test-integration`. The unit suite (`make test`) needs neither Docker nor a DB. |

macOS notes: the system `python3` is 3.9 and Homebrew's current `python3`
is 3.13 — neither is in the supported range. Run
`uv python install 3.12` (or `brew install python@3.12`) and pass the
interpreter explicitly: `PYTHON=/opt/homebrew/opt/python@3.12/bin/python3.12 bash install.sh`.

## Install

```bash
# one-line installer (creates a venv at ~/.local/share/devops-mcp-bundle
# and installs from this repo via `pip install git+https://...`)
curl -fsSL https://raw.githubusercontent.com/BishBish123/devops-mcp-bundle/main/install.sh | bash

# or, manually from a checkout:
git clone https://github.com/BishBish123/devops-mcp-bundle
cd devops-mcp-bundle && uv sync

# or directly from git as a uv-managed tool (no clone, isolated venv,
# console scripts on PATH):
uv tool install git+https://github.com/BishBish123/devops-mcp-bundle.git
```

PyPI publishing lands with v1.0; until then install from the git URL
above. The installer respects `PIP_SOURCE` if you need to point it at
a fork or a tag.

Then wire the bundle into Claude Code's `mcp.json`:

```bash
export POSTGRES_DSN=postgresql://bench:bench@localhost:5433/bench
export PROMETHEUS_URL=http://localhost:9090
export LOKI_URL=http://localhost:3100
export KUBECONFIG=~/.kube/config

devops-mcp install \
    --pgvector-dsn   "$POSTGRES_DSN" \
    --prometheus-url "$PROMETHEUS_URL" \
    --loki-url       "$LOKI_URL" \
    --kubeconfig     "$KUBECONFIG"
```

Every flag falls back to the matching env var (`POSTGRES_DSN`,
`PROMETHEUS_URL`, `LOKI_URL`, `KUBECONFIG`), so once you've exported
them for the stdio smoke test you can re-run with no flags:

```bash
devops-mcp install                  # picks up the four env vars
devops-mcp install --dry-run        # print the merged config, don't write
devops-mcp install --validate       # SELECT 1 + /-/healthy + /ready before writing
```

`--validate` only probes the backends whose URL/DSN was provided; it's
safe to run with a partial environment.

## Run a server standalone (stdio)

Useful for the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
POSTGRES_DSN=postgresql://bench:bench@localhost:5433/bench \
    uv run mcp-postgres-dba

PROMETHEUS_URL=http://localhost:9090 LOKI_URL=http://localhost:3100 \
    uv run mcp-observability

KUBECONFIG=~/.kube/config uv run mcp-k8s-inspector
```

Or via HTTP:

```bash
uv run mcp-postgres-dba --transport http --port 8080
```

## Tool surface

Counts below are the source of truth — verify with
`grep -c "@mcp.tool" src/devops_mcp_bundle/<server>/server.py` if you
suspect drift.

### postgres-dba (10 tools)

- `list_databases()` — sizes + owners from `pg_database`
- `list_tables(schema)` — row estimate + on-disk size, scoped to a schema
- `describe_table(qualified_name)` — columns + indexes (incl. `pg_get_indexdef`)
- `slow_queries(min_mean_ms=100, limit=20)` — `pg_stat_statements` top-N (returns `[]` if extension missing)
- `vacuum_status(qualified_name)` — last vacuum/analyze + dead-tuple counts
- `activity_snapshot(min_runtime_ms=0, exclude_idle=False)` — `pg_stat_activity` rows narrowed to triage columns
- `bloat_estimate(schema="public", min_ratio=0.0)` — ioguix-style bloat approximation (no `pgstattuple` needed)
- `kill_query(pid)` — refusal-by-design; returns the `pg_cancel_backend`/`pg_terminate_backend` SQL the user could run themselves
- `classify_statement(sql)` — exposes the safety classifier so an agent can preview *why* a SELECT would be rejected
- `run_safe_query(sql, timeout_ms=5000, row_cap=1000)` — parser-validated SELECT, server-side `default_transaction_read_only=on` + `SET LOCAL statement_timeout` enforced; rows pulled through a server-side cursor capped at `row_cap + 1`

### k8s-inspector (10 tools)

- `list_namespaces()`
- `list_pods(namespace, label_selector)` — phase, node, age, restart count, ready state (filterable by `label_selector`; the response model carries no labels — call `describe_pod` for those)
- `describe_pod(namespace, name)` — containers + conditions + labels + `creation_timestamp`
- `pod_logs(namespace, name, container, tail=200)` — RFC3339 timestamp parsing; tail capped at 10k lines
- `pod_events(namespace, name)` — events filtered to involved object
- `top_pods(namespace)` — live CPU + memory (degrades to `[]` if no metrics-server)
- `recent_oomkills(namespace, since_min=60)`
- `list_configmaps(namespace)` — names + key counts; flags GCP/AWS/mTLS-style secret keys
- `namespace_events(...)` — recent events scoped to a namespace
- `resource_quotas(namespace)` — used vs. hard limits per quota object

### observability (8 tools)

- `prom_query(promql)` — instant PromQL
- `prom_range(promql, start, end, step)` — range PromQL; window capped at 1 week, samples capped at 10k
- `prom_alerts()` — firing/pending alerts
- `prom_targets(state="active")` — scrape-target health (active|dropped|any)
- `loki_query(logql, since="1h", limit=100)` — LogQL, sorted descending; since capped at 1d, limit at 5k
- `slo_status(service, objective, success_query, total_query, window)` — actual + burn-rate from caller-supplied PromQL
- `multi_window_burn_rate(objective, long_burn_query, short_burn_query, …)` — Google SRE-workbook two-window page + ticket
- `compare_windows(promql_a, promql_b)` — delta + percent change between two PromQL expressions

The `escape_logql_label(value)` and `render_logql(template, **labels)`
helpers in `observability.queries` are the supported way to compose
LogQL with untrusted label values — they prevent matcher break-out
attacks the same way `is_read_only_sql` prevents SQL injection.

`render_logql` uses Python `str.format` syntax under the hood, which
means **every literal LogQL brace has to be doubled**: `{{` for a
literal `{`, `}}` for a literal `}`. Single braces are reserved for
your placeholders. This is the easiest thing to get wrong — the error
is a `str.format` `ValueError: unexpected '{' in field name` that
points at the template, not at the caller.

```python
from devops_mcp_bundle.observability.queries import render_logql

# Correct: doubled braces around the label-matcher block.
render_logql(
    '{{app="{app}", container="{container}"}} |= "{needle}"',
    app="api",
    container="web",
    needle='oh "no"',
)
# -> '{app="api", container="web"} |= "oh \\"no\\""'

# Wrong: single braces — the literal `{` is read as the start of a
# nested placeholder name and str.format raises
# `ValueError: unexpected '{' in field name`.
render_logql('{app="{app}"}', app="api")
```

The double-quote breakout in `needle` is escaped automatically; you
never have to call `escape_logql_label` yourself when going through
`render_logql`.

## Safety

Every server is **read-only by construction**:

- The Postgres server has no helper for `INSERT`/`UPDATE`/`DELETE`/`DDL`. The
  one tool that takes user SQL (`run_safe_query`) gates it through a sqlparse
  classifier (rejects multi-statement, write-via-CTE, `SELECT ... INTO`,
  row-locking `FOR UPDATE`/`FOR SHARE`, EXPLAIN ANALYZE, etc.) *and* runs
  inside a `transaction(readonly=True)` with a `SET LOCAL statement_timeout`.
  Layer 1 (classifier) is unit-tested; Layer 2 (DB-side `READ ONLY` +
  `SET LOCAL` enforcement) has integration tests in
  `tests/test_postgres/test_queries_integration.py` covering round-trip,
  row caps, slow-query cancellation via `pg_sleep`, and a
  classifier-miss regression that issues a write inside a read-only
  transaction and asserts Postgres rejects it.
- The Kubernetes server has no helper for `delete`, `patch`, `apply`,
  or `exec`. There is no flag to enable them.
- The observability server is HTTP GET against Prometheus + Loki only.
  PromQL itself is read-only; LogQL too.

If a future contributor adds a write path, the test suite has a place
ready (`safety` modules + `read-only` integration assertions) for them
to add the gate.

## Tests

```bash
make install             # uv sync --extra dev
make test                # unit tests (no Docker)
make test-integration    # integration tests (brings up local Postgres :5433)
make smoke               # end-to-end import + classifier sanity, no DB / cluster / HTTP needed
make check               # lint + typecheck
```

`make smoke` is the fastest "did I break something obvious?" check —
it imports every server, asserts the SQL safety classifier accepts
`SELECT 1` and rejects `DROP TABLE`, exercises the LogQL escape
helper, and runs the unit suite. It needs no Docker, no DB, and no
network — useful in pre-push hooks and as the first step of any CI
debugging session.

The bundle ships with a comprehensive test suite — run `make test`
for the latest count. Roughly:

- Postgres layer: SQL safety classifier (CTE injection, multi-statement,
  mutating keywords, side-effecting function denylist, blank input),
  read-only-SQL classifier, `run_safe_query`, plus helper tests
- K8s server unit tests using mocked `CoreV1Api` / `CustomObjectsApi`
  (helpers, parsers, queries)
- Observability server unit tests using `httpx.MockTransport` —
  every PromQL result type, error envelopes, duration parser, SLO
  burn-rate, LogQL render/escape
- CLI tests for the top-level `devops-mcp` command (version,
  list-servers, list-skills, install --dry-run, idempotent merge,
  `--validate` advisory + `--strict-validate` hard-fail)
- Integration tests against a real Postgres 16 service container
  (round-trip, row caps, `SET LOCAL` timeouts, classifier-miss
  rejection), plus a wheel-build assertion that `SKILL.md` files ship

## Layout

```
src/devops_mcp_bundle/
  postgres/      models + queries + safety + server
  k8s/           models + queries + server (mock-tested)
  observability/ models + queries + server (mock-tested via httpx)
  cli.py         `devops-mcp` — list-servers, list-skills, install
skills/
  postgres-slow-query-triage/
  k8s-pod-incident-playbook/
  deploy-postmortem/
tests/           unit + integration (marked)
install.sh       one-line installer for end users
```

## Further reading

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — module layout, the three-layer
  Postgres safety model, and the seam between MCP servers and skills.
- [`SECURITY.md`](SECURITY.md) — threat model, what the bundle does
  and doesn't protect against, how to report a vulnerability.
- [`docs/adr/`](docs/adr/) — Architecture Decision Records:
  - [`0001`](docs/adr/0001-sqlparse-vs-regex-for-sql-safety.md) —
    sqlparse vs regex for SQL safety classification
  - [`0002`](docs/adr/0002-set-local-vs-set-session-statement-timeout.md) —
    `SET LOCAL` vs `SET SESSION` for the timeout
  - [`0003`](docs/adr/0003-mocked-k8s-vs-fake-cluster.md) —
    mocked `kubernetes_asyncio` vs a real cluster in tests
  - [`0004`](docs/adr/0004-loki-vs-cloudwatch-logs.md) —
    Loki as the log backend; not CloudWatch

## Pre-commit (optional)

A `.pre-commit-config.yaml` ships with the repo. To enable:

```bash
uv tool install pre-commit
pre-commit install                  # installs the hook
pre-commit run --all-files          # one-shot run
```

The hooks run ruff (lint + format), mypy on push, and the full unit
suite on push. They mirror the CI **unit + lint + type-check** jobs;
the Postgres integration job runs only in CI (it requires a Postgres
service container that the local hook can't spin up).

## Known issues

- **`notifications/initialized` returns `-32602` over FastMCP HTTP
  transport.** The MCP spec defines this as a no-op notification —
  client → server, no response expected — but FastMCP's HTTP transport
  returns a `-32602 Invalid params` JSON-RPC error on receipt. The
  notification is fire-and-forget by design, so the error is harmless:
  the server is up, every subsequent `tools/list` and `tools/call`
  works as documented. Ignore the spurious error in client logs. Stdio
  transport is unaffected. Affects FastMCP 3.x (current pin in
  `pyproject.toml` is `fastmcp>=2.0`; the deployed venv resolves to
  3.x today). Upstream issue: TBD — see FastMCP changelog.

## License

MIT. See [LICENSE](LICENSE).
