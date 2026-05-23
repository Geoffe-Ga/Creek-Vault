# Frequency Naming: Canonical Names Win

- **Status**: Accepted
- **Date**: 2026-05-23
- **Driving issue**: ONTOLOGY-001 (#265)
- **Surfaced by**: PR #244 (calibration rebalance) flagged a substantive
  naming drift between the canonical taxonomy and the few-shot
  rationales the LLM classifier sees.

## Context

The Creek Ontology defines ten APTITUDE frequencies (F1..F10) in
§6.1 of [`docs/Ontology/creek_ontology_agent_prompt.md`](../Ontology/creek_ontology_agent_prompt.md).
The canonical names there are:

| Code | Canonical name | Core theme (one-liner) |
|------|----------------|------------------------|
| F1 | **Agency** | Survival, intentional action, willpower, initiative |
| F2 | **Receptivity** | Kinship, receptivity to pleasure and Source, intuitive divination, surrender, trust |
| F3 | Self-Love / Power | Self-love as the foundation of healthy power |
| F4 | Community Love / Conformity | Community love, devotion, moral grounding, hierarchy |
| F5 | Achievism | Innovation, analysis, goal-setting, material success |
| F6 | Pluralism | Empathy, inclusivity, embodied connection, shadow work |
| F7 | Integration | Systems thinking, synthesis, holistic understanding |
| F8 | **True Self / Transcendence** | Higher self, monad, gnosis, alignment, pattern recognition from a transcendent vantage |
| F9 | Unity | Source connection, cosmic harmony, non-dual awareness |
| F10 | Emptiness | Impermanence, no-self, egolessness |

Two downstream artefacts had drifted from these names:

1. **Few-shot rationales** in `creek/classify/examples/frequency.yaml`
   used `F1: survival`, `F2: tribal-belonging`, `F8: ecological holism`.
   The first compresses the canonical theme; the latter two pick a
   nearby-but-different reading.
2. **Calibration entries** in
   `tests/fixtures/classification/calibration_set.yaml` were labelled
   to match the few-shot framing. `cal-002 "Team retro"` and
   `cal-018 "Picking the fight again"` are clear cases — both labelled
   F2 but describing tribal-belonging dynamics, not Receptivity.

The classifier currently sees the drifted framing; the canonical
taxonomy spec sees the canonical framing; reports and consumers read
whichever happened to land in their corner of the codebase. This is
the kind of silent disagreement that erodes trust in classification
outputs over time.

## Decision

**Keep the canonical names** (Agency / Receptivity / True Self /
Transcendence). They are already used in:

- `docs/Ontology/creek_ontology_agent_prompt.md` (the spec)
- `tests/fixtures/canonical_taxonomy.yaml` (the regression anchor)
- `creek/generate/indexes.py` (`FREQUENCY_NAMES`, `FREQUENCY_THEMES`)
- `creek/templates/vault/06-Frequencies/F1-Agency/` …
  `/F8-True-Self/` (the deployed scaffold)
- Every `creek/templates/skills/*.SKILL.md` (the agent contract)

The drifted glosses ("survival", "tribal-belonging", "ecological
holism") only live in the few-shot rationale text and a handful of
calibration entries. Aligning those is much cheaper than re-renaming
every other surface.

### Why these names, not the drifted ones

- **Agency vs survival.** Agency includes survival texture (the first
  content signal in the canonical spec is "basic needs"), but it is
  not limited to it — initiative, willpower, and proactive action are
  the broader register. The drifted "survival" gloss collapses the
  fuller meaning.
- **Receptivity vs tribal-belonging.** F2 is about *receiving* —
  receptivity to pleasure, to Source, to kinship bonds, to intuitive
  insight. Tribal-belonging slides toward conformity dynamics that
  belong to F4 (Community Love / Conformity), not F2. The drift
  hollows out the spiritual texture (Source, surrender, trust) the
  canonical name carries.
- **True Self / Transcendence vs ecological holism.** F8 is "the
  aspect of Source incarnated as you … pattern recognition from a
  transcendent vantage." Ecological holism is *an instance* of that
  vantage (seeing the personal and planetary as one body), not the
  category itself. The drifted gloss promotes the example to the
  definition.

### Changes in this PR

- `creek/classify/examples/frequency.yaml`: every rationale rewritten
  to use the canonical name in the form `F<n> <Canonical Name> — …`.
  The F2 example body was rewritten to better fit Receptivity (the
  "Team retro" example is preserved in the calibration set under
  Receptivity-aligned text). The F8 example body (watershed walk) was
  preserved and its rationale recast as "pattern recognition from a
  transcendent vantage."
- `tests/fixtures/classification/calibration_set.yaml`: `cal-002` and
  `cal-018` bodies rewritten to fit canonical Receptivity. Other
  dimensions (phase / mode / orientation / dosage / register) were
  preserved so the calibration coverage profile does not shift.
- This decision record.

## Out of scope (deliberately deferred)

- **`creek/classify/llm.py`'s `CLASSIFICATION_PROMPT`** still names
  frequencies as `F1: Survival/Safety, F2: Belonging/Tribe, ...` in
  the inline prompt header. This is the most operationally
  consequential drift because it goes directly into every classifier
  call, but updating it is a Python source change and so falls
  outside ONTOLOGY-001's scope (which is data + docs only). File as a
  follow-up if the calibration floor gate breaks after this change
  lands.
- **Re-classifying already-labelled vault fragments.** The frequency
  IDs (`F1`..`F10`) are stable; only the human-readable interpretation
  changes. If the operator finds vault content that now reads
  awkwardly under the canonical name, that is content-curation work,
  not schema migration.
- **F9 vs F10 boundary.** Auditing the rest of the ten surfaced a
  borderline reading on F9 ("Just watching") and F10 ("One thing")
  where Unity and Emptiness rationales touch. The canonical spec
  distinguishes them (F9 = source-connection / cosmic harmony,
  F10 = no-self / impermanence); the existing examples were kept
  with rationale text that names the canonical distinction. A future
  audit can tighten further if real classifier behaviour shows
  confusion.

## Consequences

**Positive**

- One name per frequency across the entire toolchain — spec,
  templates, indexes, skills, few-shots, calibration set.
- Future contributors reading the canonical spec see the same
  vocabulary the classifier uses; no translation table needed.
- Calibration metrics become interpretable: a drop in F2 agreement
  means "the model is confusing Receptivity with something else,"
  not "the model is using a different gloss for F2 than we are."

**Negative**

- The `CLASSIFICATION_PROMPT` in `creek/classify/llm.py` still names
  F1/F2/F8 with the older terminology. This may show up as a
  calibration-floor regression on the next `creek classify
  --calibrate` run; the follow-up to align the prompt is filed
  separately because ONTOLOGY-001 was scoped to data + docs.
- Two calibration entries (cal-002, cal-018) carry rewritten bodies.
  Anyone with a personal mental cache of "cal-002 = team retro" will
  need to refresh.

## Alternatives considered

- **Adopt the drifted glosses as canonical.** Rejected: the spec is
  the older, more developed surface; the ecosystem already uses the
  canonical names; the spiritual texture of "Receptivity" and "True
  Self" would be lost.
- **Carry both names as synonyms.** Rejected: classification systems
  work best with single canonical labels per category. Synonyms in
  the prompt train the model to be ambivalent.
- **Defer until a model-level recalibration.** Rejected: the drift
  was actively misleading consumers of the canonical taxonomy file;
  fixing the data while the issue is on the radar costs little and
  removes a confusion source today.
