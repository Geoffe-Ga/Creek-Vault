# FEAT-019: Vault sovereignty — separate user vault from repository

**Severity:** High (v1.0 — privacy + sovereignty non-negotiable)
**Category:** FEAT
**Estimated LOC:** ~500 (mostly deletions + a templates directory + an extended `creek init`)
**Estimated complexity:** M
**Source candidate:** User-stated requirement after PR #199 / #202 merged. "I don't want to check my personal journal fragments into github. And the folders look super weird sitting there empty." The fourth non-negotiable in the comparative analysis (privacy/sovereignty) is currently weaker than it should be: vault folder structure is committed to the repo as empty placeholders, which means (a) the repo encodes a vault topology that the user might not want, (b) personal data leaks risk if anyone ever pushes a fragment file (the `.gitignore` whitelist is fail-open by default of the structural rule), (c) it looks weird.
**Dependencies:** none (independent of every other FEAT). Should land *before* FEAT-001 / FEAT-002 if those haven't merged yet, so their "vault root" / "00-Creek-Meta/Skills/" targets are unambiguous.
**Parallelizable with peers:** yes (with FEAT-017, FEAT-018)
**Wave:** Wave 1 prerequisite (sequence before FEAT-001)

## Goal

Separate the repository (toolchain + canonical reference material) from the user's vault (personal content, operational state). The repo carries `creek-tools/`, the ontology spec, canonical templates, schema-skill source files, and planning docs. The user's vault lives wherever they want it (`~/Obsidian/Creek-Vault/` by default) and is scaffolded by `creek init --vault <path>`. The repo has no empty placeholder directories; the user's vault never enters version control.

## Files to touch

### Removals from the repo
- Delete `01-Fragments/` (with all empty subdirs).
- Delete `02-Threads/` (with all empty subdirs).
- Delete `03-Eddies/`.
- Delete `04-Praxis/` (with all empty subdirs).
- Delete `05-Wavelength/` (with all empty subdirs).
- Delete `06-Frequencies/` (with all empty subdirs).
- Delete `07-Voice/` (with all empty subdirs).
- Delete `08-Decisions/` (with all empty subdirs).
- Delete `09-Reference/` (with all empty subdirs).
- Delete `10-Liminal/` (with all empty subdirs).

### Repository additions
- `creek-tools/creek/templates/vault/` (new directory) — canonical, version-controlled template of the vault folder structure. Empty `.gitkeep`-style markers describe the intended topology; the user's actual content never lives here.
- `creek-tools/creek/templates/AGENTS.md` (new) — canonical template of the operational AGENTS.md that FEAT-001 creates; deployed to the user's vault by `creek init`.
- `creek-tools/creek/templates/skills/` (new) — canonical source of truth for the schema-skill tree (FEAT-001/002 wrote into `00-Creek-Meta/Skills/`; that location is now per-vault and gets seeded from this canonical directory).

### Repository preservation
- `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` — canonical spec, **stays in the repo**. The user's vault gets a symlink or copy as part of `creek init`.
- `plans/`, `creek-tools/`, `CLAUDE.md`, `README.md` — unchanged.

### Removals from .gitignore
- Strip the whitelist entries for `01-Fragments/` through `10-Liminal/` since the directories no longer exist in the repo at all.
- Strip the `creek-skills/` whitelist (also per-vault, generated content).
- Keep the `**/git-issues/` rule as-is.

