# AGENTS.md — Creek Vault Agent Contract

**Status:** Schema entry point. ≤3000 tokens. Points at canonical material; does not duplicate it.

This file is the first thing an agent (Claude Code, CrawDad, or any other LLM session) loads when starting work in the Creek Vault. It defines the four-verb contract — **compile / query / lint / save** — and points at the canonical specification and the per-verb schema skills.

## Canonical specification

The single source of truth for the Creek Ontology is:

- [`00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md`](00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md)

When this file and the spec disagree, **the spec wins.** This file is a pointer; do not paraphrase the spec here.

## The four-layer architecture

Creek's data model has four layers with explicit ownership:

| Layer | Folder(s) | Owner | Mutability |
|---|---|---|---|
| **Raw** | `01-Fragments/` | Human-writable, ingestor-writable | Append-only after ingest; provenance frozen |
| **Compiled** | `02-Threads/`, `03-Eddies/`, `04-Praxis/`, `06-Frequencies/` | LLM-curated, human-reviewed | Rewritable by `creek compile`; every claim links back to fragment IDs |
| **Schema** | `00-Creek-Meta/Ontology/`, `00-Creek-Meta/Skills/`, this file | Human + agent co-evolved | Versioned; changes are deliberate |
| **Liminal** | `10-Liminal/` (Paradoxes, Unnamed, Synchronicities, Compost) | LLM-surfaced, human-curated | Compilation explicitly fails or refuses here — that's the point |

The voice-skill tree at `creek-skills/` (LLM-conditioning material) is parallel to and distinct from the schema-skill tree at `00-Creek-Meta/Skills/` (operational instructions). Do not conflate them.

## The four-verb contract

Every agent operating on the vault uses these four verbs. Each has a dedicated schema-skill file under `00-Creek-Meta/Skills/`; load the relevant one when the verb is invoked.

| Verb | Skill file | What it does |
|---|---|---|
| `creek compile` | [`00-Creek-Meta/Skills/compile.SKILL.md`](00-Creek-Meta/Skills/compile.SKILL.md) | Per-source, interactive, provenance-preserving rollup of fragments → compiled-layer pages (Threads, Eddies, Praxis, Frequency indexes) |
| `creek query` | (covered inline in this file; dedicated skill arrives with FEAT-004) | Read the compiled layer first; fall back to fragments only when the compiled page is missing or insufficient |
| `creek lint` | [`00-Creek-Meta/Skills/lint.SKILL.md`](00-Creek-Meta/Skills/lint.SKILL.md) | Unified hygiene check across orphans, contradictions, stale claims, missing pages, data gaps. **Lint never resolves paradoxes; contradictions route to `10-Liminal/Paradoxes/`.** |
| `creek save` | [`00-Creek-Meta/Skills/save.SKILL.md`](00-Creek-Meta/Skills/save.SKILL.md) | File a good answer back into the vault as thread / eddy / praxis / paradox / unnamed / draft, honoring privacy tiers |

### Query — the compiled-layer-first rule

Until FEAT-004 ships a dedicated `query.SKILL.md`, the contract is:

1. **Compiled-layer first.** When asked "what does the vault know about X?", read the relevant Thread, Eddy, or Frequency-index page first.
2. **Fragments are the fallback.** Drop down to `01-Fragments/` only when the compiled page is missing, stale, or contradicts what fresh fragments say. When you do, surface the discrepancy as a `creek lint` candidate.
3. **`10-Liminal/` is not a fallback.** It's a deliberate destination, not an overflow bin. Don't read from it as if it were a synthesis layer; read from it to honor what the system has refused to flatten.

## Canonical taxonomy

All schema skills, compile prompts, lint reports, and save destinations use these names verbatim. No synonyms, no abbreviations. INC-019 reconciled drift here; do not re-introduce it.

- **Wavelength phases (six):** Rising, Peaking, Withdrawal, Diminishing, Bottoming Out, Restoration.
- **Wavelength modes (five):** Inhabit, Express, Collaborate, Integrate, Absorb.
- **Wavelength orientations:** Do, Feel, Do/Feel.
- **Wavelength dosage:** Medicine, Toxic.
- **APTITUDE frequencies (ten):** F1 Agency, F2 Receptivity, F3 Self-Love / Power, F4 Community Love / Conformity, F5 Achievism, F6 Pluralism, F7 Integration, F8 True Self / Transcendence, F9 Unity, F10 Emptiness.

For the full definitions of each, see spec §6 (Frequencies) and §7 (Wavelength). For the Medicine/Toxic-by-phase map, see §7.3.

## Non-negotiables

These hard constraints come from the spec; they are repeated here because every verb depends on them:

1. **Liminal preservation.** Paradoxes are data, not defects. Unnamed patterns are research sites, not failures. The four verbs honor this — `compile` refuses to flatten contradictions, `lint` routes them to `10-Liminal/`, `save` has a `paradox` destination, and `query` reads `10-Liminal/` only when it is the right destination.
2. **Provenance preservation.** Every claim on a compiled page traces back to fragment IDs in frontmatter. No orphan synthesis. See `compile.SKILL.md` for the required frontmatter shape.
3. **Privacy tiers (Open / Personal / Intimate).** Intimate content never auto-saves and never enters voice-proxy generation without explicit consent. Personal content saves title + summary only by default. See `save.SKILL.md` for defaults and spec §13.2 for sources.
4. **No external API calls during ingestion.** The LLM classification pass is the only stage that may hit a cloud API, and only when the human has explicitly opted in. Compile, lint, save, and query are local-default.
5. **The human owns what enters the vault.** Auto-filing is opt-in per command. The human is the curator; the agent is the bookkeeper.

## Token budgets

- This file: **≤3000 tokens.**
- Each schema skill under `00-Creek-Meta/Skills/`: **≤1500 tokens.**

A `creek lint` size check enforces these budgets (FEAT-008). Files over budget fail the build.

## What this file is not

- Not the spec. The spec is `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md`.
- Not the voice-skill tree. That lives at `creek-skills/` and is generated by `creek-tools/creek/generate/skills.py`.
- Not the engineering CLAUDE.md. `creek-tools/CLAUDE.md` covers Python quality standards for the tooling subproject and is unrelated to vault semantics.
- Not exhaustive. When in doubt, follow the spec, then the relevant schema skill, then ask.
