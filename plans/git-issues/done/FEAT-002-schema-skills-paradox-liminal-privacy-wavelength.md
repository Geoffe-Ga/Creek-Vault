# FEAT-002: Schema-skill tree (paradox / liminal / privacy-tier / wavelength-aware)

**Severity:** High (v1.0)
**Category:** FEAT
**Estimated LOC:** ~400
**Estimated complexity:** S
**Source candidate:** [`plans/2026-05-05_comparative-analysis/candidates/ADAPT-005-modular-skill-files-as-schema.md`](../2026-05-05_comparative-analysis/candidates/ADAPT-005-modular-skill-files-as-schema.md) (part 2 of 2)
**Dependencies:** FEAT-001 (the root `AGENTS.md` it composes with)
**Parallelizable with peers:** yes (with FEAT-005 only; FEAT-003 and FEAT-004 hard-depend on this FEAT)
**Wave:** 1 (schema foundation)

## Goal

Author the four remaining schema-skill files under `00-Creek-Meta/Skills/` covering the project's non-negotiables and the wavelength lens. These are the guard rails every downstream FEAT must respect.

## Files to touch

- `00-Creek-Meta/Skills/paradox.SKILL.md` (new) — how to handle contradictions: route to `10-Liminal/Paradoxes/`, never resolve.
- `00-Creek-Meta/Skills/liminal.SKILL.md` (new) — when to leave content uncategorized; the four-layer architecture's fourth layer.
- `00-Creek-Meta/Skills/privacy-tier.SKILL.md` (new) — the `open` / `personal` / `intimate` ladder and the fail-closed defaults.
- `00-Creek-Meta/Skills/wavelength-aware.SKILL.md` (new) — phase as the lens through which every other tag is interpreted.

## Pre-decided choices

- **Privacy-tier names:** `open` / `personal` / `intimate` (matches the spec; INC-003 is resolving the code-side `public` → `open` rename — confirm before merging this FEAT).
- **Liminal as a fourth layer (not a wiki sub-folder):** explicitly named in `liminal.SKILL.md`. This is the highest-priority distinctiveness item per INTEGRATION-PLAN.md's distinctiveness watchlist.
- **Wavelength snapshot location for skill activation:** `00-Creek-Meta/State/latest.md` (created in FEAT-007). For this FEAT, name the location and document that it's the source the wavelength-aware skill reads.

## Test plan

- Same size-budget check as FEAT-001 (≤1500 tokens per skill).
- A manual conformance review: every `Pre-decided choices` block in subsequent FEATs (003+) must respect the rules these skills name.

## Acceptance criteria

- `paradox.SKILL.md` exists, ≤1500 tokens, contains the verbatim rule: "Contradictions are data, not defects. Route to `10-Liminal/Paradoxes/`. Do not propose resolution."
- `liminal.SKILL.md` exists, ≤1500 tokens, names the four-layer architecture (raw / compiled / schema / liminal) and explicitly says the liminal layer is *not* a wiki sub-folder.
- `privacy-tier.SKILL.md` exists, ≤1500 tokens, documents the three tiers, the fail-closed default (ambiguous → `personal`, unknown → `intimate`), and the per-tier inclusion behaviour for `mine` / `draft` / `report` / `skills`.
- `wavelength-aware.SKILL.md` exists, ≤1500 tokens, documents the six phases × five modes × Do/Feel × Medicine/Toxic and points at `00-Creek-Meta/State/latest.md` as the snapshot source.
- All four files use canonical taxonomy and respect INC-019's resolution.

## References

- Source candidate: ADAPT-005.
- Spec sections: §7.1 (six phases), §7.2 (five modes), §7.3 (Medicine/Toxic), §10 (emergence + liminal infrastructure), §13.2 (privacy tiers).
- INTEGRATION-PLAN.md distinctiveness watchlist (the four non-negotiables in priority order).
