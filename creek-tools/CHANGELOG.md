# Changelog

All notable changes to `creek-tools` are documented here. Versioning is
loose during pre-1.0 development; the headings below track design-trace
IDs (`FEAT-*`, `INC-*`, `BUG-*`, …) embedded in commit messages and
inline code annotations. The original `plans/git-issues/` directory of
long-form spec files was retired in #243; use `git log --grep='<ID>'`
to locate the originating commit for any reference below.

## Unreleased

### Tooling

- **`./scripts/check-all.sh` now matches CI gate-for-gate (GAP-007).**
  Three drifts closed:
  1. **Interrogate** (docstring coverage ≥95%) is now invoked between
     the tryceratops and unit-tests gates via the new
     `scripts/lint-interrogate.sh`. CI already runs it; previously a
     local pass could fail CI on docstring coverage alone.
  2. **Bandit severity** in `scripts/security.sh` switched from
     "all severities" to `-ll` (medium-or-above), matching CI and the
     CLAUDE.md §6.1 policy. The local gate is now no stricter than CI;
     low-severity findings are intentionally not gated. To audit them
     anyway, run `bandit -r creek/` directly.
  3. **`state-budget.sh` removed from `check-all.sh`.** The script
     silently no-ops when `CREEK_VAULT` is unset (its default state
     for most developers), so it had been a fake-pass entry on the
     local gate while CI never invoked it at all. It now ships as a
     standalone opt-in audit; run `./scripts/state-budget.sh --vault
     <path>` against a real vault when you want to check report-size
     budgets.

### Breaking changes

- **`creek skills` is now a typer subapp** (FEAT-019). Replace existing
  invocations:
  - `creek skills --generate --vault <vault>`
    → `creek skills generate --generate --vault <vault>`
  - The new sibling `creek skills sync --vault <vault>` re-deploys the
    canonical schema-skill tree from `creek-tools/creek/templates/skills/`
    into `<vault>/00-Creek-Meta/Skills/`. Pass `--force` to overwrite
    locally-modified files.
- **`creek init --vault <path>` is required** (FEAT-019). The previous
  default of "current directory" is gone. `creek init` also refuses
  paths inside a git repository by default; pass `--allow-in-repo` to
  override.

### Added

- `creek init --refresh` re-copies canonical templates (ontology spec,
  AGENTS.md, schema-skill tree, folder scaffold) into an existing vault
  without touching user data or `creek_config.yaml`.
- Canonical templates live under `creek-tools/creek/templates/{vault,
  skills,AGENTS.md}`. The ontology spec lives at repo-level
  `docs/Ontology/creek_ontology_agent_prompt.md`.
- **Freshness-aware embeddings cache** (INC-006): `creek link --method
  embeddings` now persists vectors to
  `<vault>/00-Creek-Meta/embeddings.parquet` with `(fragment_id,
  content_hash, model_name, embedding, computed_at)`. Re-runs reuse rows
  whose `content_hash` matches the current fragment text and recompute
  the rest, so a 1k-fragment vault re-links in seconds. Switching
  `embeddings.model` invalidates the cache automatically; `--rebuild`
  still forces a full recompute from scratch. Existing `embeddings.npz`
  files are ignored and can be deleted.

### Removed

- The repo no longer ships empty placeholder directories for
  `01-Fragments/` … `10-Liminal/` or `00-Creek-Meta/`. User vault
  content lives outside this repository (default suggested location:
  `~/Obsidian/Creek-Vault/`, scaffolded by `creek init`).
