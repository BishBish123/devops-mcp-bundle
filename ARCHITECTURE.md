# Architecture

This bundle ships three Model Context Protocol (MCP) servers and a
companion Skills pack. This document is the deeper write-up: how the
pieces fit, why the safety model is what it is, and where the seams
are if you want to extend the bundle.

## Stack diagram

```
                  ┌──────────────────────────────┐
                  │  Claude Code  (or any MCP    │
                  │  client — Inspector, etc.)   │
                  └──────────────┬───────────────┘
                                 │  stdio | HTTP/SSE
                  ┌──────────────┴───────────────┐
                  │         FastMCP servers      │
                  │                              │
   POSTGRES_DSN ──▶ postgres-dba   ─── asyncpg ──▶ Postgres
   KUBECONFIG  ──▶ k8s-inspector  ─── kubernetes_asyncio ──▶ kube-apiserver
   PROM_URL    ──▶ observability  ─── httpx     ──▶ Prometheus
   LOKI_URL    ──┘                                  Loki
                  └──────────────────────────────┘

   skills/                 ─ markdown that wraps the tools above
   src/.../cli.py          ─ `devops-mcp install` writes mcp.json for you
```

Each server is a single Python process. They do not communicate with
each other — composition happens in the agent (or in the Skills, which
are markdown playbooks the agent follows).

## Module layout

```
src/devops_mcp_bundle/
  postgres/
    safety.py     # `is_read_only_sql` + `classify_sql` — pure, no I/O
    queries.py    # `async def` helpers per tool, takes `asyncpg.Connection`
    models.py     # Pydantic types crossing the MCP boundary
    server.py     # FastMCP wrapper — open conn, call query, return model
  k8s/
    queries.py    # `async def` helpers per tool, takes a `*Api` client
    models.py     # Pydantic
    server.py     # FastMCP wrapper
  observability/
    queries.py    # `async def` helpers per tool, takes `httpx.AsyncClient`
    models.py     # Pydantic
    server.py     # FastMCP wrapper
  cli.py          # `devops-mcp` — list-servers, list-skills, install
```

The recurring pattern is **server.py is thin, queries.py is testable**.
Server modules know about `os.environ`, transport selection, and FastMCP
decorators; queries modules know nothing about MCP. This is what makes
the test suite feasible: tests pump in mock connections / mock httpx
transports / mock kubernetes clients and assert on `queries.*` directly.

## The safety model — three layers

The Postgres server is the only one that takes user-supplied SQL, so
its safety model is the most elaborate. Three layers, deliberately
redundant:

### Layer 1 — parser-classifier (`postgres/safety.py`)

`is_read_only_sql(sql)` parses with `sqlparse`, asserts a single
statement, classifies the leading keyword. Anything not in the
read-only allow-list (`SELECT`, `EXPLAIN`, `WITH`, `SHOW`, `VALUES`)
is rejected. The classifier then walks every flattened token looking
for mutating keywords inside subqueries (catches the
`WITH inserted AS (INSERT … RETURNING …) SELECT *` pattern) and the
`ANALYZE` keyword inside an `EXPLAIN` (catches `EXPLAIN ANALYZE`,
which executes the inner statement).

### Layer 2 — server-side default txn read-only

Every connection sets `default_transaction_read_only = on`. If the
parser somehow lets a write through, the database refuses it. The
session also sets `statement_timeout = 10000` as a coarse safety net.

### Layer 3 — explicit per-call transaction

`run_safe_query` opens an explicit transaction with `readonly=True`
*before* calling `SET LOCAL statement_timeout`. This is load-bearing:
asyncpg autocommits each `execute()` if no transaction is open, which
would discard `SET LOCAL` immediately. Without the explicit
transaction, the caller-supplied timeout would be silently dropped.

The result-set is truncated **in Python**, not via a synthetic
`LIMIT`. Rewriting `f"{sql} LIMIT N"` breaks `SHOW`, breaks user-supplied
`LIMIT`/`FETCH`, and is bypassed by a trailing `--` comment.

### Kubernetes safety

The k8s server has no helper for `delete`, `patch`, `apply`, or `exec`.
There is no flag to enable them. `kubernetes_asyncio.client.CoreV1Api`
exposes plenty of write methods; we simply never call them. If a future
contributor adds one, the absence of an existing safety harness should
make the review noisy.

`pod_logs` runs every line through `redact_secrets_from_logs` before
returning. Best-effort masking — anyone calling `kubectl logs` directly
sees the raw stream — but the chat agent shouldn't have a bearer
token in its context window where it might quote it back to the user
verbatim.

### Observability safety

Prometheus + Loki are HTTP GET only. PromQL itself is read-only; LogQL
likewise. The `escape_logql_label` helper exists so user-supplied label
values can't break out of `{label="…"}` matchers and inject a second
matcher.

## Composition: skills → tools

Skills (`skills/*/SKILL.md`) are Anthropic Agent Skills — markdown that
the Claude agent loads and follows. They reference tools by name; the
MCP servers expose those tools. A skill is a **playbook**, not code:

```
postgres-slow-query-triage:
  uses tools  → slow_queries, describe_table, vacuum_status, run_safe_query
  outputs     → templates/report.md (rendered by the agent)
  evals       → EVAL.md (pass/fail conditions, transcript greps)
```

The bundle ships skills that compose tools across servers
(`deploy-postmortem` reads from observability + k8s).
`redis-memory-pressure-triage` is a **companion skill**: it ships in
`skills/` but depends on external tooling (`redis-cli` on PATH, or a
community-built Redis MCP server the user wires in themselves). Its
SKILL.md frontmatter declares `requires_external_tooling: redis-cli`
so a harness can short-circuit if the dependency is missing.

## What's not in here

- **Write paths.** No skill or tool in this bundle ever writes to any
  backing system. If you need to apply a fix, the skill renders the
  SQL/kubectl/cli command and asks the user to run it.
- **Authn.** MCP servers inherit the auth of the connection string /
  kubeconfig / URL. Don't hand the bundle a superuser DSN; use a
  read-only role.
- **Audit logging.** The bundle doesn't keep its own audit trail.
  Postgres + the kube-apiserver have their own — that's where the
  source of truth lives.
- **Rate limiting / cost guards.** The Postgres server has a
  `statement_timeout` and a `row_cap`; everything else relies on the
  upstream's own protection. Production deployments behind a real
  agent should add a sidecar (e.g. `pgbouncer` for Postgres) for
  connection limits.
