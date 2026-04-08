# Skill conventions

Every skill in `skills/<name>/` follows the same shape so the agent
can rely on it being there.

## Required files

| File | Purpose |
| --- | --- |
| `SKILL.md` | Anthropic Agent Skill: frontmatter, playbook, boundaries. |
| `EVAL.md` | Pass/fail conditions, transcript greps, edge cases. |
| `templates/<report>.md` | Markdown template the agent renders into. |

## Frontmatter

```yaml
---
name: <kebab-case-name>           # matches skill directory
description: <when-to-use>        # one sentence; LLM-loadable trigger
---
```

The `description` is the only field that matters at trigger-time —
keep it specific to the user-question shapes that should fire the
skill, not to the implementation.

## Playbook structure

1. `## Required tools` — explicit list of MCP tools the skill calls.
   Used by the EVAL.md transcript grep.
2. `## Playbook` — numbered steps. Each step says **what** to call and
   **how to interpret** the response. Skills don't write code; they
   compose tools.
3. `## Boundaries` — what the skill must not do (write, exec, propose
   irreversible action without explicit evidence).
4. `## Output` — rendering instructions, including the template path.

## Eval conventions

`EVAL.md` is structured as:

- **Pass conditions** — what the report must contain.
- **Fail conditions** — what marks the run as a regression.
- **Transcript greps** — concrete `grep -E` patterns over the agent's
  tool-use trace. These are the unit tests of skills.
- **Edge cases** — table of (scenario, expected behaviour) the skill
  should handle correctly.

The EVAL files turn "does the skill work?" into something an LLM
review pass can mechanically verify.

## Adding a new skill

1. Pick the kebab-case name. It will appear in `devops-mcp list-skills`
   and in the README.
2. Create `skills/<name>/SKILL.md`, `skills/<name>/EVAL.md`, and at
   least one `skills/<name>/templates/<report>.md`.
3. Update the README's skill table.
4. (Optional) add a `tests/test_skills/test_<name>.py` that lints the
   frontmatter and verifies every tool the skill names is exposed by
   one of the bundle's MCP servers.
