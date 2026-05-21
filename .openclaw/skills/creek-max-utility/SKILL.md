---
name: creek-max-utility
description: >-
  Operate the Creek Vault repository with maximum effectiveness through the
  canonical CLI lifecycle. Use when an OpenClaw agent needs to answer,
  compile, lint, save, scaffold, or maintain a Creek vault with reliable
  provenance and privacy controls. Provides end-to-end runbooks, command
  selection logic, and quality gates. Do NOT use for general Python coding
  style advice unrelated to Creek operations.
metadata:
  owner: Creek Vault
  version: 1.0.0
---

# Creek Max Utility

Primary orchestration skill for OpenClaw agents working in this repository.
Follow this skill first, then load the referenced playbooks for the specific
verb being executed.

## What "maximum utility" means

1. Prefer canonical Creek commands over ad-hoc scripts whenever a command
   exists.
2. Preserve provenance and privacy-tier guarantees.
3. Use compile/query/lint/save as the default operating loop.
4. Keep repository + vault state reproducible (document flags, paths, outputs).

## Repository orientation (first 90 seconds)

Run these checks immediately:

```bash
pwd
python --version
rg --files README.md creek-tools/creek/cli.py creek-tools/creek/templates/skills
```

Then review:
- `README.md` for repo and vault topology.
- `creek-tools/creek/cli.py` for all supported commands and options.
- `creek-tools/creek/templates/skills/*.SKILL.md` for canonical operational contracts.

## Default execution contract

### Step 1 — Identify intent
Map user request to one or more verbs:
- **Compile**: reconcile fragments into compiled pages.
- **Query**: answer from compiled layer first, then fragments when needed.
- **Lint**: detect hygiene issues and contradictions.
- **Save**: write validated outputs back into the vault.
- **Scaffold/Sync**: initialize or refresh canonical structure and schema skills.
- **Generate Skills**: emit voice-skill tree (`creek skills generate`).

### Step 2 — Select playbook
Load exactly one focused playbook in `.openclaw/skills/creek-cli-playbooks/`:
- `compile-query-lint-save.SKILL.md`
- `init-sync-generate.SKILL.md`
- `safety-and-quality-gates.SKILL.md`

### Step 3 — Execute with evidence
For every command run, capture:
- exact command line
- key output summary
- artifacts/paths touched
- follow-up decision (next command or stop)

### Step 4 — Close the loop
Before finishing:
1. Run relevant checks (lint/tests/CLI dry-runs when possible).
2. Confirm privacy-tier handling when content is user-derived.
3. Summarize outcomes + unresolved risks.

## Non-negotiable guardrails

- Never bypass compiled-first query behavior unless explicitly requested.
- Never resolve paradox contradictions by force; route to liminal/paradox flows.
- Never silently down-tier intimate content.
- Never invent CLI flags; verify in `creek-tools/creek/cli.py`.

## Quick command map

```bash
# Install CLI in editable mode
pip install -e ./creek-tools

# Initialize or update vault scaffolding
creek init --vault <path>
creek skills sync --vault <path>

# Core lifecycle
creek compile --vault <path>
creek query --vault <path> --question "..."
creek lint --vault <path>
creek save --vault <path> --kind thread --title "..." --body-file <file>

# Voice-skill generation
creek skills generate --vault <path>
```

## References to load on demand

- `references/operational-checklist.md`
- `references/troubleshooting.md`
