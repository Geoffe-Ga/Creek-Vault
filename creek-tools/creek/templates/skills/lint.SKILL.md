# lint.SKILL.md

**Verb:** `creek lint`
**Layer:** schema → reads compiled + raw + liminal layers, writes a report
**Budget:** ≤1500 tokens
**Loaded by:** Claude Code, CrawDad, any agent surfacing "what's growing / stale / contradicting / orphaned"

## What `creek lint` does

`creek lint` is the unified hygiene operation across the vault. It runs five checks, produces one consolidated markdown report, and proposes fixes that the human approves. It replaces the five separate `creek report --type {tags|unnamed|synchronicity|compost|paradox}` entry points with one verb.

## The load-bearing guard rail

> **Lint never resolves paradoxes; contradictions route to `10-Liminal/Paradoxes/`.**

This sentence is the difference between a generic-wiki lint and a Creek lint. Karpathy's lint treats contradictions as defects to fix. Creek treats them as data about a polygnostic human. The guard rail is non-negotiable and is repeated verbatim in the lint module's docstring (per FEAT-008 enforcement).

## The five checks

| Check | What it surfaces | Action |
|---|---|---|
| **Orphans** | Compiled pages with zero inbound links; orphan tags from the Tag Garden | Flag for review. Orphan *fragments* are normal — only orphan compiled pages get flagged. |
| **Contradictions** | Two or more fragments with directly opposing stances on a settled claim | Route both fragments to `10-Liminal/Paradoxes/` with `#paradox` and the relevant Frequencies. Never pick a winner. |
| **Stale claims** | Compiled-page claims a newer fragment supersedes | Flag for human review. Claims are versioned, not deleted. |
| **Missing pages** | Important concepts mentioned ≥N times across fragments but lacking their own Thread/Eddy/Praxis | Route to `10-Liminal/Unnamed/` for the next Unnamed Digest. **Never auto-create compiled pages.** |
| **Data gaps** | Topics where the vault has questions but no fragment answers them | Surface as questions in the report. Do not auto-fetch from web; CrawDad's conversational mode picks these up later. |

## Deterministic vs. semantic checks

Two cost classes; lint runs them on different cadences:

- **Deterministic** (always run, fast): broken wiki-links, frontmatter validation, tag co-occurrence, orphan detection, schema-skill token-budget enforcement (this file ≤1500 tokens; `AGENTS.md` ≤3000 tokens).
- **Semantic** (slower, behind a flag or longer cadence): contradiction detection, synchronicity surfacing, unnamed clustering — these consume embeddings and/or LLM calls.

Default behavior:

```bash
creek lint --vault <path>                  # all deterministic checks; semantic on default cadence
creek lint --vault <path> --check tags     # single check
creek lint --vault <path> --report-only    # don't propose fixes
creek lint --vault <path> --since 7d       # incremental (deterministic checks default to incremental)
```

## What lint must never do

These are non-negotiable behaviors documented in the lint module's docstring (FEAT-008 enforces):

1. **Never auto-create compiled pages.** Force-classification violates liminal preservation. Missing-page candidates go to `10-Liminal/Unnamed/`, where they cluster naturally. The human (or `creek compile`) decides when an Unnamed pattern earns a Thread or Eddy.
2. **Never resolve contradictions.** Lint never resolves paradoxes; contradictions route to `10-Liminal/Paradoxes/`.
3. **Never delete orphan fragments.** Isolated insights belong somewhere even when not yet linked. Only orphan *compiled* pages are candidates for deletion, and only after human review.
4. **Never auto-fetch external data.** "Data gaps" are presented as questions in the report, not as web-search prompts.
5. **Never bypass privacy tiers.** Lint reads frontmatter freely, but body content from intimate-tier fragments stays out of the report. Personal-tier bodies appear only as title + summary unless the human has opted in.

## Report shape

One consolidated markdown document, one section per check, with a header summary:

```markdown
# Creek Lint Report — YYYY-MM-DD

## Summary
- Checks run: orphans, contradictions, stale claims, missing pages, data gaps
- Mode: deterministic + semantic
- Window: --since 7d

## Orphans
...

## Contradictions
- Routed to 10-Liminal/Paradoxes/: 3
...

## Stale claims
...

## Missing pages
- Routed to 10-Liminal/Unnamed/: 7
...

## Data gaps
- Open questions (no auto-fetch): 4
...
```

Output lives at `00-Creek-Meta/Processing-Log/lint/YYYY-MM-DD.md`. The processing log is append-only; previous reports are not overwritten.

## Canonical taxonomy

Lint reports use these names verbatim (INC-019 reconciliation):

- **Phases (six):** Rising, Peaking, Withdrawal, Diminishing, Bottoming Out, Restoration.
- **Modes (five):** Inhabit, Express, Collaborate, Integrate, Absorb.
- **Orientations:** Do, Feel, Do/Feel.
- **Dosage:** Medicine, Toxic.
- **Frequencies (ten):** F1 Agency, F2 Receptivity, F3 Self-Love / Power, F4 Community Love / Conformity, F5 Achievism, F6 Pluralism, F7 Integration, F8 True Self / Transcendence, F9 Unity, F10 Emptiness.

When lint surfaces a paradox or a missing page, frontmatter on the routed Liminal note carries the relevant Frequency tags so future compiles can pick them up.

## What lint does not do

- Does not compile. `creek compile` produces compiled pages; lint inspects them.
- Does not save. `creek save` files good answers back; lint inspects what's been saved.
- Does not classify. `creek classify` assigns frequency/phase/mode; lint validates the assignments.
- Does not purge. `creek purge` deletes; lint flags candidates and stops.
