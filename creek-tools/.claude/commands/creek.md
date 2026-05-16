---
description: Default `/creek` entry — render the current vault state audit report.
argument-hint: "(no arguments — see /creek state for explicit form)"
---

# /creek

The root `/creek` command. With no arguments, it renders the latest vault state report by calling the `creek.state.read` MCP tool. This is the same audit report `creek state` produces from the CLI.

## What I will do

1. Call the `creek.state.read` MCP tool to fetch `<vault>/00-Creek-Meta/State/latest.md`.
2. Display the wavelength snapshot, active eddies/threads, suggested questions, and any drift warnings.
3. If the state report is stale, suggest running `/creek state` to regenerate it via `creek.state.render`.

## Related commands

- `/creek state` — explicit form, same behaviour.
- `/creek phase` and `/creek wavelength` — shorthand for the phase block of the state report.
- `/creek explain` — list every `/creek` subcommand with descriptions.
