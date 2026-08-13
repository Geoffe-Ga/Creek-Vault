---
description: Save an answer or fragment back to the vault (`creek save` via MCP).
argument-hint: "[--target {fragment|paradox|draft|liminal}] [--body TEXT]"
---

# /creek save

File an answer, paradox, or draft back to the vault by calling the `creek.save` MCP tool. `creek.save` requires an explicit `tier` on every call and refuses (`status: refused`) if it is omitted (FEAT-011 write-side tier-ceiling rule).

## What I will do

1. Determine the most-restrictive privacy tier among the source content's contributing fragments, then call `creek.save` with the supplied `--target` (fragment / paradox / draft / liminal), `--body`, and that explicit `tier`.
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

`creek.save` requires an explicit `tier` and refuses a call that omits one — it never infers or inherits a tier from the source content. Determine the most-restrictive tier among the source content's contributing fragments, pass it explicitly as `tier`, and ensure your session's `privacy_tier_ceiling` admits it: if you're saving content that originated at an `intimate` tier, your session ceiling must be `intimate` or higher (see `creek-tools/docs/mcp.md`).
