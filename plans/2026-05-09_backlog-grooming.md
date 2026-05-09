# 2026-05-09 — Backlog Grooming

Brief grooming pass following the merge of FEAT-003 (PR #210). Scope: process the just-merged work, close stale issues, and capture review follow-ups before they get lost.

## What landed since the last grooming (PR #200, May 5)

| PR  | Plan file              | Title                                                                    |
|-----|------------------------|--------------------------------------------------------------------------|
| #210| FEAT-003               | feat(compile): `creek compile` primitive with per-claim provenance      |
| #208| FEAT-002               | docs(skills): paradox / liminal / privacy-tier / wavelength-aware       |
| #205| FEAT-005               | feat(pipeline): deterministic-first three-pass vocabulary + --no-llm    |
| #204| FEAT-001               | docs(skills): schema-skill tree + root AGENTS.md                        |
| #203| INC-019                | fix(taxonomy): reconcile phase/mode/frequency drift to spec             |
| #202| (n/a — meta)           | docs(plans): file FEAT-001..FEAT-016 — v1.0 implementation roadmap      |
| #199| (n/a — meta)           | docs(plans): comparative analysis of Creek Vault + CrawDad vs landscape |

## Actions taken

### Issues closed

- **#201** — *INC-019: spec/implementation drift on phase, mode, and frequency taxonomy.* Fixed by PR #203 with one-release migration aliases mirroring INC-003. FEAT-003 (#210) shipped on top of this and cited it as a satisfied dependency. Closed with a summary referencing the migration aliases (`_PHASE_LEGACY_ALIASES`, `_MODE_LEGACY_ALIASES`, `_FREQUENCY_LEGACY_ALIASES`) and the canonical doc fixes.

### Issues created

- **#212** — *FEAT-003 follow-ups: default_llm rename, CompiledPage.type Literal, path-escape guard, test assertion.* Bundles four non-blocking nits the reviewer flagged in the PR #210 LGTM re-review:
  1. 🟡 Rename `_default_llm` → `default_llm` (private import across module boundary).
  2. 🟡 Tighten `CompiledPage.type` to `Literal["compiled_page"]`.
  3. 🟢 Path-escape guard in `_resolve_target_path` (target_id can currently traverse).
  4. 🟢 Missing assertion in `test_compile_fragments_skips_empty_claim_text` (docstring promises provenance is preserved; the test doesn't verify that half).
  Labelled `chore`, `creek-tools`, `compile`, `good-first-issue`.

### Plan files moved to `plans/git-issues/done/`

Five plan files corresponding to merged PRs were re-homed:

- `INC-019-spec-impl-drift-phase-mode-frequency-taxonomy.md` (PR #203)
- `FEAT-001-schema-skills-compile-lint-save.md`              (PR #204)
- `FEAT-002-schema-skills-paradox-liminal-privacy-wavelength.md` (PR #208)
- `FEAT-003-compile-primitive.md`                            (PR #210)
- `FEAT-005-deterministic-first-pipeline.md`                 (PR #205)

## Backlog state after this pass

### Open GitHub issues (5)

| #   | Title                                                            | Status                                              |
|-----|------------------------------------------------------------------|-----------------------------------------------------|
| 207 | FEAT-001 follow-ups: save.SKILL.md observed_phase + .gitignore   | Still open — best folded into FEAT-004 PR           |
| 206 | creek-tools: local check-all.sh diverges from CI                 | Still open — environment fix                        |
| 212 | FEAT-003 follow-ups (this pass)                                  | New                                                 |
| 166 | refactor(ingest): typed intermediates in `parse()`               | Older — still relevant                              |
| 165 | refactor(ingest): caller-controllable `_split_header`            | Older — still relevant                              |

### Plan files still pending (`plans/git-issues/`)

- **Remaining FEATs:** FEAT-004 (query routing), FEAT-006/007 (`creek state`), FEAT-008 (`creek lint`), FEAT-009 (`creek save`), FEAT-010/011/012 (MCP server tiers), FEAT-013/014/015 (CrawDad), FEAT-016 (slash-command grammar).
- **Other:** INC-006 (embeddings parquet cache), STYLE-001 (refurb/tryceratops backlog).
- **INDEX.md** still references the pre-launch issue catalog; not updated this pass since the bulk of those were closed in PR #200's grooming and the remaining surface is small.

## Statistics

| Metric                     | Count |
|----------------------------|------:|
| PRs reviewed (since #200)  | 7     |
| Issues closed              | 1     |
| Issues created             | 1     |
| Plan files re-homed        | 5     |
| Net open issues            | 5 (was 5; -1 #201, +1 #212) |

## Notes

- The four FEAT-003 follow-ups are deliberately bundled into a single issue (#212) rather than four separate issues. Same pattern as #207 for FEAT-001 nits — keeps the tracker small while preserving every reviewer-flagged item.
- `plans/git-issues/INDEX.md` was not regenerated. The catalog it indexes is mostly closed; a fresh INDEX would be a separate exercise tied to filing the next round of v1.0 issues.
- Skipped older PRs (pre-#200) because PR #200 already groomed 58 of 60 catalog items.
