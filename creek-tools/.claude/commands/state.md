---
description: Render the current vault audit report (`creek state` via MCP).
argument-hint: "[--render]"
---

# /creek state

Render the vault's audit report by calling the `creek.state.read` MCP tool. Pass `--render` to force a fresh regeneration via the `creek.state.render` tool (slower but reflects the latest fragments).

## What I will do

1. Default: call `creek.state.read` — cheap; returns the cached `latest.md`.
2. With `--render`: call `creek.state.render` — re-walks the vault and rewrites `latest.md`.
3. Display the wavelength snapshot first (per FEAT-007 ordering), then active eddies, threads, surprising connections, and suggested questions.

## When to use

- Daily orientation: "what's surfacing this week?".
- Before drafting (`/creek mine`, `/creek draft`) — the suggested questions block primes mining.
- After an `/creek ingest` run — pass `--render` to refresh.
