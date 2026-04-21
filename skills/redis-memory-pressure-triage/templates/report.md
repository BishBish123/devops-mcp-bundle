# Redis memory triage — `{{ instance }}`

_Generated {{ generated_at }} by `redis-memory-pressure-triage`._

## Headline

| Metric | Value |
| --- | --- |
| `used_memory_human` | {{ used_memory_human }} |
| `used_memory_rss_human` | {{ used_memory_rss_human }} |
| `mem_fragmentation_ratio` | {{ frag_ratio }} |
| `maxmemory_human` | {{ maxmemory_human }} |
| `maxmemory_policy` | `{{ maxmemory_policy }}` |
| `evicted_keys` (since boot) | {{ evicted_keys }} |

{% if evicted_keys > 0 %}
**Cluster is evicting.** The `{{ maxmemory_policy }}` policy is dropping
keys because `used_memory` hit `maxmemory`. Latency-sensitive callers
will be seeing more cache misses than usual.
{% endif %}

## Top-10 offenders

| Key | Type | Size | TTL | Idle | Recommendation |
| --- | --- | --- | --- | --- | --- |
{% for key in top_keys %}
| `{{ key.name }}` | {{ key.type }} | {{ key.size_human }} | {{ key.ttl_human }} | {{ key.idle_human }} | {{ key.recommendation }} |
{% endfor %}

## Suggested commands (run yourself)

```bash
# Sampled big-key scan (safe on prod, samples 1k keys per type)
redis-cli --bigkeys --memkeys

# Memory accounting for one suspect key
redis-cli MEMORY USAGE "{{ first_key }}" SAMPLES 5

# Read-only allocator-level diagnostic
redis-cli MEMORY DOCTOR
```

> Fragmentation defrag is intentionally **not** suggested here:
> `MEMORY PURGE` mutates allocator state and is excluded by the
> read-only contract. If `mem_fragmentation_ratio > 1.5` and is stable,
> schedule a rolling restart of the instance instead.

## What this report does **not** do

- It does not delete or expire any keys.
- It does not change `maxmemory` or `maxmemory-policy`.
- It does not run `FLUSHDB` or `FLUSHALL`. Ever.

If a key in the top-10 is genuinely safe to drop, the SQL-shaped
recommendation column shows the exact `UNLINK` command — copy and run
it manually after a second pair of eyes.
