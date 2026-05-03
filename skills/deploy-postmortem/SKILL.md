---
name: deploy-postmortem
description: Draft a deploy postmortem from observability + k8s evidence. Use when the user mentions a recent deploy, names a service that just shipped, or asks "did the deploy go badly". Produces a structured doc with timeline, evidence, and follow-ups, never recommends action without evidence.
---

# Deploy postmortem drafter

You write the *first* draft of a deploy postmortem so the engineer who
deployed has a starting point instead of a blank page. The draft is
**evidence-led**: every claim about what happened cites a tool call.

You **do not** modify the cluster, the database, or any external service.
Postmortems are after-the-fact reconstructions, not interventions.

## Required tools

From the **observability** server:
- `prom_query`, `prom_range`, `prom_alerts`
- `loki_query`
- `compare_windows` — *the* killer tool for "is this metric different
  than before the deploy?"
- `slo_status` — when the user asks "did we burn budget?"

From the **k8s-inspector** server (optional but useful):
- `recent_oomkills(namespace, since_min)` — OOM kills since deploy time
- `pod_events(namespace, name)` for pods rolled out

## Required user input

You need at least one of:
- A deploy timestamp (RFC3339 or "30 minutes ago")
- A service name (so PromQL can scope `job=<service>`)
- A namespace (so you can use the k8s tools)

If the user gives none of these, ask for the deploy time first.

## Playbook

1. **Establish the deploy window.** Set `deploy_at` from the user input.
   The "before" window is the 1h ending at `deploy_at`; the "after"
   window is the 1h starting at `deploy_at`. Mention both windows
   explicitly in the report so the reader knows what you compared.

2. **Compare the four golden signals** with `compare_windows(promql_a,
   promql_b, label_a, label_b)`. The tool runs both PromQL expressions
   instantly and returns the delta + percent change — there's no
   separate `before` / `after` time argument, so the *time window*
   has to live inside each PromQL string via `[5m]` rate windows and
   an explicit `offset 1h` on the "before" side.

   For each signal, build two PromQLs of the same shape, one with
   `offset 1h` and one without. Example for error rate on
   `job="api"`:

   ```
   promql_a = 'sum(rate(http_requests_total{job="api",code=~"5.."}[5m]))'
   promql_b = 'sum(rate(http_requests_total{job="api",code=~"5.."}[5m] offset 1h))'
   compare_windows(promql_a, promql_b, label_a="after", label_b="before")
   ```

   Repeat for the three remaining signals (use the same `[5m]` rate
   window and `offset 1h` so the comparison is apples-to-apples):

   - Latency: `histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job=...}[5m])))`
   - Traffic: `sum(rate(http_requests_total{job=...}[5m]))`
   - Saturation: `max(node_memory_MemUsed_bytes / node_memory_MemTotal_bytes)`

3. **Check active alerts** with `prom_alerts`. Filter to those whose
   `severity` is `page` and whose `started_at` is within the deploy
   window.

4. **Pull error logs** with `loki_query`. Build the LogQL with
   `render_logql` so the service name is escaped:

   ```python
   from devops_mcp_bundle.observability.queries import render_logql
   logql = render_logql('{{job="{job}"}} |= "ERROR"', job=service)
   loki_query(logql, since="1h", limit=50)
   ```

   Never interpolate `service` into a raw f-string — `escape_logql_label`
   exists to prevent matcher break-out. (If you have a fixed
   service name you've already validated, raw is fine; for any value
   that came from the user, route it through `render_logql`.)

5. **If a namespace was given**, `recent_oomkills(namespace, since_min)`
   to see if anything got killed by the rollout. Pick `since_min` to
   cover the "after" window (60 if the window is 1h).

6. **Render the report** from `templates/postmortem.md`. Lead with the
   timeline, then the golden-signals comparison table, then the
   evidence dump.

## Boundaries

- Never declare an incident *resolved*. That's a human's call.
- Never recommend a rollback without explicit evidence: at least one
  golden signal worse by ≥ 2× *and* a corroborating alert or error log.
- If the evidence is mixed or absent, say so ("no signal of regression
  in the data I queried").
- Don't synthesise PromQL queries the user couldn't have written. If
  you need a query the user hasn't given you a recipe for, ask them
  for it.

## Output

Render the markdown postmortem to the terminal. If the user wants a
file, save to `./postmortems/<service>-<deploy_at>.md`.
