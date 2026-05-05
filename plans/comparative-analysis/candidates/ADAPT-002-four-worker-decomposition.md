# ADAPT-002: Four-Worker Decomposition (Curator / Janitor / Distiller / Surveyor)

**Verdict:** ADAPT
**Source system:** AlfredOS / `alfred-vault`
**Affects:** Creek Vault data layer (vocabulary refactor) + CrawDad agent layer (background cadence)
**Roadmap target:** v0.2 (vocabulary first; background-worker scheduling later)
**Estimated complexity:** S (vocabulary refactor) + M (scheduled cadence)
**Conflicts with non-negotiables?** liminal — must be careful with Distiller (see "Translation")

## What it is

`alfred-vault`'s [README](https://github.com/ssdavidai/alfred) decomposes Obsidian-vault assistant work into four named background workers:

- **Curator** — processes inbox files into structured records.
- **Janitor** — fixes broken links, orphaned files.
- **Distiller** — surfaces implicit assumptions and contradictions.
- **Surveyor** — semantic embeddings, relationship discovery.

Each runs continuously on a cadence; together they tend the vault.

## Why it's interesting

Creek currently has *all* of this functionality, but it's spread across several CLI commands and emergence report types with no unifying name. The user thinks of these as facets of the same operation; the vocabulary refactor makes that mental model explicit. AlfredOS gave four good names; Creek can adopt them.

Mapping:

| Worker | Creek today |
|---|---|
| Curator | `creek ingest` + `creek redact` + `creek classify --method rules` + `creek classify --method llm` (the full ingest/classify pipeline) |
| Janitor | `creek clean orphans` + `creek clean broken-links` + `creek clean stale-reviews` + `creek clean duplicates` |
| Distiller | `creek report --type paradox` + `creek report --type unnamed` + `creek report --type tags` (the surfacing functions) |
| Surveyor | `creek link --method embeddings` + `creek link --method temporal` + `creek link --method eddies` + (future) `creek link --method leiden` |

The renaming clarifies that "what the system does continuously" is one thing with four roles, not seven separate report types.

## Fit with Creek Vault and/or CrawDad

Two phases of adoption:

**Phase 1 (v0.2): vocabulary refactor.** Rename the existing CLI subcommand families:

- `creek curate` (alias-or-replacement for `creek ingest` + `creek classify`)
- `creek janitor` (alias for `creek clean *`)
- `creek distill` (consolidates `paradox`, `unnamed`, `tags` reports — overlaps with `creek lint`)
- `creek survey` (alias for `creek link *` family)

The existing commands stay; the new names point at the same code paths. `creek lint` (ADOPT-002) becomes a meta-command that runs Distiller + parts of Janitor.

**Phase 2 (v0.3+): scheduled background cadence.** Each worker runs on a schedule (daily Curator on inbox; weekly Janitor; weekly Distiller; nightly Surveyor for incremental re-linking). This requires a workflow runner (see DEFER-002 for Temporal). For personal-use scale, a cron + `creek` CLI is sufficient; durable-workflow infrastructure can wait.

## Translation if adapted

The critical adaptation is on **Distiller**: AlfredOS describes it as surfacing "implicit assumptions and contradictions" with no clear stance on what to do with them. Creek's stance is explicit: contradictions go to `10-Liminal/Paradoxes/` and are *preserved*, not resolved. The Creek-flavored Distiller:

- Surfaces paradoxes → routes to `10-Liminal/Paradoxes/` (existing behavior).
- Surfaces unnamed clusters → routes to `10-Liminal/Unnamed/` for digest.
- Surfaces tag clusters → routes to Tag Garden for review.
- Surfaces synchronicities → routes to `05-Wavelength/Synchronicities/`.
- Never auto-resolves anything. Distiller's output is always for human review.

The Curator naming is fine; the Janitor naming is fine; the Surveyor naming is fine. Only Distiller needs the explicit "preservation, not resolution" guard rail in its docstring and tests.

## Dependencies

- Pairs with: ADOPT-002 (lint subsumes most of what Distiller does), ADAPT-001 (Leiden lives in Surveyor).
- Pairs with the agent layer: when CrawDad asks the user "what should we work on?" the answer comes from Distiller's output; this is the Discord-level translation of the vocabulary.

## Acceptance criteria

- `creek curate`, `creek janitor`, `creek distill`, and `creek survey` commands exist (as aliases or first-class commands; either is fine).
- The four-worker vocabulary is documented in the README and in the ontology spec or a sibling doc.
- Distiller's output explicitly routes paradoxes to `10-Liminal/Paradoxes/`, never to a "to-fix" queue. Tested by regression.
- A scheduled-cadence story is documented (cron is fine for v0.2; durable workflow is DEFER-002).
- The vocabulary is also surfaced in CrawDad's intent schema — `intents` types like `curate.ingest`, `janitor.clean`, `distill.surface`, `survey.link` make the agent layer legible.
