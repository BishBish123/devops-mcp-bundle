# ADR-0001: sqlparse vs regex for SQL safety classification

- **Status:** accepted
- **Date:** 2025-11-12
- **Author:** Bishara Mekhaeil

## Context

`postgres-dba`'s `run_safe_query` tool takes user-supplied SQL. The
agent (or its user) might be malicious or merely careless, so the
bundle needs to refuse anything that isn't a single read-only
statement before it ever reaches Postgres.

The naive approach is a regex against the leading word of the SQL —
`r"\s*(SELECT|EXPLAIN|WITH|SHOW)"`. It's a one-liner. Tempting.

## Considered options

### A. Regex on the leading keyword

- **Pro:** trivial, no dependency, fast.
- **Con:** misses `WITH inserted AS (INSERT … RETURNING *) SELECT *
  FROM inserted` — a documented Postgres trick that lets a `WITH`
  statement *write*. Misses `EXPLAIN ANALYZE`, which executes the
  inner statement. Misses multi-statement payloads
  (`SELECT 1; DROP TABLE x`) unless the regex is anchored carefully,
  and even then any inline comment can confuse anchoring. Misses
  dollar-quoted code blocks. Each individual gap is fixable; the gaps
  collectively are not.

### B. sqlparse-based classifier (chosen)

- **Pro:** handles tokenisation correctly. Correctly identifies CTE
  inner statements, EXPLAIN bodies, multi-statement input, and quoted
  literals. The library is permissively licensed and ~5kloc — small
  enough to read.
- **Con:** sqlparse is a *parser*, not a Postgres parser. It can be
  wrong on Postgres-specific syntax. We accept this by layering
  server-side `default_transaction_read_only=on` on top — so a parser
  miss can still not write to the DB.

### C. Use Postgres's own parser via libpg_query

- **Pro:** the database's own grammar; impossible to be wrong.
- **Con:** C-extension binding, complicates packaging, and the
  bundle's testing matrix doubles. Overkill for the read-only/write
  distinction we actually need.

## Decision

**B.** sqlparse is good enough as the first layer; the database's own
`read_only` flag is the second.

## Consequences

- The `sqlparse` dependency is the bundle's only non-trivial parsing
  cost (it's pure-python, no C extensions).
- Tests live in `tests/test_postgres/test_safety.py` and
  `tests/test_postgres/test_classifier.py`. Every novel attack on the
  classifier should land as a regression test there.
- The classifier is the single source of truth: server.py calls
  `classify_sql` once per request, and the result drives both the
  refusal-or-execute branch *and* the human-readable error message.

## Follow-ups

- If we ever ship a write-capable tool, `is_read_only_sql` is the only
  reasonable place to add a `mode={"read","write"}` flag.
- ADR-0002 covers the related question of `SET LOCAL` vs
  `SET SESSION` for the timeout — that's the *enforcement* side of
  the same safety story.
