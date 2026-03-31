# Security model

This bundle is built around a single property: **read-only by
construction**. Every tool, every skill, every code path. The threat
model below describes what the bundle does and does not protect
against.

## Reporting a vulnerability

Email **bisharaayoub12@gmail.com** with the subject line
`[devops-mcp-bundle] security`. I will acknowledge within 72 hours.
For low-severity issues, opening a GitHub issue is fine; for anything
that could enable a write to a production system, please disclose
privately first.

## Threat model

| Threat | In scope | Mitigation |
| --- | --- | --- |
| Agent issues SQL that writes to Postgres | ✅ | sqlparse classifier + `default_transaction_read_only=on` (two layers) |
| Agent issues SQL that exhausts a connection | ✅ | `SET LOCAL statement_timeout` per call + `row_cap` in Python |
| Agent issues SQL via CTE-write trick | ✅ | classifier flat-scans tokens, rejects `WITH x AS (INSERT …)` |
| Agent runs `EXPLAIN ANALYZE` (executes the inner stmt) | ✅ | classifier rejects `ANALYZE` keyword anywhere in the body |
| Agent calls `kubectl exec` / `delete` / `patch` | ✅ | no MCP tool exists; bundle has no shell-out path |
| Bearer token leaks from k8s logs into agent context | ✅ | `redact_secrets_from_logs` masks `key=value` and `key: value` shapes |
| Untrusted Loki label value breaks out of matcher | ✅ | `escape_logql_label` + `render_logql` template helper |
| User points at a malicious Prometheus/Loki URL | ⚠️  partial | bundle treats responses as untrusted JSON, validates envelope, but won't catch a malicious payload designed to exhaust the agent's context |
| Credentials leak from a wrongly-configured ConfigMap | ⚠️  partial | `list_configmaps` returns key names only; reports keys whose names look secret-shaped in `redacted_keys` |
| Agent accidentally fans out queries until the DB is hot | ❌ | not handled — use a connection pooler / cost guard upstream |
| User provides a superuser DSN | ❌ | use a read-only role; the bundle's safety helps but the principle of least privilege is on you |
| Supply-chain attack via dependencies | ❌ | dependencies pinned in `uv.lock`; review changes |

## What "read-only" means precisely

For Postgres: every statement that reaches the database executes with
`default_transaction_read_only = on` set on the session, and the SQL
itself has been parsed and classified as one of `SELECT`, `EXPLAIN`,
`WITH`, `SHOW`, `VALUES` (with no mutating keyword anywhere in the
flat token stream).

For Kubernetes: no helper in `src/devops_mcp_bundle/k8s/queries.py`
calls `delete_*`, `patch_*`, `replace_*`, `create_*`, `connect_*`
(exec/portforward/attach) or any subresource that mutates. `pod_logs`
is a streaming read; it does not start a session.

For Prometheus + Loki: HTTP GET only. PromQL is a read language;
LogQL is a read language. There are no `POST`/`PUT`/`DELETE` calls
anywhere in `src/devops_mcp_bundle/observability/`.

## Defense in depth

You should assume the parser-classifier might be wrong on some
sufficiently exotic input — that's why the database session is also
configured `read_only`. Likewise, you should assume the secret-redaction
heuristic in `redact_secrets_from_logs` might miss a key it doesn't
recognise — that's why the agent should never be granted permission to
run `kubectl logs` directly outside the bundle.

If you're deploying this in front of a production database, run as a
dedicated read-only role (`CREATE ROLE mcp_reader … LOGIN`; `GRANT
USAGE ON SCHEMA public TO mcp_reader; GRANT SELECT ON ALL TABLES …`)
and disable `pg_stat_statements` if your privacy policy doesn't permit
the LLM seeing query texts.

## Out of scope

- **Auditing.** The bundle does not keep its own audit log. Postgres
  has `pg_stat_statements`; the kube-apiserver has audit policies; use
  those.
- **PII redaction beyond log-secret heuristics.** The bundle does not
  attempt to scrub PII from query results or log lines. If your data
  is sensitive enough that the LLM seeing it is a concern, scope the
  read-only role to a non-sensitive view.
