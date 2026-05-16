---
description: Explain the `/creek` slash command surface — help discoverable anywhere.
argument-hint: "[SUBCOMMAND]"
---

# /creek explain

Help command. Lists every `/creek` subcommand with a one-line description, or — when called as `/creek explain <subcommand>` — shows the detailed body of just that command.

## What I will do

1. Without an argument: list all 10 subcommands (`state`, `lint`, `mine`, `draft`, `save`, `phase`, `wavelength`, `skills`, `ingest`) with descriptions pulled from each file's frontmatter.
2. With an argument: render the matching command file's body.
3. Unknown subcommand: list the valid ones (no stack trace, no MCP call).

## Discoverability

Per FEAT-016's design, help is reachable from every prefix: `/creek explain`, `/creek <cmd> --help`, and `/creek help` all surface this content.
