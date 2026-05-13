# Decision support

The decision-support layer (Ontology §12) detects decision-relevant fragments, drafts a Decision note in the vault, gathers cross-vault context, and enforces the **anti-manipulation guardrails** in §12.4 — the rules that keep this from becoming a recommender system.

This page is the user-facing manual for what the implementation actually does. It complements [`generation.md`](generation.md), which lists the CLI route, and [Ontology §12](../../docs/Ontology/creek_ontology_agent_prompt.md), which is the spec.

---

## Lifecycle

A Decision moves through the five statuses defined on `DecisionStatus`:

```
sensing → deliberating → committing → enacted → reflecting
```

| Status | Folder | Meaning |
|---|---|---|
| `sensing` | `08-Decisions/Active/` | Something feels like a decision is forming. Detector creates the draft here. |
| `deliberating` | `08-Decisions/Active/` | Options listed; criteria being weighed. |
| `committing` | `08-Decisions/Active/` | A direction has emerged; preparing to enact. |
| `enacted` | `08-Decisions/Archive/` | Decision lived out. Note moved to Archive. |
| `reflecting` | `08-Decisions/Archive/` | Looking back from a distance. Note stays in Archive. |

Statuses are advanced by `DecisionDetector.update_decision_phase(decision_id, new_phase, vault_path)`. The function rewrites the frontmatter `status` field and moves the file between `Active/` and `Archive/` automatically. Invalid statuses raise `ValueError` — there is no silent fallback.

The frontmatter follows `creek.models.Decision`: `id`, `title`, `status`, `opened`, `decided`, `frequency_context`, `wavelength_phase_at_opening`, `relevant_threads`, `relevant_praxis`, `options`, `criteria`, `outcome`, `tags`. The opening wavelength phase is captured at draft time and is **never** rewritten by later phase advances — the historical record matters more than tidying the frontmatter.

---

## Detection (DecisionDetector)

`creek/generate/decisions.py:DecisionDetector` scans classified fragments using two strategies:

1. **Keyword** — case-insensitive title match against `DECISION_KEYWORDS`:
   `should i`, `trying to decide`, `weighing options`, `not sure whether`,
   `torn between`, `considering`, `the question is`. Score base **0.7**.
2. **Pattern** — `praxis_potential = explicit` AND `confidence = exploring`
   AND the primary+secondary frequencies overlap a known pair
   (`F1+F4` agency × structure, or `F1+F5` agency × achievement).
   Score base **0.6**.

A fragment matching both strategies produces a single candidate scored **0.9**.

Each candidate captures `wavelength_phase_at_detection` and the active `frequency_context` so the eventual Decision note can preserve the felt-sense of when the question first surfaced.

---

## Drafting (`create_decision_note`)

`DecisionDetector.create_decision_note(candidate, vault_path)` writes a draft to `08-Decisions/Active/<date>-<sanitised-title>.md` with:

- `status: sensing`
- `opened: <today, LA-tz>`
- `wavelength_phase_at_opening` carried over from detection
- a body skeleton with `## Source Fragments`, `## Options`, `## Criteria`
- two `_Option 1_` / `_Option 2_` placeholders the user fills in

The file is human-edited from there. Status advances are operator-driven, not automatic.

---

## Context gathering (`DecisionContextGatherer`)

For an active Decision, `DecisionContextGatherer` aggregates **read-only** cross-vault context:

- **Related threads** — Jaccard-similar threads, by title and tag tokens.
- **Past decisions** — same-frequency or same-theme decisions in `Archive/`. Used as precedent, not advice.
- **Current wavelength** — the most recent wavelength observation, if any.
- **Relevant praxis** — praxis whose frequencies overlap the decision.
- **Frequency affinity** — top frequencies active across the related set.
- **Interventions** — `(frequency_color, phase_value)` lookups against the §12.5 Phase × Frequency map (e.g. `(orange, rising) → Pranayama`, `(green, diminishing) → Journaling`).

The aggregator returns a `DecisionContext` dataclass; rendering happens through `render_context_text(...)`, which is then passed through `apply_guardrails(...)` before being written to disk. This is where §12.4 lives.

---

## §12.4 anti-manipulation guardrails

`DecisionContextGatherer.apply_guardrails(text)` strips directive, urgency, and scarcity framings from any rendered context block. The patterns and replacements are defined in `_DIRECTIVE_REPLACEMENTS`:

| Pattern (case-insensitive) | Replacement |
|---|---|
| `you should` | `one option is to` |
| `you need to` | `one possibility is to` |
| `you must` | `one possibility is to` |
| `the best option` | `one option` |
| `the right choice` | `one possibility` |
| `it's clear that` | `one reading is that` |
| `obviously` | `from one perspective` |
| `act now` | `act when it feels right` |
| `before it's too late` | `when the moment feels right` |
| `last chance` | `a moment that is present` |
| `this is permanent` | `some considerations include` |
| `this is irreversible` | `some considerations include` |

The contract is one-way: guardrails only **soften** language. A future PR that adds new directive patterns must extend `_DIRECTIVE_REPLACEMENTS` (and a regression test) so the guardrail keeps working as the model the system reads from changes.

These guardrails are tested in `tests/test_decisions.py`; any change to `_DIRECTIVE_REPLACEMENTS` shows up there.

---

## Re-opening behaviour

The lifecycle is conventionally one-directional: a decision in `enacted` or `reflecting` lives in `Archive/`. To re-open, advance the same `decision_id` back to `committing` (or earlier) — `update_decision_phase` will move the file from `Archive/` back into `Active/` and update the `status` frontmatter field. There is no re-open prevention guard; the file move is the audit trail.

`opened` is preserved across phase changes; `decided` is set when the operator transitions to `enacted` (the model permits `None`, so a re-opened decision can leave `decided` empty until it commits again).

---

## CLI route

```bash
creek report --type decision --vault ~/Obsidian/Creek-Vault
```

Drives the detector across `01-Fragments/`, drafts notes for newly-detected candidates under `08-Decisions/Active/`, and refreshes the context block (with guardrails applied) on every Active decision. See [`generation.md`](generation.md#reports) for the shared `--type` table.

---

## See also

- [`generation.md`](generation.md) — broader generation surface, including `report --type decision`.
- [Ontology §12](../../docs/Ontology/creek_ontology_agent_prompt.md) — full spec.
- `creek/models.py:Decision`, `DecisionStatus`, `DecisionCandidate` — frontmatter schema.
