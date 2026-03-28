# ADR-0004: Loki vs CloudWatch Logs as the log backend

- **Status:** accepted
- **Date:** 2026-03-04
- **Author:** Bishara Mekhaeil

## Context

The observability server ships a `loki_query` tool and (deliberately)
not a `cloudwatch_logs_query` one, even though most production
deployments at the user's day-job scale run on AWS and pipe logs into
CloudWatch.

This ADR records why.

## Considered options

### A. Loki (chosen)

- **Pro:** open API. LogQL is documented, public, and the
  `query_range` endpoint is a single HTTP GET. Test setup is trivial:
  `httpx.MockTransport` returns canned bodies.
- **Pro:** label-based selection mirrors PromQL. The same agent that
  can read Prometheus already understands `{app="api", level="error"}`
  matchers.
- **Pro:** runs locally for dev. `make up` brings up Loki + Promtail
  in a compose file; demos work without an AWS account.
- **Con:** smaller install base than CloudWatch in cloud-native
  shops. Some users will need to deploy Loki to use this server.

### B. CloudWatch Logs

- **Pro:** ubiquitous on AWS.
- **Con:** logs-insights query language (CWLI) is its own dialect;
  agent has to learn a third syntax beyond PromQL/LogQL.
- **Con:** requires `boto3` + IAM, doubling the bundle's auth
  story. The MCP server would need to handle SigV4 signing and STS
  assume-role rotation.
- **Con:** rate limits + per-call costs ($0.005 per GB scanned for
  Logs Insights) make agent fanout expensive in a way Loki isn't.

### C. Both (Loki primary, CloudWatch behind a feature flag)

- **Pro:** users get to pick.
- **Con:** doubles the test surface, the docs surface, and the skill
  templates' required tools list. The skills are written assuming
  *one* log backend they can name; "if Loki then `loki_query` else
  `cloudwatch_logs_query`" is an LLM footgun.

## Decision

**A.** Loki only. CloudWatch users can layer in a community
[`mcp-server-aws`](https://github.com/modelcontextprotocol/servers)
or write their own; the skills here will keep referencing Loki by name.

## Consequences

- The `deploy-postmortem` skill's playbook says `loki_query` directly.
  If the user has CloudWatch and not Loki, the skill won't run as-is.
- The README documents the dependency clearly. The compose file
  spins Loki up locally so demos work without external infra.
- If a future ADR-0005 reverses this, the seam is clean: `loki_query`
  is the only log-backend tool, and the skills mention it by name in
  exactly two places (`deploy-postmortem/SKILL.md` and the README).

## Follow-ups

- If a user files an issue asking for CloudWatch, point them at this
  ADR before writing code. The community AWS MCP server is the right
  layering boundary, not "add a second backend to this bundle".
