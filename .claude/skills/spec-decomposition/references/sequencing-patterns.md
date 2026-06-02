# Sequencing Patterns

Tracer-code sequencing inside an epic. The rule is invariant across patterns:

> The skeleton issue lands first. Every subsequent issue replaces one
> stub with real logic without breaking any other surface.

Below are four reference sequences. Pick the one closest to the epic's
shape and adapt the issue titles.

## Pattern 1: HTTP API Epic

Use when the epic exposes one or more REST/GraphQL endpoints.

| # | Phase | Issue title (template) | What lands |
|---|-------|-----------------------|------------|
| 1 | Skeleton | `feat(<scope>): Wire <endpoints> with stubbed responses` | Routes, request/response models, smoke tests, all endpoints return valid stub data |
| 2 | Core | `feat(<scope>): Implement <primary endpoint> end-to-end` | The P0 endpoint hits real logic + storage |
| 3 | Core | `feat(<scope>): Implement <secondary endpoint> end-to-end` | Repeat for the next-most-important endpoint |
| 4 | Edges | `feat(<scope>): Add request validation and error responses` | Pydantic/zod validation, RFC 7807 error bodies, negative-path tests |
| 5 | Edges | `feat(<scope>): Handle <specific edge case>` | E.g. pagination, idempotency keys, rate limits |
| 6 | Polish | `chore(<scope>): Add structured logging and metrics` | Observability, no new behavior |

**Stop condition:** if the epic has more than ~6 issues, split it into
two epics by endpoint group.

## Pattern 2: CLI Tool Epic

Use when the epic adds a CLI command or subcommand.

| # | Phase | Issue title (template) | What lands |
|---|-------|-----------------------|------------|
| 1 | Skeleton | `feat(<scope>): Wire <command> arg parser with stub output` | `cli <command> --help` works, command returns 0 with a "not yet implemented" line |
| 2 | Core | `feat(<scope>): Implement <command> primary flow` | Happy-path execution works against real input |
| 3 | Edges | `feat(<scope>): Handle malformed input and partial failures` | Bad args, missing files, partial work resumability |
| 4 | Polish | `feat(<scope>): Add --dry-run, progress output, and docs` | UX niceties, README section |

## Pattern 3: UI Feature Epic

Use when the epic adds a user-facing screen or component flow.

| # | Phase | Issue title (template) | What lands |
|---|-------|-----------------------|------------|
| 1 | Skeleton | `feat(<scope>): Add <page/component> with static placeholder data` | Route exists, component renders, navigation works, no real data |
| 2 | Core | `feat(<scope>): Wire <page> to <API endpoint>` | Real data fetched and rendered |
| 3 | Core | `feat(<scope>): Implement <primary user action>` | Form submission / mutation works end-to-end |
| 4 | Edges | `feat(<scope>): Add loading, empty, and error states` | All four states (loading/empty/error/success) covered |
| 5 | Polish | `feat(<scope>): Accessibility audit and dark-mode pass` | WCAG 2.1 AA, design tokens applied |

## Pattern 4: Data Pipeline Epic

Use when the epic builds a batch or streaming pipeline.

| # | Phase | Issue title (template) | What lands |
|---|-------|-----------------------|------------|
| 1 | Skeleton | `feat(<scope>): Wire pipeline stages with pass-through stubs` | Pipeline runs end-to-end on a tiny fixture, each stage is a no-op identity function |
| 2 | Core | `feat(<scope>): Implement <stage 1> transformation` | First real stage, fixture grows |
| 3 | Core | `feat(<scope>): Implement <stage 2> transformation` | Next stage |
| 4 | Edges | `feat(<scope>): Handle malformed rows and partial-batch failures` | Dead-letter queue, retry semantics |
| 5 | Polish | `feat(<scope>): Add backpressure and throughput metrics` | Observability |

## Anti-Patterns to Avoid

**"Refactor first" issue.** Do not file a leading issue whose sole goal
is to refactor. Refactors land alongside the feature they enable. A
standalone refactor issue breaks the tracer-code invariant because it
delivers no observable system value.

**"All the tests" issue.** Do not file a leading issue that adds tests
for code that doesn't exist yet. Tests are written inside each feature
issue (TDD per stay-green). A "write tests" issue is a code smell.

**"Wire everything" issue.** The skeleton issue wires only this epic's
surfaces. Wiring all epics in one issue defeats the per-epic sequencing
and produces a giant unreviewable PR.

**"Polish before core" sequencing.** Logging, metrics, and docs come
last. Filing them before the core feature issue means an agent could
pick up Polish before the thing it's polishing exists.

## Cross-Epic Sequencing

Within a SPEC, epics also have an order. Capture it in each epic's
"Sequencing Notes" section:

- **Blocks:** This epic must complete before epic X can start.
- **Unblocks:** This epic unblocks epic Y.
- **Parallel-safe:** This epic can run alongside epic Z.

If two epics share files heavily, they probably should be one epic — or
one should be reframed as a thin slice that doesn't collide.
