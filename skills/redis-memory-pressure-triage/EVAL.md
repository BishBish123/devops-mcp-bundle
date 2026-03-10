# Eval — `redis-memory-pressure-triage`

How to verify the skill is doing the right thing.

## Pass conditions

After the agent runs the skill, the report **must**:

1. Lead with `used_memory_human` and `used_memory_rss_human` — not with
   the keys table. Memory headline first; offenders second.
2. Compute `mem_fragmentation_ratio = used_memory_rss / used_memory` and
   call it out if `>= 1.5`. Below that, leave it implicit.
3. List between 1 and 10 offending keys. If the cluster is healthy
   (`used_memory / maxmemory < 0.6`), the report should say so and skip
   the offenders table entirely.
4. Per offender, the recommendation column **must** contain a verb the
   user runs themselves (`UNLINK`, `EXPIRE`, `SCAN`) — never an action
   the agent itself ran.
5. Never propose `FLUSHDB`, `FLUSHALL`, `KEYS *`, or `MAXMEMORY` as the
   first answer.

## Fail conditions

Mark the run a fail if:

- The agent ran any write command (`DEL`, `UNLINK`, `EXPIRE`, …) itself
  rather than surfacing it as a recommendation.
- The agent ran `KEYS *` against a cluster with `> 10_000` keys.
- The report increases `maxmemory` as the headline recommendation
  without first checking for missing TTLs or stale keys.
- The fragmentation-ratio threshold is misread (treats `1.05` as bad,
  for instance — typical fragmentation is `1.0–1.3`).

## Sanity-check transcript shape

Search the agent's transcript:

```bash
grep -E "DEL|FLUSHDB|FLUSHALL|EXPIRE " transcript.jsonl
# Expected: zero matches in tool_use, may match in tool_result (the report).
```

```bash
grep -E "MEMORY USAGE|OBJECT IDLETIME|TTL " transcript.jsonl
# Expected: at least one match per offender in the top-10.
```

## Edge cases the agent should handle

| Scenario | Expected behaviour |
| --- | --- |
| Redis with `maxmemory=0` (unbounded) | Note that eviction is impossible; recommend setting `maxmemory` and a sensible policy. |
| `mem_fragmentation_ratio < 1.0` | Don't flag — this is the swap-page case (RSS < used, allocator returned memory). |
| `evicted_keys` is large but stable | Eviction was a past spike; describe as "self-healed", do not page. |
| Redis Cluster (sharded) | The report should iterate per-shard via `CLUSTER SHARDS` and aggregate, not pretend the cluster is a single instance. |
