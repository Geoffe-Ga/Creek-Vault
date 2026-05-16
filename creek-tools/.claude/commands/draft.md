---
description: Draft an essay from a mined seed (`creek draft` via MCP).
argument-hint: "[--phase PHASE] [--index N]"
---

# /creek draft

Generate a voice-faithful essay draft from a previously mined seed by calling the `creek.draft` MCP tool. Voice fidelity is owned by the `creek-tools` LLM pass — this command does not re-implement composition.

## What I will do

1. Call `creek.draft` with the supplied `--phase` and `--index` (defaults to the top mined seed for the current dominant phase).
2. The MCP tool returns the draft alongside the skill stack it activated (voice-core + phase + register skills).
3. Display the draft body, the activated skills, and the source fragment provenance. Suggest saving with `/creek save --target draft`.

## When to use

- After `/creek mine` has surfaced an interesting seed.
- During a phase where outward expression is appropriate (Rising, Peaking, Restoration).

## Voice-fidelity contract

The composer in the CrawDad loop (FEAT-015) never re-drafts essays inline. Drafting always goes through `creek.draft` so its skill-stack assembly stays consistent across surfaces.
