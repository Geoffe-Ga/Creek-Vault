# ADAPT-005: Modular Skill Files Replace Monolithic CLAUDE.md

**Verdict:** ADAPT
**Source system:** Karpathy LLM Wiki (and `cablate/llm-atomic-wiki` critique)
**Affects:** Creek Vault data layer (schema artifact) + CrawDad agent layer (skill stack)
**Roadmap target:** v1 (mostly already in place via the voice-skill tree; this candidate names the discipline)
**Estimated complexity:** S
**Conflicts with non-negotiables?** none

## What it is

Karpathy's pattern uses a single `CLAUDE.md` as the schema document. Community implementations have noted that monolithic CLAUDE.md files grow past ~5000 tokens and the agent starts ignoring sections (per [`cablate/llm-atomic-wiki`](https://github.com/cablate/llm-atomic-wiki) and the ScrapingArt stack). The fix is **modular skill files** — split the schema into per-domain skills (Claude Code Skill format or AGENTS.md-style folder structure), each loaded only when relevant.

## Why it's interesting

Creek already has the modular discipline — the voice-skill tree (`creek-skills/`) is one SKILL.md per frequency, phase, mode, register, thread, and eddy. What's missing is the *schema* analog: there's a canonical ontology spec at `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` (1358 lines, ~28k tokens — way over Karpathy's monolithic ceiling) and a `creek-tools/CLAUDE.md` that's about quality-engineering practices, not vault semantics. Neither is the right thing for an agent to load at session start.

The integration plan proposes (ADOPT-001) a thin root-level schema document that points at canonical material. This candidate names the *discipline*: that document should be small, and it should compose with skill files rather than try to be exhaustive.

## Fit with Creek Vault and/or CrawDad

A proposed structure:

```
Creek-Vault/
├── AGENTS.md                          # ~2-3k tokens. Compile/query/lint contract.
├── 00-Creek-Meta/
│   ├── Ontology/
│   │   └── creek_ontology_agent_prompt.md   # Canonical spec (unchanged).
│   └── Skills/                        # Schema-flavored skills (NEW)
│       ├── compile.SKILL.md           # How to compile fragments → wiki pages.
│       ├── lint.SKILL.md              # How to run the lint pass.
│       ├── save.SKILL.md              # How to file good answers back.
│       ├── paradox.SKILL.md           # How to handle contradictions (NOT resolve).
│       ├── liminal.SKILL.md           # When to leave content uncategorized.
│       ├── privacy-tier.SKILL.md      # Tier rules.
│       └── wavelength-aware.SKILL.md  # Phase as interpretive lens.
└── creek-skills/                      # Voice-skill tree (existing)
    ├── frequencies/
    ├── phases/
    ├── modes/
    ├── registers/
    └── ...
```

Two skill trees: `00-Creek-Meta/Skills/` is the *schema* skill tree (how the system works); `creek-skills/` is the *voice* skill tree (how to write in the human's voice). Both are modular, both are loaded contextually.

The root `AGENTS.md` is the entry point: it's small, points at the schema skill tree for operational details, and points at `creek_ontology_agent_prompt.md` as the canonical specification.

## Translation if adapted

Three considerations:

1. **The voice-skill tree is already the right pattern.** Don't change `creek-skills/`. Add a *parallel* schema skill tree at `00-Creek-Meta/Skills/`. Keep the two distinct — voice skills are LLM-conditioning material; schema skills are operational instructions.
2. **Don't put paradox-preservation rules in the canonical spec; put them in a skill.** The spec currently has paradox material in §10.2 (about 100 lines). That's fine for canonical reference but agents won't load 1358-line specs. A `paradox.SKILL.md` of ~500 tokens with the explicit "do not resolve" guard rail is what gets loaded into agent context at runtime.
3. **CrawDad loads its own skill subset.** When CrawDad starts a session, it loads `compile.SKILL.md`, `save.SKILL.md`, `paradox.SKILL.md`, `privacy-tier.SKILL.md`, `wavelength-aware.SKILL.md`, and the current `voice-core/SKILL.md`. It doesn't load the entire spec or every voice skill — those load contextually based on intent.

## Dependencies

- Depends on: ADOPT-001 (the schema is the contract for the three-layer architecture).
- Pairs with: ADOPT-002 (`lint.SKILL.md`), ADOPT-003 (`save.SKILL.md`), ADAPT-004 (`privacy-tier.SKILL.md` is what the MCP server enforces).

## Acceptance criteria

- A root `AGENTS.md` (or vault-root `CLAUDE.md`) exists, fits in under ~3000 tokens, and points at canonical material rather than duplicating it.
- A `00-Creek-Meta/Skills/` directory exists with at least the seven skills listed above.
- Each schema skill is under ~1500 tokens and self-contained.
- The voice-skill tree at `creek-skills/` is unchanged.
- CrawDad's session-start documents which schema skills it loads (and doesn't).
- A `creek lint` check (or pre-commit hook) verifies the size budget on each schema skill — over-budget skills fail the build.
