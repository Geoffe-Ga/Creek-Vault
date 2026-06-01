# Backlog Grooming — 2026-02-26

## PRs Analyzed (15 merged)

| PR | Title | Issues Closed | Merged |
|----|-------|---------------|--------|
| #92 | feat(classify): rule-based pre-classification | #23 | 2026-02-26 |
| #91 | feat(redact): full redaction pattern library | #13 | 2026-02-26 |
| #90 | feat(scripts): pr-status.sh workflow monitor | #89 | 2026-02-24 |
| #88 | Add Claude Code GitHub Workflow | — | 2026-02-24 |
| #75 | feat(ingest): Claude conversation ingestor | #17 | 2026-02-25 |
| #74 | feat(ingest): ChatGPT conversation ingestor | #18 | 2026-02-25 |
| #73 | feat(ingest): Markdown file ingestor | #20 | 2026-02-15 |
| #72 | feat(ingest): Discord message ingestor | #19 | 2026-02-15 |
| #71 | feat(pipeline): Pipeline orchestrator + E2E | #12 | 2026-02-15 |
| #70 | feat(redact): redaction scanner/redactor stubs | #8 | 2026-02-15 |
| #69 | feat(generate): index generator | #11 | 2026-02-15 |
| #68 | feat(vault): vault writer | #7 | 2026-02-15 |
| #67 | feat(ingest): abstract Ingestor base class | #6 | 2026-02-15 |
| #66 | feat(link): linking pipeline stubs | #10 | 2026-02-15 |
| #65 | feat(classify): classification pipeline stubs | #9 | 2026-02-15 |

## Issue Resolution Verification

All 14 issues referenced in merged PRs are confirmed **CLOSED**:
#6, #7, #8, #9, #10, #11, #12, #13, #17, #18, #19, #20, #23, #89

No issues found incorrectly open or incorrectly closed.

## Follow-Up Issues Created (from Claude Reviews)

| Issue | Source | Description |
|-------|--------|-------------|
| #93 | PR #91 review | Add runtime severity validation to PatternInfo `__post_init__` |
| #94 | PR #91 review | Expand Discover card regex to cover 65xx and 644-649 prefixes |
| #95 | PR #92 review | Deduplicate scoring logic in RuleClassifier |

## Open Issues (33 total)

### Tracer Phase 2 (24 issues)

**Redaction (3):** #14 scanner enhancements, #15 applier enhancements, #16 CLI commands
**Classification (3):** #24 LLM via Ollama, #25 LLM via Anthropic, #26 confidence scoring + review queue
**Linking (5):** #28 embeddings, #29 semantic similarity, #30 temporal proximity, #31 thread detection, #32 eddy formation
**Indexes (4):** #33 wiki-links, #34 frequency indexes, #35 thread/eddy/source indexes, #36 temporal index
**Intelligence (5):** #37 unnamed digest, #38 paradox preservation, #39 synchronicity, #40 compost tracking, #41 emergent tags
**Wavelength/Decision (4):** #42 wavelength tracking, #43 wavelength reports, #44 decision detection, #45 decision context
**Voice (4):** #47 voice exemplars, #48 voice patterns, #49 voice profiles, #50 lexicon
**Writing (3):** #51 skill tree, #52 blog mining, #53 draft workflow
**Privacy (1):** #27 privacy tiers, #46 `creek purge`

### Tracer Phase 3 (4 issues)

#54 image/OCR ingestor, #55 code repo ingestor, #56 Google Drive downloader, #57 Google Drive sub-parsers, #58 generic/fallback ingestor

### Data Quality / Cleaning (12 issues)

#76 content quality scorer, #77 post-ingestion validation, #78 normalized dedup, #79 Discord filter, #80 chatbot filter, #81 markdown filter, #82 Google Drive filter, #83 authorship tagger, #84 non-user content, #85 embedding dedup, #86 vault hygiene, #87 cleaning config

### Code Quality (3 issues — NEW)

#93 PatternInfo validation, #94 Discover card regex, #95 classifier scoring dedup

## Duplicates Found

None.

## Statistics

| Metric | Value |
|--------|-------|
| PRs analyzed | 15 |
| Issues verified closed | 14 |
| Issues already correctly closed | 14 |
| Issues needing closure | 0 |
| Follow-up issues created | 3 |
| Duplicates found | 0 |
| Open issues (before) | 30 |
| Open issues (after) | 33 (+3 follow-ups) |
| Backlog health | Clean |
