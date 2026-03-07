---
name: redis-memory-pressure-triage
description: Triage Redis memory pressure — identify big keys, expiring keys, and memory fragmentation using read-only MEMORY commands. Use when a user asks "why is Redis OOM-ing", "what's eating Redis memory", or names a Redis instance that's hot.
---

# Redis memory-pressure triage

You are the on-call DBA for a Redis instance that's running hot on memory.
Produce a triage report that covers:

1. Current memory footprint (`used_memory`, `used_memory_rss`, fragmentation
   ratio).
2. Top-10 keys by RSS, with idle time and TTL.
3. Whether eviction is happening (`maxmemory-policy`, `evicted_keys`).
4. One concrete recommendation per offender.

You **do not** write to Redis. The Postgres MCP server's `run_safe_query`
contract applies in spirit — every Redis command this skill uses is
read-only (`INFO`, `MEMORY USAGE`, `OBJECT IDLETIME`, `TTL`,
`DEBUG OBJECT`, `--bigkeys` is a sampled scan; never `DEL`, `FLUSHDB`,
`FLUSHALL`, `EXPIRE`).

## Required tools

This skill expects the user has Redis CLI access (`redis-cli`) on their
own machine. The MCP servers in this bundle don't shell out to Redis;
the skill exports a recipe the user can paste into their terminal, plus
a structured output format to feed back into the agent.

If the user has wired in a Redis MCP server (community-built), the skill
prefers calling its read-only tools:

- `redis_info(section="memory")`
- `redis_memory_usage(key, samples=5)`
- `redis_object_idletime(key)`
- `redis_ttl(key)`
- `redis_random_key()` — for sampling when `KEYS *` is too dangerous

## Playbook

1. Start with `INFO memory`. Pull `used_memory_human`, `used_memory_rss_human`,
   `mem_fragmentation_ratio`, `maxmemory_human`, `maxmemory_policy`,
   `evicted_keys`. If `evicted_keys > 0` and growing, that's the smoking gun —
   the cluster is already at maxmemory.

2. Find big keys. Two strategies, in order of preference:
   - `redis-cli --bigkeys --memkeys` — sampled scan, safe to run on prod.
     Captures the largest 1 key per type.
   - `redis-cli --memkeys-samples 1000` — wider sample, still safe.
   - **Avoid** `KEYS *` on production — it blocks the event loop on a
     large dataset. If the cluster is small (`<10k keys`), it's fine.

3. For the top-10 keys:
   - `MEMORY USAGE <key> SAMPLES 5` for collection types (lists, sets,
     hashes, sorted sets, streams) — returns bytes including overhead.
   - `OBJECT IDLETIME <key>` — seconds since the key was last accessed
     (useful for "is anybody actually reading this?").
   - `TTL <key>` — `-1` means no expiry. Keys that should expire but
     don't are the easiest wins for memory pressure.

4. Classify offenders:
   - **Stale** (`OBJECT IDLETIME > 7d`): can probably be deleted.
   - **No-TTL** (`TTL == -1`): should usually have an expiry; ask the
     team what the intended lifetime is.
   - **Pathologically large** (>1 MB single key): consider sharding,
     splitting into hash-of-hashes, or moving to Postgres.
   - **High fragmentation** (`mem_fragmentation_ratio > 1.5` and stable):
     not a key problem — issue an `MEMORY PURGE` (it's read-only-ish,
     it just compacts) or schedule a restart.

5. Render the report from `templates/report.md`.

## Boundaries

- Read-only. The skill never proposes `DEL`, `EXPIRE`, `UNLINK`,
  `FLUSHDB`, `MEMORY DOCTOR` (gives advice but is fine), or `CONFIG SET`
  as auto-applied actions. These appear in the report as **suggested**
  commands the user can run.
- `--bigkeys` is sampled — it can miss the actual largest key in a
  highly skewed dataset. Note this caveat in the report.
- `MEMORY USAGE` on a 10M-element list takes seconds. If the user's
  Redis has very large keys, set `SAMPLES 0` (full scan) only on a
  replica.
- Don't recommend `MAXMEMORY` increases as the first answer. Memory is
  a symptom; the cause is usually a missing TTL or a runaway producer.

## Output

Render `templates/report.md` with:

- Memory headline (used / rss / fragmentation)
- Eviction status
- Top-10 offending keys
- Per-key recommendation
- Three "what to do next" candidate commands the user can run themselves
