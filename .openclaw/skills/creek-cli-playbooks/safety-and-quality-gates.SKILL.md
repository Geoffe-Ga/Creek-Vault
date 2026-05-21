---
name: safety-and-quality-gates
description: >-
  Apply operational safety checks for provenance, contradiction handling, and
  privacy-tier protection during Creek CLI workflows. Use when an OpenClaw
  agent is about to finalize outputs or save content. Do NOT use as a substitute
  for the command execution playbooks.
metadata:
  owner: Creek Vault
  version: 1.0.0
---

# Safety and Quality Gates

## Gate 1: Provenance integrity

- Do not present synthesized claims as facts without source alignment.
- Preserve IDs/traceability where the workflow provides it.

## Gate 2: Contradictions are data

- If claims conflict, route to paradox/liminal handling.
- Never silently choose a winner unless explicitly asked for a hypothesis.

## Gate 3: Privacy-tier discipline

- Treat intimate content as opt-in for reuse.
- Avoid accidental persistence or exposure in generated artifacts.

## Gate 4: CLI correctness

- Validate command/flags against `creek-tools/creek/cli.py`.
- Avoid shell one-offs that bypass documented behavior unless debugging is required.

## Gate 5: Final response quality

- Include what was done, what changed, and what remains uncertain.
- Provide reproducible commands for follow-up.
