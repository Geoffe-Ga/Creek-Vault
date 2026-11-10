---
description: Draft your mission and values for Reconcile (`creek mission` via MCP).
argument-hint: "[--limit N] [--tier open|personal]"
---

# /creek mission

Draft a personal mission statement and values, in your own voice, by calling the `creek.mission` MCP tool. The result is shaped for Reconcile's Personal page: a title of at most 120 characters, then a Markdown body with a `## Mission` paragraph and one `### <value>` section per value.

## What I will do

1. Call `creek.mission` with the supplied `--limit` (default 12 fragments) and `--tier` as `privacy_tier_ceiling` (default `open`).
2. Show the returned `title` and `markdown` exactly as written, in a fenced block, so you can copy both into Reconcile.
3. Say where the note was saved (`mission_path`, under `07-Voice/Mission/`) and how many of your fragments it drew on (`source_count`).
4. If the answer is `empty` or `refused`, give its `reason` as it stands and stop; never write a mission myself instead.

## Before you use it

Read it through. It is a draft in your voice built from your own notes, and only you can say whether it is true. It names no person, group or place, on purpose: a mission statement tied to a name is exactly the kind of record Reconcile works to protect.
