# query.SKILL.md

**Verb:** `creek query` (plus every read-side consumer: `creek mine`,
`creek draft`, CrawDad reflection, ad hoc agent reads)
**Layer:** schema → governs how the compiled / raw / liminal layers
are *read*
**Budget:** ≤1500 tokens
**Loaded by:** Claude Code, CrawDad, any agent answering "what does
the vault know about X?"

## What `creek query` does

Query is the read-side counterpart to `creek compile`. It answers
questions about the vault's accumulated knowledge by routing through
the compiled layer first. Compile is what makes the vault compounding;
query is what spends that compounding instead of paying the
synthesis cost on every read.

There is no single CLI verb named `creek query` today — the contract
is enforced by every command and agent that reads the vault, with
`creek mine` and `creek draft` as the primary surfaces (FEAT-004).

## The compiled-layer-first rule

Three rules, in strict order of precedence:

### 1. Read the compiled layer first

When asked "what does the vault know about X?":

1. Check `02-Threads/`, `03-Eddies/`, and `06-Frequencies/` for a
   compiled page covering the topic.
2. If a compiled page exists, **use it** as the primary source. Its
   per-claim provenance footnotes (see `compile.SKILL.md`) tell you
   which fragment IDs back each claim.
3. Cite the compiled page directly. Don't paraphrase it from
   fragments — that re-introduces the lossy compression compile
   already paid for.

### 2. Fragments are the fallback, not the default

Drop down to `01-Fragments/` only when:

- The compiled page does not exist for the relevant target.
- The compiled page exists but is `inferred`-tier or contradicts what
  fresh fragments say (stale).
- An exact-quote use case demands the original fragment text — in
  which case the compiled page's provenance gives you the fragment
  IDs to load.

When you fall back, the system records the gap. `creek mine` and
`creek draft` log a `compile-needed` entry to
`00-Creek-Meta/Processing-Log/compile-gaps.jsonl`; agents and humans
should do the same when reading manually. `creek lint` later surfaces
these gaps so the human can recompile or accept.

### 3. `10-Liminal/` is a destination, not an overflow bin

Read from `10-Liminal/` only when paradoxes, unnamed patterns, or
synchronicities are themselves what's being queried. Liminal content
exists because compile **refused** to flatten it; treating it as a
fallback synthesis layer falsifies the refusal.

## Escape hatch: `--bypass-compiled`

`creek mine --bypass-compiled` and `creek draft --bypass-compiled`
skip the compiled layer entirely and read fragments directly. The
flag is documented, prints a stderr warning, and is for diagnostic
use only — for example, verifying that a compiled page faithfully
represents its source fragments. Routine reads must not pass it.

## Provenance traversal

Every claim on a compiled page carries a per-claim `[^claim-NN]`
footnote whose entry in frontmatter `provenance:` lists the fragment
IDs that produced it (see `compile.SKILL.md`). Use this when:

- A draft needs an exact quote from a source fragment.
- A reader needs to verify a claim against its sources.
- A lint pass detects drift between a compiled page and current
  fragments.

The provenance traversal is the bridge from the compiled summary back
to the raw record. Agents that collapse to "just read the fragments"
are doing query backwards — the design's whole point is that compile
already paid the cost of the rollup.

## Privacy tiers during query

Privacy tiers (Open / Personal / Intimate) flow with the data, not
with the verb:

- A compiled page that aggregates Personal-tier fragments must carry
  the highest contributing tier in its frontmatter.
- Default reads filter Intimate fragments out and replace Personal
  bodies with title-only summaries.
- The `--include-tier` override appends an audit entry to
  `<vault>/00-Creek-Meta/audit/privacy.jsonl` (spec §13.2).

A compile-first read is *not* a way around the tier filter. The
filter operates on whichever surface (compiled or raw) the query
ultimately reads.

## Canonical taxonomy

Query results carry the same tag vocabulary as everything else:

- **Phases (six):** Rising, Peaking, Withdrawal, Diminishing,
  Bottoming Out, Restoration.
- **Modes (five):** Inhabit, Express, Collaborate, Integrate, Absorb.
- **Orientations:** Do, Feel, Do/Feel.
- **Dosage:** Medicine, Toxic.
- **Frequencies (ten):** F1 Agency, F2 Receptivity, F3 Self-Love /
  Power, F4 Community Love / Conformity, F5 Achievism, F6 Pluralism,
  F7 Integration, F8 True Self / Transcendence, F9 Unity, F10
  Emptiness.

INC-019 reconciled drift here; do not paraphrase.

## What query does *not* do

- It does not write. Read-side only. If a query produces a useful
  artifact (a draft, a praxis note, a paradox) the result is filed
  via `creek save`, not by the query path.
- It does not auto-recompile. A missing compiled page logs a gap;
  recompile is a separate, human-triggered `creek compile` invocation.
- It does not collapse paradoxes. A query that lands in
  `10-Liminal/Paradoxes/` returns the paradox intact.
