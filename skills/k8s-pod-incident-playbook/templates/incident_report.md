# Pod incident report — {{ namespace }}/{{ pod }}

**When:** {{ timestamp }}
**Phase at investigation:** {{ phase }}
**Restart count:** {{ restart_count }}

## Hypothesis

**Primary:** {{ primary_hypothesis }}

**Evidence:**

{{ evidence_bullets }}

**Confidence:** {{ confidence }}  *(strong | moderate | weak)*

{{ secondary_hypothesis_section }}

## Containers

{{ container_table }}

## Last 60 minutes of OOM kills in {{ namespace }}

{{ oom_kill_table }}

## Notable events

{{ event_table }}

## Log excerpts (last 200 lines per container, filtered)

{{ log_excerpts }}

## Live resource usage (if metrics-server available)

{{ live_metrics }}

## What I did NOT change

This server is read-only. Recommended `kubectl` commands you may want to run:

{{ kubectl_recommendations }}

## Tool call audit

{{ tool_call_audit }}