### CLI extensions
- `creek-tools/creek/cli.py:340-379` (`def init`) — extend the existing `creek init` to:
  - Take `--vault <path>` (required; no default to current directory because that's how personal data lands in repos by accident).
  - Refuse if `<path>` is inside a git repository unless `--allow-in-repo` is passed (with a warning). This protects against the failure mode that motivated this FEAT.
  - Scaffold the vault folder structure from `creek-tools/creek/templates/vault/`.
  - Copy the canonical ontology spec into `<vault>/00-Creek-Meta/Ontology/` (or symlink — see Pre-decided choices).
  - Copy the canonical AGENTS.md into `<vault>/AGENTS.md` (the FEAT-001 target).
  - Copy the canonical schema-skill tree into `<vault>/00-Creek-Meta/Skills/`.
  - Create a minimal `<vault>/00-Creek-Meta/creek_config.yaml` (similar to today's behaviour, possibly already implemented).
- `creek-tools/creek/cli.py` — add `creek skills sync` command that re-deploys the canonical schema-skill tree from `creek-tools/creek/templates/skills/` into `<vault>/00-Creek-Meta/Skills/` (so the user can pull upstream skill updates after they upgrade `creek-tools`).

### Documentation
- `README.md` (repo root) — clarify: "this repo is the *tool* and the canonical material. Your vault lives elsewhere (default: `~/Obsidian/Creek-Vault/`). Run `creek init --vault <path>` to scaffold a vault."
- `creek-tools/README.md` — update the Quickstart to start with `creek init --vault ~/Obsidian/Creek-Vault` before any other command.
- `creek-tools/CLAUDE.md` — add a short "repo topology" section near the top: repo = toolchain + canonical; vault = user data.
- `creek-tools/docs/getting-started.md` — explicit step: "1. Pick a vault location (NOT inside this repo). 2. `creek init --vault <that path>`. 3. ..."

## Pre-decided choices

- **Repo never contains user vault content.** The deletion of `01-Fragments/` … `10-Liminal/` is permanent. The repo carries the template under `creek-tools/creek/templates/vault/`; that template is what `creek init` materializes into the user's chosen path.
- **`creek init --vault <path>` is required (no default).** Defaulting to current directory is how personal data ends up in repos. The required flag forces the user to make an explicit choice.
- **Refuse to init inside a git repository by default.** If `<path>` resolves under a `.git/` parent, `creek init` errors with: `<path> is inside a git repository. Personal vault data should not be version-controlled. Pass --allow-in-repo to override.` This is a sovereignty guard, not a hard block — power users can override.
- **Canonical ontology spec is copied, not symlinked.** Symlinks don't survive cross-platform; copies are robust. `creek init` documents the copy and explains how to re-sync (`creek init --refresh` re-copies canonical material without touching user content).
- **Schema-skill tree (FEAT-001/002) is canonical-source in the repo, deployed to vault.** The canonical files live at `creek-tools/creek/templates/skills/*.SKILL.md`. `creek init` copies them to `<vault>/00-Creek-Meta/Skills/`. `creek skills sync` re-deploys to pick up upstream changes. Users can edit their per-vault copies; a sync overwrites, with a confirm prompt if local changes are detected.
- **AGENTS.md handling:** the canonical template lives at `creek-tools/creek/templates/AGENTS.md`. `creek init` copies to `<vault>/AGENTS.md`. The user can edit the deployed copy.
- **Existing local vaults are not broken.** If someone has already started using the in-repo folders (which the gitignore made effectively impossible, but in principle), document the migration: `creek init --vault <new-path> --migrate-from <old-repo-root>` (deferred — not in this FEAT).
- **The user's intended vault path is documented as a personal choice.** Suggested default: `~/Obsidian/Creek-Vault/` (the existing repo name). The user can pick anything.
- **`00-Creek-Meta/` no longer exists at the repo root.** The Ontology spec moves to `docs/Ontology/creek_ontology_agent_prompt.md` (repo-level) and gets copied into `<vault>/00-Creek-Meta/Ontology/` during init.

## Test plan

- Unit: `creek init --vault /tmp/test-vault` creates the documented folder structure.
- Unit: `creek init --vault <path-inside-this-repo>` refuses with the documented error message.
- Unit: `creek init --vault <path-inside-this-repo> --allow-in-repo` proceeds (with a warning) for power users.
- Unit: the canonical schema-skill tree at `creek-tools/creek/templates/skills/` is non-empty and lints clean (every `*.SKILL.md` parses as valid frontmatter + body).
- Unit: `creek skills sync --vault <path>` re-copies the canonical tree; with `--check-modified` it refuses if local changes exist, otherwise prompts.
- Regression: existing creek-tools commands (`process`, `classify`, `link`, etc.) work against a vault scaffolded by the new `creek init`.
- Regression: the repo's CI suite passes without the now-deleted top-level vault folders (no test depends on `01-Fragments/` etc. being on disk in the repo).
- Documentation: every existing doc that referenced `01-Fragments/` (etc.) as paths in the repo is updated to clarify those paths exist in the user's vault.

## Acceptance criteria

- The repo no longer contains `01-Fragments/`, `02-Threads/`, `03-Eddies/`, `04-Praxis/`, `05-Wavelength/`, `06-Frequencies/`, `07-Voice/`, `08-Decisions/`, `09-Reference/`, `10-Liminal/` (verified by directory listing after merge).
- `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` either stays at the repo level (preferred) or moves to `docs/Ontology/`; a `creek init` deployment to a fresh vault produces a `<vault>/00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` matching the canonical content byte-for-byte.
- `creek-tools/creek/templates/vault/` exists and is the canonical scaffolding source.
- `creek init --vault <path>` is required (errors helpfully without it); refuses inside-a-repo paths by default; succeeds with `--allow-in-repo`.
- `creek skills sync` exists and re-deploys canonical skills to a target vault.
- `README.md` (repo root) makes the topology explicit before the user runs any command.
- The CI suite passes without the deleted folders.
- ≥90% branch coverage on the changed CLI paths.
- `.gitignore` is cleaned up — no orphan whitelist entries for paths the repo no longer contains.

## References

- INTEGRATION-PLAN.md distinctiveness watchlist: "Privacy / sovereignty by construction" (priority 4 of the four non-negotiables).
- FEAT-001 (the AGENTS.md target — this FEAT clarifies where "vault root" resolves to).
- FEAT-002 (the schema-skill tree — this FEAT establishes the canonical-vs-deployed split).
- FEAT-013 (CrawDad's `crawdad.yaml` config will name the vault path; once FEAT-019 lands, that path defaults to `~/Obsidian/Creek-Vault/` rather than the repo root).
- Existing `creek init` at `creek-tools/creek/cli.py:340-379` (the extension target).
- `.gitignore` whitelist entries for `01-Fragments/` etc. (the cleanup target).
