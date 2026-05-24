# ADR: Jig workflows live under `crawdad/workflows/`, not the vault

**Date:** 2026-05-24
**Status:** Accepted
**Issue:** [#268 — ADAPT-003 Jig-style workflow DSL](https://github.com/Geoffe-Ga/creek-vault/issues/268)
**Phase:** 1 (parallelizable skeleton)

## Context

ADAPT-003 introduces a YAML workflow DSL for CrawDad's composite
commands. The implementation notes on #268 flagged the placement
decision explicitly: workflows could live (a) under the bot's own
config at `crawdad/workflows/`, or (b) inside the user's vault at
`<vault>/00-Creek-Meta/Workflows/`. The example file path in the
recovered ADAPT-003 plan implies (a); vault-located workflows would
otherwise compose more naturally with the `creek init` scaffolding.

## Decision

Workflows ship as YAML files under **`crawdad/workflows/`** in the
crawdad subproject, alongside the bot's own source. The bundled
location is the canonical default; the workflow registry takes a
directory at construction time so a future iteration can layer a
user-supplied directory on top.

## Rationale

1. **CLI invocation cannot assume a vault is wired.** `crawdad
   workflows list` and `crawdad workflows run <name>` must work
   immediately after `pip install`, before any `crawdad.yaml` exists
   and before the bot has been pointed at a vault. Sourcing the
   registry from the vault would couple Phase 1 to a wiring step that
   belongs in the runtime, not in the CLI surface.
2. **Phase 1 is parallelizable scaffolding.** The dry-run walker
   prints each step's `tool:` identifier; it does not yet touch the
   MCP surface, the vault, or any user state. Bundling the three
   reference workflows with the code keeps the test suite hermetic
   and the smoke test (`uv run crawdad workflows list`) zero-config.
3. **Version control naturally lives with the bot.** The acceptance
   criteria require "workflow files are checked into the repo;
   modifications go through the normal review/version-control flow."
   The bundled location satisfies that; the vault is intentionally
   not checked in (FEAT-019).
4. **Future vault layering remains open.** `WorkflowRegistry` takes
   a `Path` at construction time, so the runtime can later merge a
   vault-side directory (e.g.  `<vault>/00-Creek-Meta/Workflows/`)
   on top of the bundled defaults without changing the file format.
   Phase 2 or a later FEAT can introduce that layering once the real
   walker exists; revisit this ADR then if requirements change.

## Consequences

- The three reference workflows ship in `crawdad/workflows/` and are
  imported by the registry via `bundled_workflows_dir()`.
- Tests can assert against the bundled set directly without a tmp
  vault fixture.
- A future `--workflows-dir <path>` CLI flag (or vault-merging logic)
  is a non-breaking additive change.
- Operators who edit the bundled YAML files are editing the bot's
  trusted-config surface — the same trust boundary that already
  applies to `crawdad.yaml`.
