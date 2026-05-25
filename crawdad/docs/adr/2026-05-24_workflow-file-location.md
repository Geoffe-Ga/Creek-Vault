# ADR: Workflow file location

- **Date**: 2026-05-24
- **Issue**: #268 (ADAPT-003 — Jig-style workflow DSL for CrawDad)
- **Status**: Accepted

## Context

ADAPT-003 introduces an authored workflow DSL for CrawDad's composite
commands. The acceptance criteria require:

1. At least three reference workflows ship with the project (Substack
   draft, Wavelength check-in, Compost surfacing).
2. Workflow files are checked into version control; modifications go
   through normal review/PR flow.
3. Users can author their own workflows.

The issue body's implementation notes flagged the location as an open
question, with two candidate homes:

- **`crawdad/crawdad/workflows/` inside the package** — matches the
  example in the original ADAPT-003 plan, ships with the install, but
  forces users to edit installed package files to author their own
  workflow.
- **`<vault>/00-Creek-Meta/Workflows/` inside the vault** — composes
  with the rest of the vault scaffolding (`State/`, `Inbound/`,
  `Paradoxes/`), keeps user content out of the package, but does NOT
  by itself satisfy the "checked into the repo" AC.

## Decision

**Two-source registry.** The :class:`crawdad.workflows.WorkflowRegistry`
scans:

1. `crawdad/crawdad/builtin_workflows/` — the three reference workflows
   ship here, are version-controlled in the monorepo, and load on
   every install. This satisfies AC 1 and AC 2.
2. `<vault>/00-Creek-Meta/Workflows/` — user-authored workflows land
   here, alongside the existing Creek-Meta substructure. The directory
   is created lazily on first use. This satisfies AC 3.

User-authored files with the same `name` as a built-in **win** — that
gives the user an override path without having to edit the package.

## Consequences

- **Built-in reference workflows are immutable per install.** Editing
  the bundled YAML files is technically possible but is not the
  authoring path; bumping a reference workflow requires a PR.
- **No cross-vault sharing of user workflows.** Two vaults on the same
  machine have independent `Workflows/` directories. Acceptable for
  the personal-use deployment target.
- **The package gains a new data directory.** Setuptools `packages.find`
  already includes the `crawdad` package; the YAML files ship as
  package data automatically because they live under the package
  source tree.
- **Future migration path.** If we ever want a shared / cross-vault
  workflow registry, the registry's two-source design extends cleanly
  to a third source (e.g. `~/.config/crawdad/workflows/`) without
  changing the call sites.

## Rejected alternatives

- **Vault-only.** Would require shipping reference workflows as part
  of `creek init`-style scaffolding inside `creek-tools`. That couples
  CrawDad's behaviour to `creek-tools` deployment timing and forces
  re-scaffold for existing vaults — more friction than the two-source
  registry resolves.
- **Package-only.** Forces users to either fork the package or write
  workflows into the install path, both of which break with package
  upgrades. Rejected.

## References

- Issue #268 (this ADR's tracker).
- ADAPT-003 plan, recovered from commit `3363e52`
  (`plans/2026-05-05_comparative-analysis/candidates/ADAPT-003-jig-style-workflow-dsl.md`).
- `crawdad/crawdad/workflows.py` — implementation.
- `crawdad/crawdad/builtin_workflows/*.workflow.yaml` — reference workflows.
