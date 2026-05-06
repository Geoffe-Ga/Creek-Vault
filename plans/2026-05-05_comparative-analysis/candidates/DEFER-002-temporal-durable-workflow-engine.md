# DEFER-002: Temporal as Durable-Workflow Engine for the Kinetic Layer

**Verdict:** DEFER
**Source system:** AlfredOS / `alfred-vault`
**Affects:** Both — Creek Vault background workers + CrawDad's long-horizon agent loops
**Roadmap target:** unscheduled (revisit at v1.3+)
**Estimated complexity:** L
**Conflicts with non-negotiables?** none

## What it is

`alfred-vault`'s "Kinetic" layer ([README](https://github.com/ssdavidai/alfred)) uses Temporal for durable workflow execution and cron scheduling. The architectural argument: long-horizon agent tasks need replay, retry, and durability — ad-hoc Python scripts don't survive contact with reality (a laptop closes, a network blip kills the run, an LLM rate-limit causes a 30-second backoff that cron can't handle).

## Why it's interesting

The argument is sound. Creek's `creek classify --method llm` already implements *partial* durable execution (writes each fragment to disk the moment the LLM call returns, so a crash mid-run is survivable; per `docs/classification.md`). But:

- The four-worker decomposition (ADAPT-002) introduces multiple cadences (Curator continuously, Janitor weekly, Distiller weekly, Surveyor nightly). Coordinating them with cron + flags becomes brittle.
- Jig-style workflows (ADAPT-003) are multi-step pipelines where an intermediate failure should resume from the failed step, not restart from scratch.
- CrawDad's longer drafting workflows ("draft a Substack" can take 5–10 minutes end-to-end including idea mining, skill stack assembly, LLM generation) need something more robust than a Discord bot's in-memory state.

For all of these, Temporal (or a lighter durable-workflow library) would help.

## Why DEFER, not ADOPT

For personal use, on a single VPS, with a single user driving a few workflows a day, **cron + a write-each-result-to-disk discipline is enough**. The existing classification engine demonstrates this works. The cost of Temporal (running a Temporal server, learning the SDK, durably storing state, debugging Temporal-flavored failures) is real and only pays off at workflow-orchestration density that personal use won't reach.

The right time to revisit:
- When the user is running >10 distinct workflows on regular cadences and the cron file is unreadable.
- When a workflow failure mode "the laptop closed mid-draft" actually happens and matters.
- When CrawDad's session continuity across hours-long pauses requires durable state that ad-hoc storage can't provide.

For now, the integration plan stays with cron + per-step-write-to-disk. The architectural option is documented; the deployment isn't.

## Dependencies

- Adjacent to: ADAPT-002 (four-worker decomposition documents the cadence question that Temporal would solve), ADAPT-003 (workflow DSL — Temporal would be the runtime, but a simple step-walker is sufficient for v1.1).

## Acceptance criteria

N/A — deferred. Two trigger conditions for revisit are documented above.
