# Eval — `k8s-pod-incident-playbook`

How to verify the skill is doing the right thing.

## Pass conditions

After the agent runs the skill on a crash-looping pod, the report **must**:

1. State the pod's current `phase` and `restart_count`. If the pod is
   `Running` and ready, the report says "pod looks healthy" and stops
   here — no fishing.
2. Pull the most recent `pod_logs` (default `tail=200`) and call out the
   last 1–3 error lines. Redact anything matching the secret-key
   heuristic (the bundle's `redact_secrets_from_logs` helper does this).
3. List `pod_events` filtered by `involved_object`, prioritising
   `OOMKilled`, `Failed`, `BackOff`, `FailedScheduling` reasons.
4. Propose **one** root cause hypothesis, not three. If the signals are
   ambiguous, say "I can't tell — here are the two most likely causes
   and the kubectl command that would distinguish them".

## Fail conditions

Mark the run a fail if:

- The agent invoked `kubectl exec`, `kubectl delete`, `kubectl rollout
  restart`, or anything mutating. The k8s MCP server in this bundle
  exposes none of those — a fail here means the agent fell back to a
  shell tool.
- A redacted secret appears in the report (e.g. `password=hunter2` from
  a log line). Cross-check by grepping the report for known secret
  prefixes.
- The report invents a container name that wasn't in `describe_pod`.
- The report cites events older than the pod's `creation_timestamp`
  (the field `describe_pod` returns from `metadata.creationTimestamp`).
  Events from a previous incarnation of the pod's name are almost
  always misleading; the pod-restart timestamp itself isn't on the
  k8s tool surface, but the pod's last creation is, and any event
  older than that definitely belongs to a prior incarnation.

## Sanity-check transcript shape

```bash
grep -E "kubectl (exec|delete|rollout|patch|apply|edit)" transcript.jsonl
# Expected: zero matches.
```

```bash
grep -E "list_pods|describe_pod|pod_logs|pod_events" transcript.jsonl
# Expected: at least describe_pod + pod_logs + pod_events for the
# target pod.
```

## Edge cases the agent should handle

| Scenario | Expected behaviour |
| --- | --- |
| Pod's container is `Pending` (no node fits) | Skip logs; head straight to `pod_events` to find the `FailedScheduling` reason. |
| `OOMKilled` event on the same container in the last hour | Lead with "OOMKilled — container hit its memory limit". Don't bury this in a list. |
| `ImagePullBackOff` with auth error | Recommend checking the imagePullSecret; do not guess at registry credentials. |
| Pod is missing entirely | Report "pod `name` not found in namespace `ns`"; offer the `list_pods` output so the user can pick the right one. |
