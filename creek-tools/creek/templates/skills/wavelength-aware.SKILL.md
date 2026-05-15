# wavelength-aware.SKILL.md

**Concern:** phase as the lens through which every other tag is interpreted
**Layer:** schema → re-reads frequency / mode / orientation / dosage in the human's *current* phase
**Budget:** ≤1500 tokens
**Loaded by:** Claude Code, CrawDad, every classifier, every generation flow

## Why phase is the lens

Frequency, mode, orientation, and dosage are descriptive. **Phase tells the agent which reading is live right now.** A fragment about "Power-With" (Red Express/Do Medicine) reads differently when the human is in Peaking versus Diminishing — same tag, different operative meaning. An agent that ignores phase will produce technically-classified-yet-tone-deaf output: matching the labels, missing the human.

This is the wavelength-aware discipline. Every other schema-skill (`compile`, `lint`, `save`, `paradox`, `liminal`, `privacy-tier`) consumes its rules; this skill names them.

## The four wavelength axes

All four axes use the canonical taxonomy verbatim (INC-019). No synonyms, no abbreviations.

### Six phases

The Archetypal Wavelength has **six** phases — not four, not five — mapping a narrative of Abundance and Scarcity:

| Phase | Narrative | Operative reading |
|---|---|---|
| **Rising** | Abundance begins to create Indulgence | Energy building; commitment, inspiration, fresh momentum. |
| **Peaking** | Abundance peaks | Full expression, flow, glory, attunement. |
| **Withdrawal** | Indulgence creates Scarcity | First cracks; doubt, distraction, the come-down. |
| **Diminishing** | Scarcity begins to create Resilience | Active decline; loss of focus, contraction, radical acceptance. |
| **Bottoming Out** | Scarcity peaks | Maximum contraction; despair, fallow, dark-night material. |
| **Restoration** | Resilience creates Abundance | Re-emergence; vulnerability, new flow, healthy craving. |

(Spec §7.1.)

### Five modes × Do/Feel orientation

Modes are functional stances, not emotions. Each pairs with a **Do** or **Feel** orientation, except Absorb which collapses to Do/Feel (Ultraviolet, where Do and Feel merge):

| Mode | Orientations | Spiral Dynamics colours |
|---|---|---|
| **Inhabit** | Do (Beige) / Feel (Purple) | Body and kinship presence. |
| **Express** | Do (Red) / Feel (Blue) | Outward projection of power or devotion. |
| **Collaborate** | Do (Orange) / Feel (Green) | Working with reality; experiment or empathy. |
| **Integrate** | Do (Yellow) / Feel (Teal) | Weaving parts into wholes. |
| **Absorb** | Do/Feel (Ultraviolet) | Dissolving into unified awareness. |

(Spec §7.2.)

### Medicine vs. Toxic dosage

Every Mode × Phase cell has a **Medicine** (right-sized, healthy) and a **Toxic** (overdose, shadow) expression. Same Mode + Phase, two cells; the agent must surface both whenever a fragment hovers near the boundary rather than collapsing to the majority.

Examples (full table in spec §7.3):

- **Purple, Withdrawal** → Medicine: Introspectivity. Toxic: Anxiety.
- **Red, Peaking** → Medicine: Power-With. Toxic: Power-Over.
- **Green, Diminishing** → Medicine: Unwinding. Toxic: Alienation.
- **Ultraviolet, Bottoming Out** → Medicine: Pleasure. Toxic: Aversion.

When a fragment could plausibly read either way, save both readings; let the human resolve at review.

## The wavelength snapshot

The agent does not guess the human's current phase from in-session signals. It reads the **wavelength snapshot**:

> **Snapshot location: `00-Creek-Meta/State/latest.md`** (created by FEAT-007 — `creek state`).

The snapshot is a small Markdown file with frontmatter naming the human's *currently observed* phase, mode, orientation, and dosage tendency, plus a confidence score and an `as_of` timestamp. Every wavelength-aware flow reads this file at session start and treats its contents as the operative lens until a fresher snapshot is written.

Indicative shape (FEAT-007 finalises the schema):

```yaml
---
type: state-snapshot
as_of: "YYYY-MM-DDTHH:MM:SS-08:00"
window_days: 14
phase: rising | peaking | withdrawal | diminishing | bottoming_out | restoration
mode: inhabit | express | collaborate | integrate | absorb
orientation: do | feel | do_feel
dosage_trend: medicine | toxic | mixed
confidence: 0.0-1.0
---
```

If the snapshot is missing, stale (`as_of` > 30 days), or fails to load, the agent falls back to **phase: unclassified, confidence: 0.0** and notes the degraded read in its output. It does not invent a phase.

## What "wavelength-aware" looks like in each verb

- **`creek classify`** uses phase to disambiguate Medicine/Toxic readings of an otherwise-clear Mode + Frequency.
- **`creek compile`** preserves phase in compiled-page frontmatter; when source fragments disagree on phase, surfaces both (`wavelength.phase` + `wavelength.observed_phase`) rather than collapsing.
- **`creek lint`** reports may use phase to weight stale-claim and contradiction signals (a Restoration-phase claim contradicting a Diminishing-phase claim is *typical* across cycles, not a bug).
- **`creek save`** inherits the snapshot's phase by default, with the dual-phase escape per `save.SKILL.md`.
- **`creek mine` / `creek draft`** filter and rank by phase fit so the surfaced ideas match where the human actually is, not an average across the corpus.
- **CrawDad** loads the snapshot at session start and lets the Haiku router weight phase-matched skills more heavily.

## What the agent must never do

1. **Never paraphrase the canonical names.** `Bottoming Out` not `bottoming-out` in prose; `bottoming_out` only as the YAML enum value. INC-019 reconciled drift; do not re-introduce it.
2. **Never collapse Medicine and Toxic to "neutral."** Boundary readings stay tagged on both sides.
3. **Never silently overwrite the snapshot.** Updates flow through `creek state` (FEAT-007); ad-hoc edits violate the audit trail.
4. **Never read the snapshot through privacy-tier escalation.** The snapshot itself is `personal`-tier metadata; reading it does not unlock intimate fragments. See `privacy-tier.SKILL.md`.
5. **Never assume Absorb has a Do-only or Feel-only reading.** Absorb is Do/Feel; the orientation collapses.

## Canonical taxonomy

- **Phases (six):** Rising, Peaking, Withdrawal, Diminishing, Bottoming Out, Restoration.
- **Modes (five):** Inhabit, Express, Collaborate, Integrate, Absorb.
- **Orientations:** Do, Feel, Do/Feel.
- **Dosage:** Medicine, Toxic.
- **Frequencies (ten):** F1 Agency, F2 Receptivity, F3 Self-Love / Power, F4 Community Love / Conformity, F5 Achievism, F6 Pluralism, F7 Integration, F8 True Self / Transcendence, F9 Unity, F10 Emptiness.

## Reference

- Spec §7.1 (six phases), §7.2 (five modes × Do/Feel), §7.3 (Medicine vs. Toxic full map), §7.4 (how to use modes for classification), §7.5 (temporal wavelength tracking).
- FEAT-007 — `creek state` writes `00-Creek-Meta/State/latest.md`.
- `compile.SKILL.md` (frontmatter shape), `save.SKILL.md` (dual-phase convention).
