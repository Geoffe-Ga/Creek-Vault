---
description: Manage the voice skill tree (`creek skills` via MCP).
argument-hint: "[--refresh]"
---

# /creek skills

Inspect or regenerate the voice-skill tree by calling the `creek.skills` MCP tool.

## What I will do

1. Without args: list the active skill files under `<vault>/creek-skills/` — voice-core, phase skills, register skills.
2. With `--refresh`: invoke `creek.skills.refresh` on the MCP server to regenerate the voice-skill tree from current vault fragments.
3. Display the resolved skill stack the CrawDad composer (FEAT-015) will load at next session start.

## When to use

- After significant new voice exemplars have been ingested.
- When the composer's reply voice feels off.
- After running `/creek lint --checks voice-drift`.

## Related

The skill stack loaded per CrawDad session is determined by the vault's current wavelength phase plus the developer's default register (`confessional`). FEAT-016's `/crawdad reflect` enters the loop with this stack pre-loaded.
