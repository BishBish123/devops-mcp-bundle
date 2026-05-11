# ADR-0002: `SET LOCAL` vs `SET SESSION` for statement timeout

- **Status:** accepted
- **Date:** 2025-11-19
- **Author:** Bishara Mekhaeil

## Context

`run_safe_query` takes a per-call `timeout_ms`. Postgres exposes
`statement_timeout` as a session-level GUC (Grand Unified
Configuration). We need each call to apply its own timeout *and* not
contaminate the next call on the same connection.

Three obvious shapes:

1. `SET statement_timeout = N` — session-level, persists for the
   life of the connection.
2. `SET LOCAL statement_timeout = N` — transaction-local, reverts at
   commit/rollback. Requires an open transaction.
3. `SET SESSION statement_timeout = N` then `RESET` after — same as
   (1) with explicit cleanup.

## Considered options

### A. SESSION-level + `RESET`

```python
await conn.execute(f"SET statement_timeout = {ms}")
try:
    rows = await conn.fetch(sql)
finally:
    await conn.execute("RESET statement_timeout")
```

- **Pro:** simple.
- **Con:** if the `RESET` is missed (exception during commit, network
  blip, asyncpg cancellation race), the connection is left with the
  caller's timeout. Connection pooling means the *next* caller
  inherits it.

### B. LOCAL inside an explicit transaction (chosen)

```python
async with conn.transaction(readonly=True):
    await conn.execute(f"SET LOCAL statement_timeout = {ms}")
    rows = await conn.fetch(sql)
```

- **Pro:** the `SET LOCAL` is bounded by the transaction. If the
  transaction commits, rolls back, or is interrupted, the timeout is
  gone. No persistent state to leak.
- **Pro (load-bearing):** asyncpg autocommits each `execute()` if no
  transaction is open. Without the explicit `transaction()` wrapper,
  `SET LOCAL statement_timeout = N` would commit immediately and
  apply to *nothing* — the timeout would be silently dropped, and the
  caller would see no error.
- **Con:** every `run_safe_query` opens a transaction even for trivial
  queries. The cost is one round-trip; we accept it.

### C. Application-side timeout via `asyncio.wait_for`

- **Pro:** independent of database semantics.
- **Con:** the query keeps running on the database after the client
  cancels — Postgres only learns the connection went away when it
  next tries to send. Bad for the resource we're trying to protect.

## Decision

**B.** Explicit transaction + `SET LOCAL`. Documented at the call
site in `queries.run_safe_query` because the failure mode of the
naive version is invisible.

## Consequences

- Every `run_safe_query` is a transaction. This is also why the
  connection pool's `default_transaction_read_only = on` flag is the
  right complement: combined with B, the database refuses to
  participate in a write under any circumstances.
- Layer 1 (sqlparse classifier) and Layer 2 (`transaction(readonly=True)`
  + `SET LOCAL statement_timeout`) are tested separately.
  `tests/test_postgres/test_queries_integration.py` covers:
    - SELECT round-trip + result shape,
    - `row_cap` enforcement,
    - argument validation (`timeout_ms <= 0`, `row_cap <= 0`),
    - `SET LOCAL statement_timeout` cancellation: `SELECT pg_sleep(2)`
      with `timeout_ms=100` raises `QueryCanceledError`,
    - classifier-miss DB rejection: an `INSERT` issued inside a
      `transaction(readonly=True)` raises
      `ReadOnlySQLTransactionError` (Postgres SQLSTATE 25006).
  These run only when `POSTGRES_DSN` points at a live database (CI
  spins up a `postgres:16` service container, matching the local
  `docker-compose.yml`); they're skipped on developer machines without
  Docker.

## Follow-ups

- If we ever expose a non-`run_safe_query` write tool (we won't), the
  same `SET LOCAL` pattern applies — but the transaction would be
  read-write and the parser-classifier would have to be flagged to
  permit it.
