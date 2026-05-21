---
name: compile-query-lint-save
description: >-
  Execute Creek's core four-verb lifecycle with reproducible CLI operations.
  Use when an OpenClaw agent must transform fragments, answer questions,
  inspect hygiene, and persist results in-vault. Includes command order,
  fallback logic, and evidence capture. Do NOT use for repository bootstrapping;
  use init-sync-generate.
metadata:
  owner: Creek Vault
  version: 1.0.0
---

# Compile / Query / Lint / Save Playbook

## Recommended order

1. `creek compile --vault <path>`
2. `creek query --vault <path> --question "..."`
3. `creek lint --vault <path>`
4. `creek save --vault <path> ...` (if user requests persistence)

## Compile protocol

- Use compile before high-confidence answers when fragments may be newer than compiled pages.
- Capture what was compiled and whether any pages were skipped/refused.

## Query protocol

- Default to compiled-first behavior.
- Use bypass mode only when user explicitly needs raw fragment-level investigation.
- Return confidence notes when answers depend on stale or sparse compiled data.

## Lint protocol

- Run lint after substantial updates or before save/reporting.
- Treat lint contradictions as signals for paradox handling, not auto-fixes.

## Save protocol

- Pick kind deliberately (`thread`, `eddy`, `praxis`, `paradox`, etc.).
- Include concise title/body that preserves meaning and provenance context.
- Respect privacy-tier defaults and explicit consent requirements.

## Minimal artifact log template

```text
Intent:
Command:
Exit:
Artifacts:
Decision:
```
