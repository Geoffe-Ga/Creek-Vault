## Epic Summary

Build the multi-agent **Creek Writing Desk** and deliver the **research / encyclopedic medium**
end-to-end: `creek author --medium research --query "..."` produces a cited, voiced,
reflection-gated answer about the APTITUDE frequency framework and the Archetypal Wavelength.
Implemented on the Anthropic SDK managed-agents pattern — a Conductor (supervisor) routing to
Graph / Retrieval / Ontology specialist agents, a Voice agent, and a Reflection node
(LLM-as-judge) with bounded retries and human escalation. Implements SPEC §4, §5 (research
contract), §6, §8, §10 (CLI).

## Scope

**In scope:**
- `creek/author/` package: Conductor, specialist agents, Voice agent, Reflection node.
- `creek author` CLI verb with `--medium research`, `--query`, `--dry-run`, `--max-rounds`, `--include-tier`.
- `MediumContract` model + the `research` medium contract (deployed to `00-Creek-Meta/Skills/mediums/`).
- Graph-native retrieval (backlink walk, frequency/wavelength index seeds) returning structured, provenance-tracked evidence.
- Conductor synthesis (claims → fragment IDs), Voice agent (voice-skill tree), Reflection node (research rubric) with `max_author_rounds` retries + ESCALATE.
- Anthropic SDK wiring with prompt caching + model tiering.

**Out of scope:**
- MCP verb / CrawDad routing (EPIC_03).
- Mediums other than `research` (EPIC_04).
- Creating `11-Other-Authors/` or the attribution model (EPIC_01 — consumed here, not built here).

## Success Criteria

The epic is done when:

- [ ] `creek author --medium research --query "..."` produces a cited answer on a fixture vault, every substantive claim carrying `provenance` to real fragment IDs / the ontology spec.
- [ ] `--dry-run` prints the plan + evidence bundle without synthesizing.
- [ ] A deliberately bad draft triggers REVISE then improves; exhausted retries ESCALATE rather than ship.
- [ ] Taxonomy in output is canonical (no aliases; INC-019 respected).
- [ ] Prompt-cache hits asserted on the static context.
- [ ] All child issues closed; `./scripts/check-all.sh` green on `main`.

## Child Issues

_Filled in after child issues are filed._

- [ ] #NNN — Skeleton: `creek author` + `creek/author/` wired end-to-end with stubs
- [ ] #NNN — Core: `MediumContract` model + `research` contract
- [ ] #NNN — Core: Graph + Retrieval specialist agents (graph-native evidence)
- [ ] #NNN — Core: Ontology specialist agent (classify / voice / wavelength analytics)
- [ ] #NNN — Core: Conductor synthesis + Voice agent rendering
- [ ] #NNN — Edges: Reflection node + bounded retries + escalation
- [ ] #NNN — Polish: prompt caching, model tiering, cost guard, docs

## Sequencing Notes

- **Blocks:** EPIC_03 (surfacing) and EPIC_04 (more mediums).
- **Blocked by:** EPIC_01 (attribution model + retrieval surface).
- **Parallel-safe:** EPIC_03 and EPIC_04 may start once this epic's skeleton + core land.

## SPEC Reference

`plans/crawdad-writing-system/SPEC.md` — §4 (architecture / agent roster / SDK), §5 (medium skill tree — `research` first), §6 (knowledge-graph navigation), §8 (reflection node), §10 (CLI), §13 open question #4 (backlink bounds).

## Labels

`epic`, `spec-decomposition`, `author-desk`
