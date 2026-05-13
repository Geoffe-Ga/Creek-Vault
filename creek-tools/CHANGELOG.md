# Changelog

All notable changes to `creek-tools` are documented here. Versioning is
loose during pre-1.0 development; the headings below track FEAT IDs from
`plans/git-issues/`.

## Unreleased

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

### Removed

- The repo no longer ships empty placeholder directories for
  `01-Fragments/` … `10-Liminal/` or `00-Creek-Meta/`. User vault
  content lives outside this repository (default suggested location:
  `~/Obsidian/Creek-Vault/`, scaffolded by `creek init`).
