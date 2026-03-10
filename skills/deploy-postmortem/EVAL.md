# Eval — `deploy-postmortem`

How to verify the skill is doing the right thing.

## Pass conditions

After the agent runs the skill, the postmortem **must**:

1. Bound the time window. The user typically gives a deploy timestamp
   (or a commit SHA the skill resolves to one); the report uses
   `compare_windows` with the deploy time as the boundary.
2. Show the before/after delta for at least 3 metrics: error rate,
   latency p99, request rate. Everything else is optional but should
   reuse the same window.
3. Cross-reference Loki logs from the post-deploy window for the same
   service. If `loki_query` returns more error-level entries after the
   deploy than before by `> 2x`, flag the deploy as "regressed".
4. End with a "what to do next" section, with one option per row:
   investigate further / roll back / accept regression with caveat.

## Fail conditions

Mark the run a fail if:

- The agent didn't run `compare_windows` or any equivalent before/after
  comparison. A postmortem without a delta is a status report.
- The window of "after" is shorter than "before" without justification.
  (This skews the comparison toward the noisier window.)
- The report claims the deploy regressed without showing the metrics
  that did. Conclusions must follow numbers in the same report.
- The agent ran a write command — there are none in this bundle, so any
  write attempt is a regression to fall back behaviour we don't want.

## Sanity-check transcript shape

```bash
grep -E "compare_windows|prom_query|prom_range" transcript.jsonl
# Expected: at least one compare_windows or two prom_query for the
# before/after pair.
```

```bash
grep -E "loki_query" transcript.jsonl
# Expected: at least one log-side check.
```

## Edge cases the agent should handle

| Scenario | Expected behaviour |
| --- | --- |
| Deploy was less than 5 minutes ago | Either widen the "after" window to ≥ 15 min before claiming regression, or say "too soon to tell". |
| User gives a SHA, not a timestamp | Resolve via `kubectl rollout history` or git; if neither works, ask. |
| Service has no SLO defined | Don't fabricate one. Compute deltas in absolute units (ms, %), not as "burn rate". |
| Multiple deploys in the window | Either pick the user-named deploy explicitly, or show all of them on the timeline and let the user say which. |
