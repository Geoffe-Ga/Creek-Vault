# FEAT-001: Schema-skill tree (compile / lint / save) + root AGENTS.md

**Severity:** High (v1.0)
**Category:** FEAT
**Estimated LOC:** ~400
**Estimated complexity:** S
**Source candidate:** [`plans/2026-05-05_comparative-analysis/candidates/ADAPT-005-modular-skill-files-as-schema.md`](../2026-05-05_comparative-analysis/candidates/ADAPT-005-modular-skill-files-as-schema.md) (part 1 of 2)
**Dependencies:** INC-019 (done), **FEAT-019** (vault-sovereignty topology must be settled — "vault root" means the user's local vault, not the repo root; schema-skill source lives in the repo template, gets deployed to the user vault)
**Parallelizable with peers:** yes (with FEAT-002, FEAT-005)
**Wave:** 1 (schema foundation)

## Goal

Author a small root `AGENTS.md` plus the first three schema-skill files under `00-Creek-Meta/Skills/` so that subsequent FEATs (compile, lint, save) have a stable contract to reference. The voice-skill tree at `creek-skills/` is unchanged; this is a parallel *schema*-skill tree.

## Files to touch

**Canonical (in repo, version-controlled — source of truth):**
- `creek-tools/creek/templates/AGENTS.md` (new) — canonical template; FEAT-019's `creek init` deploys this to the user vault.
- `creek-tools/creek/templates/skills/compile.SKILL.md` (new) — canonical template.
- `creek-tools/creek/templates/skills/lint.SKILL.md` (new) — canonical template.
- `creek-tools/creek/templates/skills/save.SKILL.md` (new) — canonical template.

**Per-vault (deployed by `creek init` — not version-controlled in the repo, only in the user's local vault):**
- `<vault>/AGENTS.md` — points at `<vault>/00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` as canonical and defines the compile/query/lint/save contract in ≤3000 tokens.
- `<vault>/00-Creek-Meta/Skills/compile.SKILL.md` — how to compile fragments → wiki pages.
- `<vault>/00-Creek-Meta/Skills/lint.SKILL.md` — what `creek lint` does, with the explicit "do not resolve paradoxes" guard rail.
- `<vault>/00-Creek-Meta/Skills/save.SKILL.md` — when and where to file good answers back, with privacy-tier rules.

## Pre-decided choices

- **Verb for the compile operation:** `creek compile`. (Resolves open question #1 in INTEGRATION-PLAN.md. The compiler-as-deterministic-binary metaphor is rejected — see REJECT-004 — but the verb is the right shorthand.)
- **Schema-skills location:** `00-Creek-Meta/Skills/` (parallel to, not nested under, the voice-skill tree at `creek-skills/`). Resolves open question #4.
- **AGENTS.md vs. CLAUDE.md:** `AGENTS.md` (matches the broader agent ecosystem; Claude Code reads either).
- **Token budget per skill:** ≤1500 tokens. Enforced by a follow-up lint check (added in FEAT-008).
- **AGENTS.md token budget:** ≤3000 tokens. Same lint check.

## Test plan

- A pre-commit / lint check verifies each schema-skill file is under its token budget (added later in FEAT-008's check set; for this PR, ship a documented size limit and a failing check is acceptable).
- Manual review confirms the four files load cleanly when concatenated into a Claude Code session and that they don't contradict the ontology spec.
- No code-level tests in this PR; deliverables are markdown only.

## Acceptance criteria

- `AGENTS.md` exists at vault root, ≤3000 tokens, points at `creek_ontology_agent_prompt.md` as canonical rather than duplicating it.
- `00-Creek-Meta/Skills/compile.SKILL.md` exists, ≤1500 tokens, defines compile as per-source / interactive / provenance-preserving and explicitly references the four-layer architecture (raw / compiled / schema / liminal).
- `00-Creek-Meta/Skills/lint.SKILL.md` exists, ≤1500 tokens, lists the lint checks (orphans, contradictions, stale claims, missing pages, data gaps) and includes the verbatim guard rail: "Lint never resolves paradoxes; contradictions route to `10-Liminal/Paradoxes/`."
- `00-Creek-Meta/Skills/save.SKILL.md` exists, ≤1500 tokens, documents the destination decision (thread / eddy / praxis / paradox / unnamed / draft) and the privacy-tier defaults.
- All four files use the canonical taxonomy from the spec (Rising/Peaking/Withdrawal/Diminishing/Bottoming Out/Restoration; Inhabit/Express/Collaborate/Integrate/Absorb; F1–F10).

## References

- Source candidate: ADAPT-005 (full motivation and tree structure).
- Spec section: `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` §1, §2, §6, §7 (the load-bearing taxonomy these skills must respect).
- Voice-skill tree (unchanged, for contrast): `creek-tools/creek/generate/skills.py` writes under `<vault>/creek-skills/`.
