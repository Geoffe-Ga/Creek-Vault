# INC-019: Spec/implementation drift — phase, mode, and frequency taxonomy mismatch

**Severity:** High
**Category:** INC
**Estimated complexity:** M (4–8h depending on which side becomes canonical)
**Parallelizable with peers in same category:** no (this resolves the canonical taxonomy that other INCs / docs depend on)
**Discovered by:** Comparative-analysis pass (`plans/2026-05-05_comparative-analysis/`); flagged in `LANDSCAPE.md` closing paragraph and `DELTA-MATRIX.md` synthesis as a v1 prerequisite for compile-then-query adoption.
**GitHub issue:** [#201](https://github.com/Geoffe-Ga/Creek-Vault/issues/201)

## Files affected

### Phase names
- **Spec (canonical):** `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md:421-434, 469-479` — uses `Rising / Peaking / Withdrawal / Diminishing / Bottoming Out / Restoration`.
- **Docs (drift):** `creek-tools/docs/classification.md:3` — uses `Origins / Rising / Peaking / Cresting / Receding / Composting`.
- **Docs (drift):** `creek-tools/docs/generation.md:50, 86` — references `cresting` phase in CLI examples.
- **Code (drift):** `creek-tools/creek/generate/compost.py:504` — emits "Composting Ideas" header.

### Mode names
- **Spec (canonical):** `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md:451-463` — uses `Inhabit / Express / Collaborate / Integrate / Absorb` (each paired with Do/Feel orientation).
- **Docs (drift):** `creek-tools/docs/classification.md:34` — uses `solo | dialogue | reflective | analytic`.
- **Docs (drift):** `creek-tools/docs/generation.md:51` — references `modes/{solo,dialogue,reflective,analytic}/SKILL.md`.

### Frequency names
- **Spec (canonical):** `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md:396-407` — APTITUDE F1–F10 with names `Agency / Receptivity / Self-Love-Power / Community-Love / Achievism / Pluralism / Integration / True Self / Unity / Emptiness`.
- **Docs (drift):** `creek-tools/docs/classification.md:32`, `creek-tools/docs/generation.md:47-48, 118` — uses wave-physics names `amplitude`, `pitch` instead of APTITUDE F1–F10.

## Dependencies

None (this *creates* the prerequisite). Blocks:
- `plans/2026-05-05_comparative-analysis/candidates/ADOPT-001-three-layer-compiled-architecture.md` (compile-then-query needs one source of truth for the load-bearing taxonomy).
- `plans/2026-05-05_comparative-analysis/candidates/ADOPT-002-creek-lint-unified-hygiene.md` (lint runs over the same taxonomy).
- `plans/2026-05-05_comparative-analysis/candidates/ADOPT-005-audit-report-as-artifact.md` (audit report's wavelength snapshot reads phase/mode names).

## Reproduction

```bash
# Phase drift.
grep -n 'Origins\|Cresting\|Receding\|Composting' \
  creek-tools/docs/classification.md \
  creek-tools/docs/generation.md \
  creek-tools/creek/generate/compost.py

# Mode drift.
grep -rn 'solo.*dialogue\|reflective.*analytic' creek-tools/docs/

# Frequency drift.
grep -rn 'amplitude\|pitch' creek-tools/docs/ creek-tools/creek/
```

## Analysis

The canonical specification (`creek_ontology_agent_prompt.md`) defines the wavelength taxonomy with maximal semantic precision: six phases that map to a narrative of Abundance/Indulgence/Scarcity/Resilience, five modes paired with Do/Feel orientations and Medicine/Toxic dosage gradients, and ten APTITUDE frequencies tied to Spiral Dynamics colors. Every section of the spec — emergence infrastructure (§10), voice proxy generation (§11), decision support (§12), interventions map (§12.5) — is anchored in this taxonomy.

The `creek-tools/` implementation has drifted on all three axes:

- **Phase names** look like a stylistic variant (Origins/Cresting/Receding/Composting) of the canonical six. They are not isomorphic — the spec's six phases each carry specific narrative, dosage, and intervention mappings that the docs' alternative names do not preserve.
- **Mode names** (`solo / dialogue / reflective / analytic`) appear to be a different taxonomy entirely — they don't map to the spec's Inhabit/Express/Collaborate/Integrate/Absorb framework, lose the Do/Feel orientation, and lose the Medicine/Toxic dosage gradient.
- **Frequency names** (`amplitude / pitch`) appear to use wave-physics vocabulary; the spec is APTITUDE F1–F10 with developmental/spiritual semantics. These are unrelated taxonomies.

This is the most operationally important drift in the project. Compile-then-query (the v1 commitment from the comparative analysis) requires every consumer (ingestion classifier, lint, audit report, voice-skill tree, mining strategies, drafting prompts, CrawDad's Haiku router) to agree on what the wavelength axes are. Today they don't.

The spec is canonical. The drift lives in `creek-tools/`. Resolution direction is to bring `creek-tools/` to the spec, not the other way around.

Confidence: verified across files listed in "Files affected."

## Proposed remediation

1. **Reconcile phase names.** Rename `Origins / Cresting / Receding / Composting` → spec's `Withdrawal / Diminishing / Bottoming Out / Restoration` in `creek-tools/docs/classification.md`, `creek-tools/docs/generation.md`, and `creek-tools/creek/generate/compost.py`. Update CLI examples that reference removed names. Update any pre-existing fragments in test fixtures that carry the drifted names.
2. **Reconcile mode names.** Replace `solo / dialogue / reflective / analytic` with spec's `Inhabit / Express / Collaborate / Integrate / Absorb`, plus the Do/Feel orientation field and the Medicine/Toxic dosage field. This is the largest of the three changes because the docs' four-mode taxonomy is genuinely different from the spec's five-mode-with-orientation system.
3. **Reconcile frequency names.** Replace `amplitude / pitch` (and any other wave-physics names that emerge during reconciliation) with the canonical APTITUDE F1–F10 names from the spec. Each frequency carries a Spiral Dynamics color and core theme that must come through.
4. **Add a regression test** that verifies the phase/mode/frequency enum values in `creek/models.py` (or wherever they live) match the spec's vocabulary, and that the docs-vs-spec terminology doesn't drift again silently. Pin the canonical strings via a tests/fixture and fail the build if any drift returns.
5. **Provide a one-release migration alias** (similar to the INC-003 pattern) so any pre-existing fragment with drifted phase/mode/frequency values loads with a deprecation warning rather than hard-failing.

## Acceptance criteria

- `grep -rn 'Origins\|Cresting\|Receding\|Composting' creek-tools/` returns zero hits in user-facing strings (test fixtures excepted if they exercise the migration alias path).
- `grep -rn 'solo.*dialogue\|reflective.*analytic' creek-tools/` returns zero hits in user-facing strings.
- `grep -rn 'amplitude\|pitch' creek-tools/` returns zero hits in user-facing strings.
- `creek/models.py` enum definitions for phase, mode, and frequency match the spec's vocabulary verbatim.
- A regression test asserts that each enum's value list equals the spec's canonical list (load from a fixture so the test fails when either the spec or the model drifts).
- For one release, fragments with drifted values load with a deprecation warning and a documented mapping to canonical values; after that release, the migration alias is removed.
- `creek-tools/docs/classification.md` and `creek-tools/docs/generation.md` use the spec's vocabulary verbatim.
- The voice-skill tree's `creek-skills/frequencies/` and `creek-skills/phases/` and `creek-skills/modes/` directories use canonical names.

## References

- Comparative analysis flagging: `plans/2026-05-05_comparative-analysis/LANDSCAPE.md` closing paragraph.
- Comparative analysis synthesis: `plans/2026-05-05_comparative-analysis/DELTA-MATRIX.md` final synthesis paragraph.
- Canonical taxonomy:
  - `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md:396-407` (frequencies)
  - `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md:421-434` (phases)
  - `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md:451-463` (modes + orientation)
  - `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md:467-493` (Medicine/Toxic dosage maps)
- Drifted implementations:
  - `creek-tools/docs/classification.md:3, 32, 34`
  - `creek-tools/docs/generation.md:47-51, 86, 118`
  - `creek-tools/creek/generate/compost.py:504`
- Related: `INC-003` (privacy-tier naming divergence — analogous reconciliation pattern).
