# Emergence infrastructure

The Creek Ontology §10 carves out five emergence sub-systems — places where the vault is allowed to grow patterns the ontology itself does not predict. Each is implemented under `creek/generate/`, surfaced via `creek report --type <kind>`, and pinned by tests in `tests/`. This page documents the *exact* criteria that decide whether content is surfaced, so a missing pattern is recognisable as a bug rather than mistaken for intended behaviour.

> **FEAT-008** unifies all five subsystems under one verb: `creek lint`. The deterministic checks (broken wiki-links, orphan compiled pages, schema-skill size budgets, tags, compost) always run; the semantic checks (paradox, synchronicity, unnamed) run when `--since` is passed. See [`docs/lint.md`](lint.md) for the CLI surface and the non-negotiable "never resolve / never auto-create / never delete" rules. The legacy `creek report --type <kind>` entry points remain as thin wrappers.

The audit below maps each §10 criterion to its code path and test, so a future change that drifts from the spec lights up red.

| Spec | Module | Status |
|---|---|---|
| §10.1 Unnamed Digest | [`creek/generate/unnamed.py`](../creek/generate/unnamed.py) | ✅ implemented |
| §10.2 Paradox Preservation | [`creek/generate/paradox.py`](../creek/generate/paradox.py) | ✅ implemented |
| §10.3 Synchronicity Detection | [`creek/generate/synchronicity.py`](../creek/generate/synchronicity.py) | ✅ all four criteria pinned |
| §10.4 Compost Tracking | [`creek/generate/compost.py`](../creek/generate/compost.py) | ✅ implemented |
| §10.5 Emergent Tag Garden | [`creek/generate/tags.py`](../creek/generate/tags.py) | ✅ implemented |

---

## §10.1 — Unnamed Digest

`UnnamedDigestGenerator` collects fragments under `10-Liminal/Unnamed/` (skipping the `Digests/` subfolder) within a seven-day window and writes a digest at `10-Liminal/Unnamed/Digests/<iso-year>-W<week>.md`.

| Criterion | Implementation |
|---|---|
| **Weekly cadence** | Digest filename is `<iso-year>-W<week>`; runs are de-duplicated by week. |
| **Embedding similarity within Unnamed** | Cosine similarity over sentence-transformer embeddings. Threshold defaults to **0.7** (deliberately loose so faint clusters surface). |
| **Surface clusters** | Fragments are grouped by similarity; the digest renders one section per cluster with excerpts. |
| **Reflection prompt** | A non-prescriptive prompt asks what the clustered fragments share that the current ontology cannot express. |

CLI: `creek report --type unnamed --period weekly --vault <vault>`.

History persists to `00-Creek-Meta/Processing-Log/unnamed-history.json` so each run reports growth deltas.

---

## §10.2 — Paradox Preservation

`ParadoxDetector` finds contradictory stances *across* fragments from the same person without resolving them. A paradox is **always** kept and **never** flattened — it is a data point about polygnostic experience, not an error.

Four detection rules; a fragment pair matching any one becomes a paradox:

1. **Phase contradiction** — high semantic similarity between fragments sitting on opposite phases of the Archetypal Wavelength cycle.
2. **Confidence contradiction** — fragments on a shared thread carrying opposite confidence levels (e.g. `musing` vs `settled`).
3. **Dosage contradiction** — same primary frequency where one fragment marks it `medicine` and the other `toxic`.
4. **Keyword contradiction** — explicit contradiction phrases (`but actually`, `I used to think`, `contrary to what I said`) paired with a topically-linked fragment.

Output: a note in `10-Liminal/Paradoxes/` linking both fragments, a neutral description of the tension, and the `#paradox` tag plus relevant frequency tags. CLI: `creek report --type paradox`.

