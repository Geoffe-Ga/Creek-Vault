## Role

You are a senior Python engineer building a new `creek/author/` package in creek-tools, fluent in
the Anthropic SDK managed-agents / tool-use pattern and this repo's Typer CLI.

## Goal

Wire the Creek Writing Desk end-to-end with stubs: a `creek author --medium research --query "..."`
command that drives an Anthropic-SDK **Conductor** which calls **stub** specialist tools (Graph,
Retrieval, Ontology), a **stub** Voice agent, and a **stub** Reflection node, returning a typed
`AuthoredDraft` with mock `provenance` and a reflection verdict. `--dry-run` prints the plan +
(stub) evidence bundle. Smoke tests prove every surface returns the right shape.

## Context

- **Parent epic:** #EPIC_02_NUMBER
- **Predecessor issue(s):** none — this is the skeleton issue for EPIC_02. (Depends on EPIC_01 having merged for the attribution model + fixture content.)
- **SPEC section:** §4.1 (agent roster), §4.2 (orchestration flow), §4.4 (SDK wiring), §10 (CLI).
- **Files involved:**
  - `creek-tools/creek/author/__init__.py`, `conductor.py`, `agents.py` (stub specialists), `voice.py` (stub), `reflection.py` (stub), `models.py` (`AuthoredDraft`, `EvidenceBundle`).
  - `creek-tools/creek/cli.py` — `author` command.
  - `creek-tools/tests/` — smoke tests + a CLI test.
- **Prior decisions:** Specialists return **structured evidence** (claims + `source_fragments`), never free prose. Bounded retries via a new `max_author_rounds` (default 3, bounds [1,10]). Model IDs come from config, never hard-coded.
- **State of the world:** No `creek/author/` package exists. `AuthoredDraft`/`EvidenceBundle` do not exist. Provenance (`ProvenanceEntry`) already exists in `models.py` and should be reused.

## Output Format

A single PR containing:

- [ ] `creek/author/` package with typed stub Conductor + stub specialist tools + stub Voice + stub Reflection.
- [ ] `creek author --medium research --query ... [--dry-run --max-rounds N]` returning an `AuthoredDraft` with mock provenance + verdict.
- [ ] Smoke tests: the command returns a valid `AuthoredDraft`; `--dry-run` prints a plan; each stub tool returns the declared shape.
- [ ] Anthropic SDK calls are behind a thin client wrapper that is mockable in tests (no live network in unit tests).

## Examples

```bash
creek author --medium research --query "What is F6 Pluralism?" --vault ./fixture --dry-run
# prints: PLAN: [graph, retrieval, ontology] → synthesize → voice → reflect
#         EVIDENCE (stub): 2 claims, 2 source_fragments
```
```python
draft = run_author(medium="research", query="...", vault=fixture)
assert isinstance(draft, AuthoredDraft)
assert draft.provenance            # non-empty (mock)
assert draft.verdict in {"PASS", "REVISE", "ESCALATE"}
```

## Constraints

**Scope fence:** Every specialist, the Voice agent, and the Reflection node are **stubs** returning
typed mock data. Do not implement real retrieval, synthesis, voicing, or judging — those are
ISSUE_03/04/05/06. Only the `research` medium path is wired.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The full desk pipeline runs end-to-end on the fixture vault returning a
shaped (mock) draft — demoable from day one — without breaking any existing CLI surface.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] PR body includes `Refs #EPIC_02_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `tracer-skeleton`, `author-desk`
