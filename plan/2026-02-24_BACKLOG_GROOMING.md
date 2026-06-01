# Backlog Grooming — 2026-02-24

## Project Status Summary

**Tracer Phase 1 (End-to-End Skeleton): COMPLETE**
All 12 issues (#1–#12) are closed. PRs #59–#71 merged. The full pipeline skeleton is wired: CLI, models, config, ingestors (abstract), redaction, classification, linking, index generation, vault writer, and pipeline orchestrator.

**Tracer Phase 2 (Replace Stubs): IN PROGRESS**
- 2 of ~30 Phase 2 issues completed (#19 Discord ingestor, #20 Markdown ingestor)
- 2 issues actively in progress (#17 Claude ingestor, #18 ChatGPT ingestor) with open PRs
- 26 Phase 2 issues remain open and unstarted

**Tracer Phase 3 (Extended Features): NOT STARTED**
- 5 issues (#54–#58) open, all unstarted

---

## Merged PRs (15 total, all accounted for)

| PR | Title | Merged | Closes |
|----|-------|--------|--------|
| #59 | refactor: rename repo to Creek-Vault | 2026-02-13 | #1 |
| #60 | Add Claude Code GitHub Workflow | 2026-02-13 | — |
| #61 | feat(vault): scaffold Obsidian vault | 2026-02-14 | #2 |
| #62 | feat(cli): scaffold creek CLI | 2026-02-14 | #3 |
| #63 | feat(models): Pydantic v2 models | 2026-02-14 | #4 |
| #64 | feat(config): YAML configuration loader | 2026-02-14 | #5 |
| #65 | feat(classify): classification pipeline stubs | 2026-02-15 | #9 |
| #66 | feat(link): linking pipeline stubs | 2026-02-15 | #10 |
| #67 | feat(ingest): abstract Ingestor base class | 2026-02-15 | #6 |
| #68 | feat(vault): vault writer | 2026-02-15 | #7 |
| #69 | feat(generate): index generator | 2026-02-15 | #11 |
| #70 | feat(redact): redaction scanner and redactor | 2026-02-15 | #8 |
| #71 | feat(pipeline): Pipeline orchestrator + E2E | 2026-02-15 | #12 |
| #72 | feat(ingest): Discord message ingestor | 2026-02-15 | #19 |
| #73 | feat(ingest): Markdown file ingestor | 2026-02-15 | #20 |

## Open PRs (2)

| PR | Title | Branch | Status |
|----|-------|--------|--------|
| #74 | feat(ingest): ChatGPT conversation ingestor | feat/18 | **No CI checks running** |
| #75 | feat(ingest): Claude conversation ingestor | feat/17 | **No CI checks running** |

**Action taken:** Commented on both PRs about missing CI checks.

---

## Open Issues (44 total)

### In Progress (2) — labeled `in-progress`
- **#17** Claude conversation ingestor — PR #75 open
- **#18** ChatGPT conversation ingestor — PR #74 open

### Backlog — Epic 2: Redaction & Safety (4)
- #13 Implement full redaction pattern library
- #14 Redaction scanner — scan files, flag matches, generate review queue
- #15 Redaction applier — replace sensitive data with [REDACTED:type] markers
- #16 `creek redact` CLI commands — scan, apply, review

### Backlog — Epic 3: Core Ingestors (1)
- #21 Document ingestor — DOCX, PDF, HTML, TXT to markdown fragments

### Backlog — Epic 4: Fragmentation & Classification (6)
- #22 Fragmentation engine — split long documents into atomic fragments
- #23 Rule-based pre-classification — frequency, phase, and mode signals
- #24 LLM-assisted classification via Ollama (local-first)
- #25 LLM-assisted classification via Anthropic API (opt-in cloud)
- #26 Classification confidence scoring and review queue generation
- #27 Privacy tier enforcement — Open, Personal, Intimate content handling

### Backlog — Epic 5: Linking (6)
- #28 Embedding generation for fragments
- #29 Semantic similarity linking — cosine similarity for Resonance detection
- #30 Temporal proximity linking
- #31 Thread detection — sliding time window + topic consistency
- #32 Eddy formation — dense fragment clusters
- #33 Wiki-link generator — add [[links]] to fragment frontmatter

### Backlog — Epic 6: Indexing (3)
- #34 Frequency index notes — one per frequency with Dataview queries
- #35 Thread index, Eddy map, and Source index generation
- #36 Temporal index — year, month, week navigation views

### Backlog — Epic 7: Emergence Infrastructure (5)
- #37 Unnamed Digest — weekly report on unclassified fragments
- #38 Paradox Preservation — detect contradictions
- #39 Synchronicity Detection — flag surprising cross-source resonances
- #40 Compost Tracking — abandoned threads/projects
- #41 Emergent Tag Garden — tag taxonomy with growth tracking

### Backlog — Epic 8: Intelligence Layers (5)
- #42 Wavelength tracking — temporal phase detection
- #43 Wavelength reports — weekly/monthly in Phase-Maps
- #44 Decision detection — flag decision-relevant fragments
- #45 Decision context gathering
- #46 `creek purge` command — right to be forgotten

### Backlog — Epic 9: Voice Proxy (4)
- #47 Voice exemplar collection
- #48 Voice pattern extraction
- #49 Voice register profiles
- #50 Lexicon generation

### Backlog — Epic 10: Generative Writing (3)
- #51 Voice Skill Tree generation
- #52 Blog idea mining pipeline
- #53 Draft generation workflow

### Backlog — Epic 11: Extended Ingestors & Polish (5)
- #54 Image/OCR ingestor
- #55 Code repository ingestor
- #56 Google Drive downloader
- #57 Google Drive sub-parsers (XLSX, PPTX)
- #58 Generic/fallback ingestor + consent architecture

---

## Worktree Status

### Stale worktrees (merged, safe to remove) — 13 total
These correspond to issues already closed via merged PRs:

| Worktree | Branch | Issue | Status |
|----------|--------|-------|--------|
| feat-2 | feat/2-vault-scaffold | #2 | Merged (PR #61) |
| feat-3 | feat/3-cli-skeleton | #3 | Merged (PR #62) |
| feat-4 | feat/4-pydantic-models | #4 | Merged (PR #63) |
| feat-5 | feat/5-config-loader | #5 | Merged (PR #64) |
| feat-6 | feat/6 | #6 | Merged (PR #67) |
| feat-7 | feat/7 | #7 | Merged (PR #68) |
| feat-8 | feat/8 | #8 | Merged (PR #70) |
| feat-9 | feat/9 | #9 | Merged (PR #65) |
| feat-10 | feat/10 | #10 | Merged (PR #66) |
| feat-11 | feat/11 | #11 | Merged (PR #69) |
| feat-12 | feat/12 | #12 | Merged (PR #71) |
| feat-19 | feat/19 | #19 | Merged (PR #72) |
| feat-20 | feat/20 | #20 | Merged (PR #73) |

### Active worktrees (in-progress work) — 2
| Worktree | Branch | Issue | PR | Commits ahead |
|----------|--------|-------|----|---------------|
| feat-17 | feat/17 | #17 | #75 (open) | 13 |
| feat-18 | feat/18 | #18 | #74 (open) | 15 |

### Worktrees with no new work (branched but unused) — 2
| Worktree | Branch | Issue | Notes |
|----------|--------|-------|-------|
| feat-13 | feat/13 | #13 | Only contains commits from other merged PRs, no #13 work |
| feat-23 | feat/23 | #23 | Only contains commits from other merged PRs, no #23 work |

---

## Recommendations

1. **Investigate CI** — PRs #74 and #75 have no CI checks. The workflow may need branch filter updates.
2. **Merge open PRs** — #74 and #75 appear implementation-complete; get CI green and merge.
3. **Clean up 13 stale worktrees** — Run `git worktree remove` for feat-2 through feat-12, feat-19, feat-20.
4. **Remove unused worktrees** — feat-13 and feat-23 have no feature work; remove or rebase when ready to start.
5. **Clean up local repo** — There's a stray file `creek-tools/=2.6.0` (likely from a bad pip install command).
6. **Next priorities (Phase 2)** — After merging ingestors, the natural next epics are:
   - Epic 2 (Redaction) — #13–#16, building on the existing stubs
   - Epic 4 (Classification) — #22–#27, building on rule classifier stubs
   - Epic 3 (remaining ingestors) — #21 Document ingestor

## Statistics

- **PRs analyzed:** 15 merged, 2 open
- **Issues closed (already):** 14 (all properly closed by PRs)
- **Issues updated:** 2 (#17, #18 — added `in-progress` label + PR comments)
- **Issues created:** 0 (backlog is comprehensive)
- **Labels created:** 1 (`in-progress`)
- **Stale worktrees identified:** 15 (13 merged + 2 unused)
