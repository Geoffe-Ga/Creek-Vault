---
description: Save an answer or fragment back to the vault (`creek save` via MCP).
argument-hint: "[--target {fragment|paradox|draft|liminal}] [--body TEXT]"
---

# /creek save

File an answer, paradox, or draft back to the vault by calling the `creek.save` MCP tool. The save respects the privacy tier of the source content (FEAT-011 write-side tier-ceiling rule).

## What I will do

1. Call `creek.save` with the supplied `--target` (fragment / paradox / draft / liminal) and `--body`.
2. The MCP tool resolves the destination folder:
   - `fragment` → `01-Fragments/<inferred-source>/`
   - `paradox` → `10-Liminal/Paradoxes/`
   - `draft` → `07-Voice/Drafts/`
   - `liminal` → `10-Liminal/Unnamed/`
3. Confirm the path written and the privacy tier applied.

## When to use

- After a `/creek lint` pass flagged a paradox.
- After `/creek draft` produced an essay you want to keep.
- To file a reflection or one-off fragment you authored conversationally.

## Privacy tier

The save inherits the privacy tier of the source content. If you're saving content that originated at an `intimate` tier, ensure your session ceiling is `intimate` or higher (see `creek-tools/docs/mcp.md`).
