# Deploy postmortem — {{ service }} @ {{ deploy_at }}

> **Status:** Draft. The drafter only reads observability + k8s state; an
> engineer must mark this resolved and assign follow-ups.

## Timeline

| When | What |
| --- | --- |
{{ timeline_table }}

## Golden signals (compared `before` deploy vs `after`)

| Signal | Before | After | Δ | % | Verdict |
| --- | ---: | ---: | ---: | ---: | --- |
{{ golden_signals_table }}

## Active alerts in the deploy window

{{ alerts_section }}

## Error log excerpts

{{ error_log_section }}

## OOM kills since deploy

{{ oom_kill_section }}

## Verdict

{{ verdict }}

## Suggested next steps

{{ next_steps }}

## What this draft did NOT do

- It did not roll anything back.
- It did not silence any alert.
- It did not call any service. Every datapoint is from read-only PromQL
  / LogQL / Kubernetes API queries listed below.

## Tool call audit

{{ tool_call_audit }}
