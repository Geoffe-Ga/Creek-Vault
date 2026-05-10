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

`CompostTracker` surfaces threads and projects that have died or been abandoned and writes a compost note that **preserves** rather than deletes. Triggers:

- A `Thread.status` transitions to `resolved`.
- Fragments reference abandoned projects (heuristic: fragments tagged `abandoned`, `let-go`, or referencing dormant threads).

The compost note records:

- **What the idea was** — title plus one-paragraph summary.
- **Why it was abandoned** — extracted from referencing fragments when explicit; left as a prompt otherwise.
- **What energy / insight it contained that may still be alive** — unresolved questions, active frequencies.
- **Links to referencing fragments** — wiki-links so the compost stays reachable.

Output: `10-Liminal/Compost/<id>.md`. CLI: `creek report --type compost`.

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

require the corresponding test and this doc to move together. If you find a §10 criterion you cannot locate in code, file a `BUG-*` under `plans/git-issues/` rather than letting the gap go un-tracked.

---

## See also

- [`generation.md`](generation.md) — `creek report --type` table.
- [Ontology §10](../../00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md) — full spec.
- [`linking.md`](linking.md) — embedding linker that feeds the synchronicity and unnamed-digest detectors.
