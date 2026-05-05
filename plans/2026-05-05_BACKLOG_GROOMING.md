# Backlog Grooming — 2026-05-05

Branch: `claude/backlog-grooming-GoCtb`
Scope: 15 most-recent merged PRs (numbers #164 — #195) plus current open
GitHub issues, reconciled against the file-based pre-launch issue
catalog under `plans/git-issues/`.

## Repository conventions

This repo tracks two parallel "issue" surfaces:

1. **GitHub Issues** — currently 2 open (`#165`, `#166`); 78 closed.
   Used historically for the launch-feature backlog (`#1`–`#107`).
2. **`plans/git-issues/*.md` (file-tracked)** — 60 pre-launch findings
   filed by PR #168 (the comprehensive issue catalog) using the
   `BUG-/SEC-/INC-/PERF-/OPS-/ARCH-/CI-/DEP-/TEST-/STYLE-` schema.
   The recent merged PRs reference these IDs in their bodies, not the
   GitHub issue numbers.

Grooming therefore operates on both surfaces.

## 1. Merged PRs analysed

| PR | Merged | Title | Issues closed |
|---:|:-------|:------|:--------------|
| #195 | 2026-05-05 | Batch H — operational polish | BUG-002/009/010, OPS-001/003/004, ARCH-001/002, INC-003/008/012/013/017/018 |
| #193 | 2026-05-04 | Batch C — audit & privacy substrate | SEC-005/006, INC-004/005/007/015, PERF-002 |
| #194 | 2026-05-03 | Batch E — vault-writer + dedup + voice scaling | PERF-001/003/004, BUG-006 |
| #191 | 2026-05-03 | feat(redact): Luhn + Batch D bundle | SEC-001 *(also silently:* SEC-002, INC-009, INC-014, INC-016*)* |
| #192 | 2026-05-03 | Batch G — security hygiene | SEC-003/004/007/008, OPS-002 |
| #190 | 2026-05-02 | Batch B — CLI surface + consent gate | BUG-003/004, INC-001/002/010/011 *(also silently:* BUG-001, BUG-005, BUG-008*)* |
| #170 | 2026-05-01 | Batch F — CI strictness + deps + tests | CI-001..004, DEP-001..003, TEST-001..005, STYLE-002 |
| #189 | 2026-05-01 | skill: bump address-feedback v1.1.0 | n/a |
| #188 | 2026-05-01 | skill: add address-feedback | n/a |
| #187 | 2026-04-30 | chore(deps): consolidate Dependabot updates | n/a |
| #171 | 2026-04-30 | chore(ci): add Dependabot config | n/a |
| #169 | 2026-04-30 | fix(models): deterministic Fragment.id; MARKDOWN platform | BUG-007, BUG-011 |
| #168 | 2026-04-29 | docs(review): pre-launch issue catalog | (created the catalog) |
| #167 | 2026-04-28 | docs: rewrite READMEs and task-oriented guides | n/a |
| #164 | 2026-04-28 | feat(ingest): XLSX/CSV and PPTX ingestors | GH #57 |

### Silent closes verified by code inspection

Several PRs landed work that closed catalog items without naming them
in the PR body or commit subject. Verified directly against current
`main`:

- **#190 (Batch B)** — closed Batch A items along with its declared
  scope:
  - `BUG-001` (`creek/pipeline.py:272-325`: `_run_ingestion` now uses
    `assemble_ingested_fragment`, propagating `ParsedFragment` metadata).
  - `BUG-005` (`creek/pipeline.py:308-309`: ingestor errors are
    forwarded onto `result.errors` with the registry key).
  - `BUG-008` (`creek/vault/writer.py:443-465`: `write_fragment` now
    accepts and persists a `body`; the docstring explicitly cites the
    "historical bug").
- **#191 (SEC-001 Luhn)** — bundle also contained Batch D:
  - `SEC-002` (`creek/redact/patterns.py` adds `discord_bot_token`,
    `github_pat`, `ipv4`, `ipv6`).
  - `INC-009` (`creek/config.py:213` `replacement_template` configurable
    with validator).
  - `INC-014` (covered by SEC-002 IPv4/IPv6 patterns).
  - `INC-016` (`creek/config.py:70,204` OCR `min_confidence` is config).

## 2. Catalog resolution status

Of the 60 catalog issues filed in PR #168, **58 are resolved** by the
PRs above. **2 remain open**:

- **`INC-006`** — embeddings cache is `.npz`, not `parquet`; per-fragment
  freshness/content-hash invalidation not implemented.
- **`STYLE-001`** — 134 `refurb` + 9 `tryceratops` violations across
  `creek/`; tools live in `.pre-commit-config.yaml` but not in CI's
  `lint-extended.sh`. CLAUDE.md acknowledges this as a known backlog.

A full status table is appended to `plans/git-issues/INDEX.md`. The
58 resolved issue files are moved to `plans/git-issues/done/` so the
top of the directory shows only outstanding work.

## 3. GitHub issues reviewed

Only two open GitHub issues exist; both are PR #164 follow-ups that
the recent grooming window did not touch:

| # | Title | Status |
|---:|:------|:-------|
| #165 | refactor(ingest): make `_split_header` header detection caller-controllable | Still open — no recent PR addresses it; remains valid follow-up. |
| #166 | refactor(ingest): replace dict round-trip with typed intermediates in `parse()` → `convert_to_markdown()` | Still open — no recent PR addresses it; remains valid follow-up. |

No GitHub issues need closing in this grooming session. No catalog
closes mapped to GitHub issue numbers.

## 4. Gaps identified (no new GitHub issues created)

- The two outstanding catalog items (`INC-006`, `STYLE-001`) are
  already filed under `plans/git-issues/`. Promoting them to GitHub
  issues would duplicate; the project's convention is to track
  pre-launch findings in the file catalog. Left in place at the top of
  the directory after the move.
- PR #195 introduced an explicit decision to keep `creek/purge/audit.py`
  on UTC for forensic portability while the rest of the pipeline moved
  to LA-zone helpers. Documented in the module docstring; a follow-up
  ADR would be polish, not a defect — not filed.
- No "work-done-without-an-issue" gaps surfaced — every PR in the
  window mapped cleanly to a catalog item, a Dependabot/skill chore, or
  a docs PR.

## 5. Actions taken

- Moved 58 resolved catalog files from `plans/git-issues/` →
  `plans/git-issues/done/` (one file per closed catalog item; original
  filenames preserved).
- Appended a "Resolution status" table to `plans/git-issues/INDEX.md`
  that maps each catalog ID to its closing PR and post-grooming
  location.
- This progress file (`plans/2026-05-05_BACKLOG_GROOMING.md`).

## 6. Statistics

| Metric | Before | After |
|--------|-------:|------:|
| Open catalog files at top of `plans/git-issues/` | 60 + INDEX | 2 + INDEX |
| Resolved catalog files | 0 (in `done/`) | 58 |
| Open GitHub issues | 2 | 2 |
| Closed GitHub issues | 78 | 78 |
| PRs analysed | — | 15 |
| Issues closed (catalog) | — | 58 |
| Issues created | — | 0 |

Backlog health: from "60 outstanding pre-launch findings" to "2
remaining medium/low items" in the grooming window. Both remaining
items are non-blocking for launch.
