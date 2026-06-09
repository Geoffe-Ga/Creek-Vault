# Swappable LLM providers — issue set

Issue-ready bodies for the swappable-provider work (Anthropic / OpenAI /
Gemini, keys from env). See the design in
[`../2026-06-09_PLAN.md`](../2026-06-09_PLAN.md).

Each file is a self-contained 6-component prompt (role, goal, context,
deliverables, constraints, acceptance) that an agent or engineer can
execute end-to-end. They are **sequenced as tracer code** — every step
ships green; the skeleton lands before new providers.

| # | File | Package | Risk |
|---|---|---|---|
| — | `EPIC-swappable-llm-providers.md` | both | — |
| 1 | `ISSUE-01-normalize-completion-and-protocol.md` | creek-tools | low |
| 2 | `ISSUE-02-provider-factory-and-routing.md` | creek-tools | low–med |
| 3 | `ISSUE-03-generalise-cloud-consent.md` | creek-tools | low |
| 4 | `ISSUE-04-openai-provider-sync.md` | creek-tools | med |
| 5 | `ISSUE-05-gemini-provider-sync.md` | creek-tools | med |
| 6 | `ISSUE-06-crawdad-async-abstraction.md` | crawdad | med |
| 7 | `ISSUE-07-crawdad-openai-gemini-and-config.md` | crawdad | med–high |
| 8 | `ISSUE-08-docs-and-adr.md` | both | low |

Steps 1–5 (creek-tools) and step 6 (CrawDad refactor) are independent and
can proceed in parallel. Step 7 depends on 6; step 8 depends on 5 + 7.

To file these as GitHub issues later, use the repo's file-based pattern
(`gh issue create --body-file <file>` / the GitHub MCP `issue_write`),
creating the epic first and linking children to it. (The repo's transient
`git-issues/` staging dir is gitignored; these committed copies under
`issues/` are the durable record.)
