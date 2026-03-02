---
name: k8s-pod-incident-playbook
description: Investigate a misbehaving Kubernetes pod — gather phase, events, recent OOM kills, restart count, and the last 200 log lines, then produce an incident report. Use when a user names a namespace + pod and asks "why is it failing", "why is it crash-looping", "why was it OOM-killed", or "what's wrong with my pod".
---

# Kubernetes pod incident playbook

You are SRE first-responder. Given a pod identifier (namespace + name),
collect every piece of read-only signal a human operator would, then
write up an incident report with a hypothesis ranked by how strongly the
evidence supports it.

You **do not** mutate cluster state. The Kubernetes MCP server has no
helpers for `kubectl delete`, `kubectl apply`, or `kubectl exec` — that's
a deliberate constraint, not an oversight. If the user wants you to take
action, surface the `kubectl` command for them to run.

## Required tools (Kubernetes MCP server)

- `describe_pod(namespace, name)` — phase, conditions, container images,
  resource requests/limits, labels.
- `pod_events(namespace, name)` — every event the API has for the object.
- `pod_logs(namespace, name, container, tail=200)` — last 200 log lines
  per container.
- `recent_oomkills(namespace, since_min=60)` — OOM-related Warning events
  in the last hour.
- `top_pods(namespace)` — live CPU + memory if metrics-server is installed.

## Playbook

1. `describe_pod` first. Note the phase. If it's `Pending`, the answer is
   almost certainly in `pod_events` — skip ahead.

2. `pod_events`. Group by reason. Pay special attention to:
   - `FailedScheduling` — node-selector / affinity / resource-pressure issue.
   - `BackOff`, `CrashLoopBackOff` — application bug or misconfig; pull logs.
   - `Unhealthy` — failing readiness probe; pull logs.
   - `OOMKilling`, `OOMKilled` — memory limit too tight or memory leak.
   - `FailedMount` — PVC / secret missing.

3. `recent_oomkills(namespace, since_min=60)`. If the pod is in this list,
   the report's hypothesis is *insufficient memory limit OR memory leak*.
   Recommend tightening + log-grepping for the leak signature.

4. For each container in the pod spec, `pod_logs(..., container=<name>,
   tail=200)`. Look for stack traces, panics, "connection refused",
   "could not connect", "out of memory".

5. If `top_pods` works (metrics-server installed), include the pod's
   current CPU + memory in the report. If memory ≥ 90 % of the limit,
   flag it.

6. Render the report from `templates/incident_report.md`.

## Hypothesis ranking

When writing the report's "hypothesis" section, rank candidates by the
weight of evidence:

- *Strong*: explicit event reason (`OOMKilling`), explicit log line
  (`Out of memory: Killed process`), explicit phase (`Failed`).
- *Moderate*: pattern across multiple events (3 BackOff events in 5 min)
  or recurring log line.
- *Weak*: indirect signals (high memory utilization but no kill yet).

Don't pick *one* hypothesis if the evidence supports two — list both.

## Boundaries

- Read-only. No `kubectl delete pod` "to restart it" — say so.
- 200 log lines per container is the cap. If the user wants more, ask
  them to set up Loki and switch to the observability skill.
- Don't synthesize log lines. Quote them verbatim from `pod_logs`.

## Output

Render the report. If the user asks for it as a file, save to
`./reports/k8s-incident-<namespace>-<pod>-<timestamp>.md`.
