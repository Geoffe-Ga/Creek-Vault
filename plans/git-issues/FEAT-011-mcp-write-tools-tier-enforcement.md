# FEAT-011: MCP write tools + tier-ceiling enforcement

**Severity:** High (v1.0)
**Category:** FEAT
**Estimated LOC:** ~500
**Estimated complexity:** M
**Source candidate:** [`plans/2026-05-05_comparative-analysis/candidates/ADAPT-004-mcp-server-as-interface.md`](../2026-05-05_comparative-analysis/candidates/ADAPT-004-mcp-server-as-interface.md) (part 2 of 3)
**Dependencies:** FEAT-010 (server skeleton + tier-ceiling helper), FEAT-009 (`creek save` is the most-used write tool)
**Parallelizable with peers:** no (FEAT-012 extends this)
**Wave:** 4

## Goal

Add the write-side MCP tools — `creek.save`, `creek.ingest`, `creek.classify`, `creek.link`, `creek.report`, `creek.skills.refresh`, `creek.compile` — with the same tier-ceiling enforcement and audit logging as the read tools.

## Files to touch

- `creek-tools/creek_mcp/tools/save.py` (new) — wraps `creek save` (FEAT-009).
- `creek-tools/creek_mcp/tools/ingest.py` (new) — wraps `creek ingest`.
- `creek-tools/creek_mcp/tools/classify.py` (new) — wraps `creek classify`.
- `creek-tools/creek_mcp/tools/link.py` (new) — wraps `creek link`.
- `creek-tools/creek_mcp/tools/report.py` (new) — wraps `creek report`.
- `creek-tools/creek_mcp/tools/skills.py` (new) — wraps `creek skills`.
- `creek-tools/creek_mcp/tools/compile.py` (new) — wraps `creek compile` (FEAT-003).
- `creek-tools/creek_mcp/server.py` — register the new tools.
- `creek-tools/creek_mcp/tier_ceiling.py` — extend with write-side tier rules (writes that would create higher-tier content require explicit confirmation).
- `creek-tools/tests/test_mcp_write_tools.py` (new).
- `creek-tools/docs/mcp.md` — extend with write-tool documentation.

## Pre-decided choices

- **`creek.save` is the priority write tool** — CrawDad's answer-filing-back loop (FEAT-014/015) depends on it.
- **Write-side tier-ceiling rule:** a write tool that *would create* a fragment / compiled page at tier `T` requires the caller's `privacy_tier_ceiling >= T`. A caller with `privacy_tier_ceiling=open` cannot create `intimate` content via MCP.
- **Default tier for write tools:** matches the source. `creek.save` honours the source fragments' tier; `creek.ingest` uses the ingestor's default tier (typically `personal`).
- **`creek.skills.refresh` is read-only** as far as user content goes — it regenerates the voice-skill tree from existing fragments. Treated as a write tool only because it produces new files.
- **Audit log richness for writes:** in addition to the read-tool audit fields, write tools also log `created_path`, `created_tier`, and `affected_fragment_ids` (a list, not full bodies).

## Test plan

- Unit per tool wrapper.
- Integration: each write tool callable via MCP against a fixture vault produces the expected vault state change.
- Regression: `creek.save` with `privacy_tier_ceiling=open` and a body that classifies as `intimate` is refused with a structured error, not silently downgraded.
- Regression: every write-tool invocation writes an audit log entry with the write-side fields.
- Regression: re-invoking `creek.compile` (idempotent per FEAT-003) doesn't double-write audit entries on no-op runs.

## Acceptance criteria

- Seven write tools exposed and callable.
- Write-side tier-ceiling rule enforced and tested.
- Audit log entries for writes include `created_path`, `created_tier`, `affected_fragment_ids`.
- ≥90% branch coverage on the new tool modules.
- `docs/mcp.md` documents each write tool's input schema and tier rules.

## References

- Source candidate: ADAPT-004 (write-side tier enforcement is part of "privacy-by-construction at the MCP boundary").
- FEAT-009 (`creek save` is the underlying primitive for `creek.save`).
- FEAT-003 (`creek compile` is the underlying primitive for `creek.compile`).
- FEAT-010 (the server + tier-ceiling helper this builds on).
