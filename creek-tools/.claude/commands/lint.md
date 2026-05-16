---
description: Run the unified vault hygiene lint pass (`creek lint` via MCP).
argument-hint: "[--checks CHECKS] [--since DATE]"
---

# /creek lint

Surface vault hygiene issues — orphaned fragments, drift in classifications, broken links, paradox candidates — by calling the `creek.lint` MCP tool.

## What I will do

1. Call `creek.lint` with the supplied `--checks` and `--since` filters (or the defaults: all checks, last 7 days).
2. Group findings by severity and section: hygiene first, paradox surfacing second, drift third.
3. For each paradox candidate, suggest `/creek save --target paradox` to route it to `10-Liminal/Paradoxes/`.

## When to use

- Weekly grooming pass.
- Before a release of generated content (drafts, reports).
- After bulk ingestion, to catch classification regressions.
