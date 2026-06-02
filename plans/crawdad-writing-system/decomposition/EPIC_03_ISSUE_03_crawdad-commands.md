## Role

You are a senior Python engineer working in the `crawdad/` sibling repo, fluent in its
`discord.py` client, the FEAT-015 two-LLM loop, the async MCP stdio client, and the FEAT-016 slash
commands.

## Goal

Route CrawDad's `/crawdad draft <topic>` and a new `/crawdad ask <question>` (research medium) to
the MCP `author` verb, and save kept AI output to `11-Other-Authors/ai-as-user/<slug>/` with
`representativeness: endorsed`, `voice_weight: 0.0`.

## Context

- **Parent epic:** #EPIC_03_NUMBER
- **Predecessor issue(s):** #EPIC_03_ISSUE_02_NUMBER (real MCP verb).
- **SPEC section:** §10 (CrawDad wiring), §7 (`ai-as-user` save target + attribution).
- **Files involved:**
  - `crawdad/crawdad/` — slash-command handlers + MCP dispatch.
  - `crawdad/` tests.
- **Prior decisions:** `/crawdad ask` uses the research medium; `/crawdad draft` uses the draft path. Saving AI output is an explicit user action (e.g. a confirm), filed via the existing `save` path into `11-Other-Authors/ai-as-user/`. Reuse the FEAT-015 loop for trivial chat (unchanged).
- **State of the world:** CrawDad has six slash commands; `/crawdad draft` exists but composes inline rather than via the desk; there is no `/crawdad ask`.

## Output Format

A single PR containing:

- [ ] `/crawdad ask` + `/crawdad draft` routed to the MCP `author` verb.
- [ ] Kept AI output saved to `11-Other-Authors/ai-as-user/` with correct attribution.
- [ ] Tests: a research question returns a cited reply; a saved draft lands with `author=ai`, `representativeness=endorsed`, `voice_weight=0.0`.

## Examples

```
/crawdad ask What distinguishes F6 medicine from its toxic dose?
→ (cited, voiced answer; "save" reaction files it to 11-Other-Authors/ai-as-user/)
```

## Constraints

**Scope fence:** Do not implement allowlist/degradation here (ISSUE_04). Do not modify the desk
internals (EPIC_02).

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** Existing slash commands keep working; CrawDad now answers research
questions via the desk.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (CrawDad's quality gate: ≥90% branch, ≥95% docstring, MyPy strict, Ruff clean, complexity ≤10).
- [ ] Pre-commit clean for the `crawdad/` repo.
- [ ] PR body includes `Refs #EPIC_03_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `core`, `crawdad`
