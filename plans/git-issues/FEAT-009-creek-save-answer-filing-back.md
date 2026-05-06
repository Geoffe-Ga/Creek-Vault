# FEAT-009: `creek save` — answer-filing-back primitive

**Severity:** High (v1.0)
**Category:** FEAT
**Estimated LOC:** ~580
**Estimated complexity:** M
**Source candidate:** [`plans/2026-05-05_comparative-analysis/candidates/ADOPT-003-answer-filing-back-loop.md`](../2026-05-05_comparative-analysis/candidates/ADOPT-003-answer-filing-back-loop.md)
**Dependencies:** FEAT-001 (save.SKILL.md), FEAT-002 (paradox + privacy-tier skills)
**Parallelizable with peers:** yes (with FEAT-006/007/008)
**Wave:** 3

## Goal

Add a `creek save` CLI primitive that takes a body, target type, and optional fragment provenance and writes a properly-classified note with full frontmatter. This is what makes the wiki *compounding* — good Q&A turns into vault content rather than vanishing into chat history. CrawDad's `/crawdad save` (FEAT-016 → FEAT-014/015) wraps this primitive via MCP.

## Files to touch

- `creek-tools/creek/save/__init__.py` (new) — public surface.
- `creek-tools/creek/save/router.py` (new) — destination decision: thread / eddy / praxis / paradox / unnamed / draft.
- `creek-tools/creek/save/writer.py` (new) — frontmatter + body composer that respects existing models (Thread, Eddy, Praxis, etc.).
- `creek-tools/creek/cli.py` — add `@app.command() save(...)`.
- `creek-tools/creek/classify/privacy_filter.py` — extend with a `pre_save_filter(body, tier)` helper that title-only-summarizes `personal` content and refuses `intimate`.
- `creek-tools/tests/test_save.py` (new) — destination-decision and tier-filter tests.

## Pre-decided choices

- **CLI signature:**
  ```
  creek save --target <thread|eddy|praxis|paradox|unnamed|draft> \
             --body <path-or-stdin> \
             --title <optional> \
             --provenance frag-XXX,frag-YYY \
             --source <conversation-id|discord-msg-id|claude-session-id> \
             --tier <open|personal|intimate>
  ```
- **`--target` is required** (no auto-classification in v1.0; smart routing is deferred to v1.1 per ADOPT-003's "simplest viable v1").
- **Tier defaulting:** `--tier` is required when stdin is the body source; defaults to the source fragments' max tier when `--provenance` is supplied.
- **Privacy enforcement:**
  - `intimate` → save title-only summary, body to `10-Liminal/Compost/intimate-stubs/` (gitignored), with a frontmatter pointer to the local-only body. Never auto-saves full intimate content.
  - `personal` → save title + summary; full body is included only when `--full-body` is explicitly passed.
  - `open` → full save.
- **Paradox routing rule:** when `--target paradox`, the save *always* goes to `10-Liminal/Paradoxes/` regardless of any other classification; paradox tier-filter is `open` (the *fact* of the contradiction, not the contradictory content, is what's preserved).
- **Provenance frontmatter:**
  ```yaml
  saved_from:
    source_kind: discord | claude-session | manual | mcp
    source_id: <opaque-id>
    contributing_fragments: [frag-XXX, frag-YYY]
    saved_at: 2026-05-06T17:35:00Z
    saved_by: <operator-or-mcp-client>
  ```

## Test plan

- Unit: each destination type produces a model-conformant note (Thread, Eddy, Praxis, etc.) at the right path.
- Unit: `pre_save_filter(body, tier=intimate)` returns title-only and a body-pointer to the gitignored stubs directory.
- Regression: `creek save --target paradox` always lands in `10-Liminal/Paradoxes/` regardless of other inputs.
- Regression: `creek save` with no `--tier` and no `--provenance` refuses with a clear error message (not a silent default).
- Regression: `intimate`-tier saves never write full body to the vault (verified by file-system inspection in the test).
- Integration: `creek save --target thread --body fixtures/answer.md --provenance frag-001,frag-002 --tier open` produces a properly-formatted Thread note.

## Acceptance criteria

- `creek save` CLI exists with the documented signature.
- All six target types route to the correct vault directory.
- Privacy-tier enforcement is verified by regression tests for each tier.
- Paradox routing always lands in `10-Liminal/Paradoxes/` regardless of other inputs.
- Provenance frontmatter is present on every saved note.
- ≥90% branch coverage on `creek/save/`.
- `docs/save.md` (new) documents the command and the tier rules.

## References

- Source candidate: ADOPT-003.
- FEAT-014/015 (CrawDad will dispatch `/crawdad save` intents through MCP to this primitive).
- Existing models being written: `Thread`, `Eddy`, `Praxis` in `creek/models.py`.
- Privacy-tier system: `creek/classify/privacy.py` and `creek/classify/privacy_filter.py`.
