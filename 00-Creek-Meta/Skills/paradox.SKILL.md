# paradox.SKILL.md

**Concern:** contradictions across fragments
**Layer:** schema → routes contradictions to the liminal layer
**Budget:** ≤1500 tokens
**Loaded by:** Claude Code, CrawDad, any agent that classifies, compiles, lints, or saves

## The load-bearing rule

> **Contradictions are data, not defects. Route to `10-Liminal/Paradoxes/`. Do not propose resolution.**

This sentence is the difference between a generic-wiki agent and a Creek agent. Karpathy's wiki treats a contradiction as a defect to fix — pick a winner, edit the loser, move on. Creek treats a contradiction as a data point about a polygnostic human whose stance on the same claim has shifted across phases, modes, or seasons. The contradiction is the evidence; flattening it destroys the evidence.

Repeat verbatim, do not paraphrase. `lint.SKILL.md` carries the corresponding "lint never resolves paradoxes" form of this rule; `save.SKILL.md` exposes it as the `paradox` save destination.

## What counts as a paradox

A paradox is two or more fragments from the same human that hold *directly opposing stances on a settled claim*. The diagnostics are:

- **Same claim.** Both fragments are about the same proposition (about the human, about a project, about a relationship, about a practice).
- **Settled stance.** Each fragment commits to its position — not "I'm wondering whether X" but "X is true" or "X is false."
- **Direct opposition.** Not nuance, not refinement, not "I changed my mind in March." A paradox holds *both* stances as live; a chronological revision is just an updated claim.

If the fragments are about different claims, or are not settled, or are temporally ordered along a single trajectory, this is not a paradox — and routing it to `10-Liminal/Paradoxes/` would dilute the folder's signal. Surface it as a stale-claim flag in `creek lint` instead.

## What to do with a paradox

When detected (by `creek lint`'s contradiction check, by a CrawDad reflection, or by the human noticing it during review):

1. **Create a note in `10-Liminal/Paradoxes/`** that names the paradox in the human's own words wherever possible.
2. **Link both (or all) contributing fragments** by ID — do not embed bodies; the fragments stay sovereign in `01-Fragments/`.
3. **Tag with `#paradox`** plus the relevant Frequencies (whichever F1–F10 the contradiction lives at) and the Phase under which each side was held, when known.
4. **Do not pick a winner. Do not annotate one side as "wrong."** The paradox note describes the tension; it does not resolve it.
5. **Surface the paradox upward** — `creek compile` will read the Paradox note's frontmatter and refuse to flatten the underlying contradiction into a Thread or Eddy synthesis.

The Paradox note's frontmatter:

```yaml
type: paradox
id: paradox-{uuid-short}
title: "{the paradox in the human's voice}"
created: "YYYY-MM-DDTHH:MM:SS-08:00"
contributing_fragments:
  - frag-{id}
  - frag-{id}
frequency:
  primary: F1 | F2 | F3 | F4 | F5 | F6 | F7 | F8 | F9 | F10 | unclassified
  secondary: []
wavelength:
  phase_a: rising | peaking | withdrawal | diminishing | bottoming_out | restoration
  phase_b: rising | peaking | withdrawal | diminishing | bottoming_out | restoration
status: held         # never `resolved`; deletion only via `creek purge`
```

## Canonical taxonomy

Paradoxes use these names verbatim (INC-019 reconciliation):

- **Phases (six):** Rising, Peaking, Withdrawal, Diminishing, Bottoming Out, Restoration.
- **Modes (five):** Inhabit, Express, Collaborate, Integrate, Absorb.
- **Orientations:** Do, Feel, Do/Feel.
- **Dosage:** Medicine, Toxic.
- **Frequencies (ten):** F1 Agency, F2 Receptivity, F3 Self-Love / Power, F4 Community Love / Conformity, F5 Achievism, F6 Pluralism, F7 Integration, F8 True Self / Transcendence, F9 Unity, F10 Emptiness.

Every Paradox note inherits these tags from its contributing fragments — never collapse divergent tags to a majority. If side A is Bottoming Out / Toxic and side B is Restoration / Medicine, both belong on the note; that is precisely the data the paradox preserves.

## What the agent must never do

These are non-negotiable:

1. **Never propose a synthesis.** "Both are true in their own way" is a synthesis. So is "the truth is in the middle." Name the tension; stop there.
2. **Never edit the contributing fragments to align them.** Fragments are append-only after ingest; the Paradox note is the only place the contradiction is named.
3. **Never auto-promote a Paradox to a Thread or Eddy.** A Thread tells a directional story; an Eddy is a topic cluster. A Paradox is neither — it is the system honoring §10.2 of the spec.
4. **Never delete a Paradox note silently.** Deletion goes through `creek purge` with elevated auth; a paradox that "no longer feels true" is itself data about the human's wavelength position.
5. **Never let a Paradox short-circuit privacy tiers.** If either contributing fragment is `intimate`, the Paradox note inherits `intimate` and the inclusion rules in `privacy-tier.SKILL.md` apply.

## When a paradox does get re-examined

Sometimes the human revisits a Paradox and decides one side has, in fact, been outgrown. The right move is *not* to delete the Paradox or to edit it into a synthesis. It is to **save a new fragment** that names the resolution in the present tense, then let `creek compile` produce a Thread or Praxis page that links the Paradox note as historical context. The Paradox stays held; the new fragment carries the new stance. The vault remembers both.

## Reference

- Spec §10.2 (Paradox Preservation) and §10 (Emergence Infrastructure).
- INTEGRATION-PLAN.md distinctiveness watchlist: paradox preservation is one of the four non-negotiables.
- `lint.SKILL.md` for the contradiction-detection check; `save.SKILL.md` for the `paradox` destination.
