# Postgres slow-query triage — {{ database }}

**When:** {{ timestamp }}
**Threshold:** queries with mean ≥ {{ min_mean_ms }} ms
**Top offenders inspected:** {{ n_queries }}

## Summary

{{ one_line_summary }}

## Top {{ n_queries }} slow queries

| Rank | Mean (ms) | Calls | Total (ms) | Query (truncated) |
| ---: | ---: | ---: | ---: | --- |
{{ slow_query_table }}

## Per-query findings

{{ per_query_findings }}

## Suggested actions

{{ suggested_actions }}

## What I did NOT change

This server is read-only — every `CREATE INDEX` / `ANALYZE` suggestion
above is presented as SQL for you to apply. Nothing was executed against
the database beyond the read queries listed at the bottom of this report.

## Tool call audit

{{ tool_call_audit }}
