# liminal.SKILL.md

**Concern:** when to leave content uncategorized
**Layer:** schema → defines the rules of the fourth (liminal) layer
**Budget:** ≤1500 tokens
**Loaded by:** Claude Code, CrawDad, any agent that classifies, compiles, lints, or saves

## The four-layer architecture

Creek's data model has **four** layers, not three. The fourth is the load-bearing one — it is what makes Creek different from a wiki.

| Layer | Folder(s) | Owner | Mutability |
|---|---|---|---|
| **Raw** | `01-Fragments/` | Human + ingestor | Append-only after ingest |
| **Compiled** | `02-Threads/`, `03-Eddies/`, `04-Praxis/`, `06-Frequencies/` | LLM-curated, human-reviewed | Rewritable by `creek compile` |
| **Schema** | `00-Creek-Meta/Ontology/`, `00-Creek-Meta/Skills/`, `AGENTS.md` | Human + agent co-evolved | Versioned; deliberate |
| **Liminal** | `10-Liminal/Paradoxes/`, `10-Liminal/Unnamed/`, `10-Liminal/Synchronicities/`, `10-Liminal/Compost/` | LLM-surfaced, human-curated | Append-on-emergence; promotion is human-only |

## The liminal layer is *not* a wiki sub-folder

Say it once, plainly: **the liminal layer is not a wiki sub-folder.**

A wiki sub-folder is overflow — a place to put content that doesn't fit anywhere else, with the implicit goal of eventually re-filing it where it "belongs." Liminal is the opposite. It is a first-class destination with its own ontology and its own preservation rules. Content that lands here is not waiting to be re-filed; it is exactly where it should be until the system has earned the right to file it differently.

This is the **fourth-layer distinction**, and it is non-negotiable per the INTEGRATION-PLAN.md distinctiveness watchlist.

## The four liminal sub-folders

Each sub-folder is a different *kind* of refusal-to-flatten. They are not interchangeable.

### `10-Liminal/Paradoxes/`

Two or more fragments hold *directly opposing stances on a settled claim*. The system never picks a winner. See `paradox.SKILL.md` for the full rule (verbatim: "Contradictions are data, not defects. Route to `10-Liminal/Paradoxes/`. Do not propose resolution.").

### `10-Liminal/Unnamed/`

A pattern is clearly real — recurring, weighty, present in multiple fragments — but does not fit any existing Thread, Eddy, Praxis, or Frequency. Lint routes it here; compile defers to it; CrawDad surfaces it in the weekly Unnamed Digest. It is not a backlog. It is a research site (spec §10.1).

### `10-Liminal/Synchronicities/`

Fragments from very different sources/times that read as semantically near-identical (similarity > 0.9, ≥30 days apart, different platforms, not just "still working on X"). The system flags the resonance; the human decides whether the synchronicity is meaningful (spec §10.3).

### `10-Liminal/Compost/`

Threads that have gone dormant or resolved with abandoned energy; ideas, projects, and stances that the human stopped feeding. They are not deleted — they decompose into future growth. A Compost note records what the energy was, why it ended (when known), and what insight may still be alive in it (spec §10.4).

## When the agent routes to Liminal

Three triggers:

1. **The classifier is uncertain.** Ambiguous frequency, no fitting phase, mode unreadable — the fragment goes to `01-Fragments/Unsorted/` (raw) or, if the *pattern* is the unfittable thing, to `10-Liminal/Unnamed/` (liminal).
2. **`creek compile` would lose information.** A compile run that would silently flatten a contradiction routes that contradiction to `10-Liminal/Paradoxes/` instead. A compile that would produce a Thread synthesizing fragments that do not actually share a narrative current routes the cluster to `10-Liminal/Unnamed/`.
3. **`creek lint` finds an emergent pattern.** Missing pages, surprise resonances, dormant threads — lint routes them to the appropriate Liminal sub-folder. Lint never auto-creates compiled pages and never resolves paradoxes.

The shared rule: **synthesis-that-would-falsify routes to Liminal. Synthesis-that-would-clarify produces a compiled-layer page.**

## Promotion out of Liminal

Promotion is human-only. The four mechanics:

- **Unnamed → Thread / Eddy / Praxis.** When an Unnamed cluster has matured (fragments accumulate; the human names it; the Unnamed Digest signal stays loud across multiple weeks), the human invokes `creek save` or `creek compile` to seed the new compiled-layer page. The Unnamed note stays as historical context.
- **Paradox → held forever.** Paradoxes do not get promoted. A new fragment may name a present-tense resolution; the Paradox note remains as evidence that both stances were once held.
- **Synchronicity → Thread or Praxis (rare).** Most Synchronicities stay where they are. When a synchronicity recurs enough to suggest a directional pattern, the human can promote.
- **Compost → no promotion.** Compost is terminal in one direction (the idea stopped being fed) but generative in another (it informs future fragments). A Compost note may be *referenced* by new compiled pages; it is never re-activated in place.

## Frontmatter discipline

Every Liminal note carries:

```yaml
type: paradox | unnamed | synchronicity | compost
id: {liminal-type}-{uuid-short}
title: "{the pattern in the human's voice}"
created: "YYYY-MM-DDTHH:MM:SS-08:00"
contributing_fragments:
  - frag-{id}
frequency:
  primary: F1 | F2 | F3 | F4 | F5 | F6 | F7 | F8 | F9 | F10 | unclassified
  secondary: []
wavelength:
  phase: rising | peaking | withdrawal | diminishing | bottoming_out | restoration
  mode: inhabit | express | collaborate | integrate | absorb
  orientation: do | feel | do_feel
  dosage: medicine | toxic
status: held | dormant | resolved-with-abandoned-energy
```

Liminal notes use the canonical taxonomy verbatim (INC-019). Phases: Rising, Peaking, Withdrawal, Diminishing, Bottoming Out, Restoration. Modes: Inhabit, Express, Collaborate, Integrate, Absorb. Frequencies: F1–F10 by their canonical names.

## What Liminal is not

- **Not a backlog.** Items here are not waiting to be processed.
- **Not a junk drawer.** Routing decisions are deliberate, per the rules above.
- **Not exempt from privacy tiers.** A Liminal note inherits the most-restrictive tier of its contributing fragments — but the agent must determine that tier and pass it explicitly via `--tier` when saving; `creek save` will refuse rather than guess. See `privacy-tier.SKILL.md`.
- **Not a place to bypass `creek purge`.** Deletion still requires elevated auth.

## Reference

- Spec §10 (Emergence Infrastructure) — sub-sections §10.1 Unnamed, §10.2 Paradox, §10.3 Synchronicity, §10.4 Compost.
- INTEGRATION-PLAN.md distinctiveness watchlist: liminal-as-fourth-layer is the highest-priority non-negotiable.
- `paradox.SKILL.md`, `compile.SKILL.md` ("When compile refuses"), `lint.SKILL.md`, `save.SKILL.md`.
