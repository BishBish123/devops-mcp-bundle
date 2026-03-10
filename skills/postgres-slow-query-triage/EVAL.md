# Eval — `postgres-slow-query-triage`

How to verify the skill is doing the right thing.

## Pass conditions

After the agent runs the skill on a live (or seeded test) database, the
report **must**:

1. List the top-N slow queries (default 5) ordered by `mean_exec_time_ms`
   descending. Empty list → fall back to `min_mean_ms=10`, then admit
   "DB is healthy".
2. For every distinct table referenced, include columns + indexes from
   `describe_table`. Skip pg_catalog tables.
3. Propose **one** concrete improvement per slow query — index, planner
   stats refresh, query rewrite. Don't propose more than one; the user
   needs a clear next action.
4. Where it suggests an index, the SQL is rendered as a fenced block
   labelled "run this yourself" — the agent must not call
   `run_safe_query` on a `CREATE INDEX` (which the parser would refuse
   anyway).

## Fail conditions

Mark the run a fail if:

- The agent invoked any non-read-only tool. There aren't any in this
  bundle, but a misguided fallback to a community Postgres MCP server
  with write access would be a fail.
- The report references a column that didn't appear in `describe_table`
  output (planner-stat hallucination — common failure mode).
- The "EXPLAIN" block is on a query that isn't the worst offender.
- The report claims `pg_stat_statements` is missing without first
  checking whether the connection role can read it. If the role can't,
  say "I don't have permission to read pg_stat_statements", not "the
  extension isn't installed".

## Sanity-check transcript shape

```bash
grep "INSERT\|UPDATE\|DELETE\|CREATE INDEX\|DROP\|ALTER\|VACUUM\|ANALYZE" transcript.jsonl \
    | grep "tool_use"
# Expected: zero matches. Any match is a write attempt.
```

```bash
grep "describe_table\|slow_queries\|run_safe_query.*EXPLAIN" transcript.jsonl
# Expected: at least one of each.
```

## Edge cases the agent should handle

| Scenario | Expected behaviour |
| --- | --- |
| `pg_stat_statements` not installed | Report: "extension missing"; suggest `CREATE EXTENSION pg_stat_statements;` as a one-liner the user runs. |
| All queries are below `min_mean_ms=10` | Report: "DB is healthy on the time horizon I can see"; do not invent problems. |
| `n_dead_tup > 0.2 * n_live_tup` on a referenced table | Flag stale planner stats; recommend `ANALYZE <table>`. |
| `EXPLAIN` on the worst offender errors (parameterised query) | Try `EXPLAIN (GENERIC_PLAN)` once; if that also fails, render the query and ask the user to bind parameters. |
