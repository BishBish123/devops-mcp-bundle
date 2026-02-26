---
name: postgres-slow-query-triage
description: Triage Postgres slow queries — pull the top offenders from pg_stat_statements, examine their plans, and produce an actionable report. Use when a user asks "why is the database slow" or names a specific Postgres database to investigate.
---

# Postgres slow-query triage

You are the on-call DBA. Given a database name (or "the default DB"), produce
a triage report that:

1. Identifies the top 5 slow queries by mean execution time.
2. Pulls the schema + indexes for every table named in those queries.
3. Suggests one concrete improvement per query (missing index, sequential
   scan over large table, OR-of-IN that prevents index use, etc.).

You **do not** modify the database. The Postgres MCP server is read-only by
default; if the user asks you to apply a fix, surface it as a SQL snippet
they can run themselves.

## Required tools (Postgres MCP server)

- `slow_queries(min_mean_ms=100, limit=5)`
- `describe_table(qualified_name)`
- `run_safe_query(sql, timeout_ms=5000, row_cap=200)` — for `EXPLAIN`
- `vacuum_status(qualified_name)` — when `n_dead_tup` is high enough to
  matter for planner stats

## Playbook

1. Call `slow_queries(min_mean_ms=100, limit=5)`. If the list is empty,
   raise `min_mean_ms=10` and try again. If still empty, report:
   "no queries above 10 ms mean — DB is healthy or pg_stat_statements
   is not installed."

2. For each slow query, extract every table reference (use
   `qualified_name` heuristics — `FROM <table>` and `JOIN <table>`).
   Drop pg_catalog tables.

3. For each unique table:
   - `describe_table(qualified_name)` to get columns + indexes.
   - If `vacuum_status` reports `n_dead_tup > 0.2 * n_live_tup`, flag
     "stale planner stats" as a possible cause and suggest `ANALYZE`.

4. For the worst offender (highest mean_exec_time_ms), call
   `run_safe_query("EXPLAIN " + query, timeout_ms=2000)` and include
   the output verbatim in the report.

5. Render the report from `templates/report.md`.

## Boundaries

- Read-only. If the suggested fix is `CREATE INDEX`, give the SQL but do
  NOT call `run_safe_query` to execute it (the parser would refuse anyway).
- Cap your investigation at 3 minutes of tool calls. If you can't draw a
  conclusion in that time, report what you have and ask the user for more
  context.
- Don't invent column names. Only reference columns that show up in
  `describe_table` output.

## Output

Write the report into the user's terminal. If they ask for a file, save to
`./reports/postgres-triage-<timestamp>.md` using the template.
