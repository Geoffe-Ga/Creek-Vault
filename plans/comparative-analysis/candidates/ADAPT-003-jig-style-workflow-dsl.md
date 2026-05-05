# ADAPT-003: Jig-Style Workflow DSL for CrawDad's Composite Commands

**Verdict:** ADAPT
**Source system:** AlfredOS / Jig
**Affects:** CrawDad agent layer (and creek-tools as the underlying tool surface)
**Roadmap target:** v0.2 (after MCP surface lands in v1)
**Estimated complexity:** M
**Conflicts with non-negotiables?** none

## What it is

Jig is AlfredOS's "markdown for operations" — a DSL for declaring business workflows as composable steps that compile into MCP servers. From the [AlfredOS 1.0 announcement](https://lumberjack.so/alfredos-10-is-here/): "Jig is basically Markdown for operations — a human-readable, MCP-native language that describes any business workflow, its steps, and the required tools or resources." Skill-based: workflows decompose into deterministic steps; each skill has narrow scope, specific tools, clear guidance. The LLM is a router-of-skills, not a free-form executor.

The exact syntax is unverified in the public materials; this candidate borrows the *idea*, not the implementation.

## Why it's interesting

The user has named three CrawDad interaction modes; one is **workflow-driven commands** — composite operations like:

- "Draft my next Substack on phase transitions"
- "Generate an APTITUDE module exercise for the Withdrawal phase"
- "Give me a Wavelength check-in for the last week"
- "Show me what's surfacing in my Compost folder"

Each is a multi-step pipeline: query the compiled layer, filter by phase, mine for ideas, compose, file the output. Without a DSL, these become ad-hoc prompt strings — version-uncontrolled, hard to test, hard to compose. With a DSL, they become authored artifacts: a `.jig` file (or whatever format) that names the workflow, declares its steps, names the tools each step uses, and is checked into git.

This is the *workflow-driven* version of CrawDad's tool surface. The skill-style commands (ADOPT-008, MCP tools dispatched by the Haiku router) are the atomic building blocks; the workflow DSL composes them into the named composite operations the user actually wants.

## Fit with Creek Vault and/or CrawDad

CrawDad-primary, with creek-tools as the tool registry the workflows compose over.

A sketched Creek-flavored workflow file:

```yaml
# crawdad/workflows/substack-draft-phase-transitions.jig.yaml
name: substack-draft-phase-transitions
description: Draft a Substack post about phase transitions, scoped to recent material.
trigger: "/draft phase-transitions"
phase_aware: true
privacy_tier_floor: open
steps:
  - id: read-state
    tool: creek.state.read
    args: { period: weekly }
  - id: mine
    tool: creek.mine
    args: { strategy: thread-terminus, phase: "{{state.wavelength.current_phase}}" }
  - id: select
    tool: creek.mine.select
    args: { index: 0 }
  - id: draft
    tool: creek.draft
    args:
      idea_id: "{{select.idea_id}}"
      register: confessional
      skill_stack: ["frequencies/F7", "phases/{{state.wavelength.current_phase}}", "registers/confessional"]
  - id: respond
    tool: crawdad.respond
    args:
      message: "Drafted '{{draft.title}}' to {{draft.path}}. Skill stack: {{draft.skill_stack | join: ', '}}."
```

The workflow declares its phase-awareness, specifies a privacy floor, names the tools by their MCP identifiers, and uses templated outputs of earlier steps as inputs to later steps. The dispatcher just walks the steps; the LLM doesn't improvise. Voice fidelity comes from the `creek.draft` step's skill stack, which is itself an authored artifact.

## Translation if adapted

Three Creek-specific adaptations:

1. **Wavelength-aware as a first-class workflow attribute.** A workflow declares whether it's phase-sensitive; the dispatcher refuses to run a `phase_aware: true` workflow if the current phase is one the workflow shouldn't fire in (e.g., don't auto-run `substack-draft-*` workflows during Bottoming Out).
2. **Privacy tier floor as a first-class attribute.** Every workflow declares the minimum tier it can safely operate at; the dispatcher refuses to run if the user attempts to invoke it on intimate content without explicit override.
3. **Don't invent syntax.** YAML or markdown-frontmatter is fine. Don't write a parser for a custom grammar; that's overbuilt for personal use. Pick a format the user can write by hand.

The skill-based decomposition instinct is correct: each step is a single MCP tool call with deterministic inputs/outputs. The LLM is invoked *inside* a step (e.g., the `creek.draft` step calls an LLM with a skill stack), but the workflow itself is a deterministic walk over named steps.

## Dependencies

- Depends on: ADAPT-004 (MCP server — workflows compose over MCP tools).
- Pairs with: ADOPT-008 (Haiku router can dispatch a workflow as a single intent: `{ type: "run-workflow", id: "substack-draft-phase-transitions" }`).

## Acceptance criteria

- A workflow format is documented (YAML frontmatter + step list, or equivalent).
- A `crawdad workflows list` and `crawdad workflows run <id>` interface exists.
- Each step in a workflow corresponds to exactly one MCP tool call.
- Workflows declare `phase_aware` and `privacy_tier_floor` as first-class attributes.
- The dispatcher refuses to run a workflow when its declared constraints aren't satisfied.
- At least three reference workflows ship in v0.2: a Substack draft pipeline, a Wavelength check-in, and a Compost surfacing.
- Workflow files are checked into the repo; modifications go through the normal review/version-control flow.
