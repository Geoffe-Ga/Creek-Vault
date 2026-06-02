# SPEC Summary — FEAT-041 Creek Writing Desk

**Source SPEC:** `plans/crawdad-writing-system/SPEC.md`
**Decomposed:** 2026-06-01
**Branch for all work:** feature branches off `main` (per CLAUDE.md; direct commits to `main` are blocked).

## One-paragraph restatement

Turn CrawDad from a single router→composer loop into a **writing desk in graph form**: a
supervisor ("Conductor") agent plans a writing task, routes sub-tasks to specialist agents that
each own one slice of the Creek knowledge base (graph/compiled layer, raw+other-authors
retrieval, ontology analytics), a Voice agent renders the draft in the vault owner's voice, and
a Reflection agent (LLM-as-judge) gates every draft on voice fidelity, ontological accuracy,
citation completeness, privacy, and paradox preservation before it ships — escalating to a human
when it can't pass. The desk lives in `creek-tools` (new `creek author` CLI + MCP `author` verb),
is built on the Anthropic SDK managed-agents pattern, and produces multiple mediums
(encyclopedic answers first, then chat/essay/research-piece/book-report/how-to). Two structural
changes make this a real wiki: a new `11-Other-Authors/` vault category (by-author, with
`ai-as-user` reserved) whose ideas are fully classified into the ontology but walled off from
voice training, and a two-axis attribution model (`voice_weight` + `representativeness`).

## Epics

| Epic | Outcome | Blocks |
|------|---------|--------|
| EPIC_01 — Vault category & attribution model | The vault can hold attributed other-author content, fully classified into the ontology, walled off from voice-proxy training | 02, 03, 04 |
| EPIC_02 — Creek Writing Desk (research medium e2e) | `creek author --medium research` produces a cited, voiced, reflection-gated answer about APTITUDE/Wavelength | 03, 04 |
| EPIC_03 — MCP `author` verb + CrawDad surfacing | CrawDad answers a research question end-to-end via the desk; AI output saved as `ai-as-user` | — |
| EPIC_04 — Additional mediums | chat/essay/research-piece/book-report/how-to reuse the desk via medium contracts | — |

## Cross-epic sequencing

```
EPIC_01 ──blocks──▶ EPIC_02 ──blocks──▶ EPIC_03  (parallel-safe with 04)
                                     └──▶ EPIC_04  (parallel-safe with 03)
```

EPIC_01 is the data foundation and must land first. EPIC_02 is the desk and depends on the
attribution model + retrieval surface. EPIC_03 (surfacing) and EPIC_04 (more mediums) are
parallel-safe once the desk exists.

## Open questions carried from the SPEC (§13)

These have **documented defaults in the SPEC** and do not block decomposition; each is pinned to
the issue where the decision is made. Confirm before/at that issue:

1. **Per-medium cost budget?** → EPIC_02 polish issue (default: rely on model tiering + caching).
2. **Should `ai-as-user` `voice_weight` ever exceed 0?** → EPIC_01 voice-exclusion issue (default: 0.0, audited opt-in only).
3. **Canonical author-slug authority / merge policy?** → EPIC_01 scaffold issue (default: slug = folder name, manual de-dup).
4. **Default backlink-walk breadth/depth bounds?** → EPIC_02 specialist-agents issue (default: breadth 25 / depth 2, relevance-pruned).

## Filing note

This environment uses **GitHub MCP tools** (`mcp__github__issue_write`), not the `gh` CLI.
Filing steps are adapted accordingly: epics filed first to capture real numbers, then
`EPIC_NN_NUMBER` placeholders substituted into child bodies before filing children, then each
epic body edited to add the child checklist. Scope is restricted to `geoffe-ga/creek-vault`.