Re-running is idempotent (#1320) — a fragment pair already captured by an existing note is skipped, never rewritten, so a note you have annotated survives every later run. The key is the note's `fragments:` frontmatter, not its filename: the filename embeds the detection date, so before #1320 a weekly run left one copy of every paradox per calendar day. Vaults that already hold those copies are told so, by count and by path, on the next run — and nothing is deleted for you, because which copy carries your reflection is not a judgement the generator gets to make.

---

## §10.3 — Synchronicity Detection

`SynchronicityDetector` filters resonance pairs already discovered by the linking pass and surfaces only those that meet **all four** §10.3 criteria. The strictness is deliberate — synchronicities are reflective prompts, not knowledge links.

| Criterion (§10.3) | Pinned threshold |
|---|---|
| Semantic similarity strictly **>** 0.9 | `DEFAULT_SIMILARITY_THRESHOLD = 0.9` |
| Source types are **different** | `fragment_a.source.platform != fragment_b.source.platform` |
| Created **>** 30 days apart | `DEFAULT_MIN_TIME_GAP_DAYS = 30` |
| Not obviously about the same project / task — filter out "still working on X" noise | `_STATUS_UPDATE_PHRASES = ("still working on", "progress on", "update on")`, plus shared proper-noun project-name detection |

The pinned values live in [`creek/generate/synchronicity.py`](../creek/generate/synchronicity.py); the regression tests in [`tests/test_synchronicity.py`](../tests/test_synchronicity.py) cover each criterion. A regression PR that loosens any threshold without updating both tests and this doc fails the build.

Output: `10-Liminal/Synchronicities/<id>.md` linking the pair, the cosine score, the time gap in days, and a reflection prompt. CLI: `creek report --type synchronicity`.

---

## §10.4 — Compost Tracking

`CompostTracker` surfaces threads, fragments, and projects that have died or been abandoned and writes a compost note that **preserves** rather than deletes. Three detection paths:

1. **Thread dormancy** — a `Thread.status` is `resolved`, or `last_seen` is older than `dormancy_days` (180 by default).
2. **Fragment abandonment (FEAT-018)** — an embedding-similarity gate against curated exemplars (`creek/generate/exemplars/compost.yaml`) followed by an optional LLM verifier. Replaces the legacy five-phrase `_ABANDONMENT_KEYWORDS` regex with a semantic surface diverse enough to recognize abandonment in the operator's own voice. The verifier returns `yes` / `no` / `ambiguous`; ambiguous verdicts route to `10-Liminal/Compost/Review/` for manual triage rather than the canonical compost folder.
3. **Project silence** — a tag appears in at least `project_min_fragments` fragments but has not been mentioned for more than `project_gap_days`.

Privacy: intimate-tier fragments are skipped by default (never sent to the embedding gate or the verifier), respecting the same policy as `creek.classify.privacy_filter`. Paradox-tagged fragments are skipped too — paradoxes are contradiction-holding notes by design, not compost candidates.

Configuration lives under `compost:` in `creek_config.yaml`:

```yaml
compost:
  embedding_threshold: 0.6       # cosine floor for verifier handoff
  llm_verification: true         # set false for embedding-only acceptance
  review_queue_relpath: "10-Liminal/Compost/Review"
  skip_paradox: true
  exemplars_relpath: null        # null → packaged default
```

The compost note records:

- **What the idea was** — title plus one-paragraph summary.
- **Why it composted** — the verifier's one-sentence reasoning (or the embedding similarity, when the verifier is disabled).
- **What energy / insight it contained that may still be alive** — unresolved questions, active frequencies.
- **Links to referencing fragments** — wiki-links so the compost stays reachable.
- **Verifier metadata** — `embedding_similarity` and `verifier_reasoning` round-trip into frontmatter so the operator can audit decisions later.

Output: `10-Liminal/Compost/<id>.md` (canonical) or `10-Liminal/Compost/Review/<id>.md` (ambiguous, awaiting triage).

Detection (#882): `creek compost scan [--vault PATH] [--no-llm] [--dry-run] [--embedding-threshold 0.7]`. The production entry point — runs the embedding gate over the vault, prints the candidate counts and the resulting LLM-call estimate *before* verifying so the cost is refusable, then files confirmed candidates canonically and ambiguous ones to the review queue. `--no-llm` runs the gate alone and files every hit to the review queue: an embedding match on its own is a suspicion, not a finding, so it is never asserted as canonical compost. Without `--no-llm` an unavailable provider is a hard refusal rather than a silent downgrade, because this command writes to the vault. Intimate-tier fragments are dropped before the gate and never reach a provider. Re-scanning is idempotent — sources already carrying a compost note are skipped and reported, spending no LLM calls.

Config: `compost.llm_verification: false` is the standing form of `--no-llm`, and `compost.exemplars_relpath` (vault-relative, like `compost.review_queue_relpath`) points the gate at a custom exemplar set — override it only after `creek compost calibrate` shows the packaged defaults under-recall on your corpus.

`creek fill --with-compost` then regenerates `10-Liminal/Compost/_Compost-Report.md`, the Dataview overview across whatever the scan filed.

Calibration (FEAT-028): `creek compost calibrate [--fixture FIXTURE.yaml] [--json PATH] [--floor-recall 0.8] [--floor-precision 0.85] [--no-verifier]`. Runs the two-stage detector against a hand-labelled fixture (default: `tests/fixtures/compost-calibration.yaml`, 20+ positives × 20+ false-positive-risk negatives) and reports recall, precision, F1, false-positive rate, and per-stage hit counts (embedding-passed / verifier-yes / verifier-no / routed-to-review). `--floor-recall` and `--floor-precision` exit non-zero on a regression so a CI job can gate on detector quality. `--no-verifier` runs embedding-only for offline calibration.

---

## §10.5 — Emergent Tag Garden

`TagGardenGenerator` maintains `00-Creek-Meta/Tag-Garden.md` and a per-run history at `00-Creek-Meta/Processing-Log/tag-history.json`.

| Criterion | Implementation |
|---|---|
| List all tags in use with counts | Vault scan computes `{tag: count}`. |
| Highlight tags that are growing rapidly | History delta against last run; `new_tags` and `growing_tags` reported separately. |
| Flag tag clusters that may indicate a new Thread or Eddy | Co-occurrence analysis surfaces clusters above a configurable threshold. |
| Quarterly suggestion of consolidation / splits | The CLI accepts `--period quarterly`; tag-similarity (SequenceMatcher) suggests consolidations when two tags overlap > 0.85. |

Output: `00-Creek-Meta/Tag-Garden.md`. CLI: `creek report --type tags --period quarterly`.

---

## Pinning the thresholds

Every numeric threshold above is pinned by a regression test in `tests/`. Changes to:

- `DEFAULT_SIMILARITY_THRESHOLD` (synchronicity)
- `DEFAULT_MIN_TIME_GAP_DAYS` (synchronicity)
- The Unnamed-Digest similarity floor (0.7)
- The Tag-Garden growth detection deltas

require the corresponding test and this doc to move together. If you find a §10 criterion you cannot locate in code, open a GitHub issue (use a `BUG-*` prefix in the title so the design-trace lineage stays grep-able) rather than letting the gap go un-tracked.

---

## See also

- [`generation.md`](generation.md) — `creek report --type` table.
- [Ontology §10](../../docs/Ontology/creek_ontology_agent_prompt.md) — full spec.
- [`linking.md`](linking.md) — embedding linker that feeds the synchronicity and unnamed-digest detectors.
