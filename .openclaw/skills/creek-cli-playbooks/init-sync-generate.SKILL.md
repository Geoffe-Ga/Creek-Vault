---
name: init-sync-generate
description: >-
  Bootstrap and maintain canonical vault scaffolding and skill trees through the
  Creek CLI. Use when an OpenClaw agent needs to initialize a vault, refresh
  schema skills, or regenerate the voice-skill tree. Do NOT use for day-to-day
  compile/query/lint/save execution; use compile-query-lint-save.
metadata:
  owner: Creek Vault
  version: 1.0.0
---

# Init / Sync / Generate Playbook

## Use cases

- New vault setup
- Post-upgrade schema skill refresh
- Regeneration of `creek-skills/` voice tree

## Commands

```bash
creek init --vault <path>
creek skills sync --vault <path>
creek skills generate --vault <path> [--output <path>]
```

## Rules

1. `init` is for first-time structure creation and safe refresh of canonical assets.
2. `skills sync` updates schema-skill files from upstream templates.
3. `skills generate` builds style/voice-oriented skill trees; keep distinct from schema-skill contract.

## Validation steps

- Verify generated/updated paths exist.
- Summarize counts or changed files where output provides them.
- If run in repo development mode, commit only intended artifacts.
