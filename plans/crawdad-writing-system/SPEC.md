# FEAT-041 — Creek Writing Desk: A Multi-Agent Authoring System for CrawDad

- **Status:** Draft (for review)
- **Date:** 2026-06-01
- **Owner:** geoff@creekmasons.com
- **Driving inspiration:** JPMorgan "Ask D.A.V.I.D." — a research desk in graph form (supervisor agent routes to specialist agents across data, documents, and analytics, with a reflection node before the answer ships).
- **Branch:** `claude/crawdad-writing-system-uAGrM`
- **Supersedes/extends:** FEAT-015 (two-LLM agent loop), FEAT-040.x (voice-skill tree + voice-fidelity lint)

---

## 1. Summary

Turn CrawDad from a single router→composer loop into a **writing desk in graph form**: a
supervisor agent that plans a writing task, routes sub-tasks to specialist agents that each
own one slice of the Creek knowledge base, and a reflection node (LLM-as-judge) that gates
every draft on voice fidelity, ontological accuracy, provenance, and privacy before it ships.

The desk produces high-quality writing across multiple mediums — encyclopedic answers,
chatbot replies, essays, research pieces, book reports, and agentic-coding how-tos — all in
the accurate voice of the vault owner, all backed by citations into the vault's Obsidian
knowledge graph.

Two structural changes make this a true wiki-style knowledge base:

1. A new top-level vault category **`11-Other-Authors/`** that captures ideas the owner did
   *not* author (blogs, books, AI-as-user pieces), organized by author, with their ideas
   fully classified into the ontology so they become navigable graph nodes — but explicitly
   walled off from voice-proxy training so they never corrupt how the owner *sounds*.
2. A two-axis attribution model (`voice_weight` + `representativeness`) so the desk knows
   *whose voice* a fragment carries and *how strongly it represents the owner's beliefs*.

The **first build target** is the **research / encyclopedic medium** — CrawDad as the
authoritative answerer of any question about the APTITUDE frequency framework and the
Archetypal Wavelength. Every other medium reuses the same desk; only the medium contract
(output shape + reflection rubric) changes.

---

## 2. Motivation

CrawDad today (FEAT-015) is a two-LLM loop: a Haiku router extracts intent, an MCP dispatcher
calls creek verbs, and a Sonnet composer wraps results in a voiced reply, capped at
`MAX_LOOP_ROUNDS`. That is excellent for conversational replies but has a single composer doing
retrieval reasoning, synthesis, voicing, and self-checking all at once. As the JPMorgan note
puts it: *demos optimize for autonomy; production systems optimize for trust.*

For long-form, citable, voice-accurate writing the gap shows:

- **No division of labor.** One model juggles graph navigation, semantic retrieval, ontology
  analysis, voicing, and judging — quality degrades as the task grows.
- **No quality gate.** "Sounds right" is not good enough. There is no dedicated reflection
  step that can *reject* a draft and request a retry.
- **No medium awareness.** An essay, a how-to, and a chat reply have different structures,
  reflection rubrics, and citation norms. Today they all flow through one composer.
- **No clean place for borrowed ideas.** Source material the owner wants to absorb has nowhere
  to live, and AI-authored pieces blur into the owner's own voice corpus — risking model
  collapse (voice proxy trained partly on its own prior output).

This SPEC closes those gaps by adopting the orchestrated-specialists pattern, adding the
quality gate, and giving borrowed and AI-authored ideas a first-class, attribution-aware home.

---

## 3. Goals and Non-Goals

### Goals

1. A **multi-agent writing desk** living in `creek-tools`, exposed via a new `creek author`
   CLI verb and an MCP tool, callable identically from Claude Code and from CrawDad.
2. Implemented on the **Anthropic SDK managed-agents pattern**: a supervisor agent + specialist
   sub-agents + a reflection node.
3. A **medium skill tree** so the desk can target encyclopedic answers, chat replies, essays,
   research pieces, book reports, and agentic-coding how-tos — research first.
4. **Knowledge-graph-native retrieval**: specialists navigate Obsidian backlinks, frequency and
   wavelength indexes, threads, and eddies — not just flat semantic search.
5. A new **`11-Other-Authors/`** vault category with per-author manifests and full ontological
   classification of the *ideas* (not the voice) it contains.
6. A **two-axis attribution model** (`voice_weight`, `representativeness`) wired into
   frontmatter, voice-proxy training eligibility, and reflection.
7. **Trust layer parity** with the JPMorgan pattern: evaluation, retries, references
   (citations), personalization (voice), and human-in-the-loop escalation.

### Non-Goals

- Replacing the FEAT-015 conversational loop for simple replies (the desk is invoked for
  authoring tasks; trivial chit-chat still uses the existing loop, optionally as the desk's
  "chatbot" medium).
- Auto-publishing anywhere. The desk produces drafts filed into the vault; publishing stays a
  human action.
- Training or fine-tuning custom models. "Analytics" here means the existing classifier/voice
  analysis, not new ML.
- Auto-ingesting external content from the network. `11-Other-Authors/` is populated through
  the normal opt-in ingest path; the desk reads it, it does not crawl.

---

## 4. Architecture Overview

### 4.1 The agent roster

Mapping the "Ask D.A.V.I.D." roles onto Creek primitives:

| JPMorgan role | Creek agent | Owns | Backed by |
|---|---|---|---|
| Supervisor | **Conductor** | Planning, routing, memory, medium selection, human-in-the-loop | Long-term memory = the vault; medium skill tree |
| Structured-data agent | **Graph agent** | The *compiled* layer + frontmatter + Obsidian backlinks: threads, eddies, frequency/wavelength indexes | `creek query` over compiled pages; backlink walk |
| RAG agent | **Retrieval agent** | The *raw* layer + reference + `11-Other-Authors/`: fragments, source material, unstructured notes | embeddings (`link/embeddings.py`), `creek query --raw` |
| Analytics agent | **Ontology agent** | "Proprietary models/APIs" → Creek's classifier + voice analysis + wavelength/frequency analytics | `classify/`, `generate/voice.py`, `generate/wavelength.py` |
| (Creek addition) | **Voice agent** | Rendering the synthesized draft in the owner's voice using the FEAT-040 voice-skill tree | `<vault>/creek-skills/` voice stack |
| Reflection node | **Reflection agent** | LLM-as-judge: voice fidelity, ontological accuracy, citation completeness, privacy compliance, paradox preservation. Can REJECT → retry | FEAT-040.x voice-fidelity lint; ontology spec; provenance check |

The **Voice agent** is the Creek-specific addition the JPMorgan desk doesn't need: faithful
personalization is a first-class goal here, so voicing is its own specialist rather than a side
effect of the composer.

### 4.2 Orchestration flow

```
                    ┌──────────────────────────────────────────────┐
   request ───────▶ │  CONDUCTOR (supervisor)                       │
   (question +      │  • classify request → medium                  │
    medium hint)    │  • load medium contract from medium skill tree│
                    │  • plan: decompose into retrieval sub-tasks   │
                    │  • manage working memory (vault context)      │
                    └───────┬───────────────┬───────────────┬───────┘
                            │               │               │
                  ┌─────────▼──────┐ ┌──────▼───────┐ ┌──────▼────────┐
                  │  GRAPH agent   │ │ RETRIEVAL    │ │  ONTOLOGY     │
                  │ compiled layer │ │ agent        │ │  agent        │
                  │ + backlinks    │ │ raw + 11-    │ │ classify /    │
                  │ + F/Wavelength │ │ Other-Authors│ │ voice / phase │
                  │   indexes      │ │ + reference  │ │  analytics    │
                  └─────────┬──────┘ └──────┬───────┘ └──────┬────────┘
                            └───────────────┼───────────────┘
                                            │ evidence bundle (with provenance)
                                  ┌─────────▼─────────┐
                                  │   CONDUCTOR        │  synthesize structured draft
                                  │   (synthesis)      │  (claims → fragment IDs)
                                  └─────────┬─────────┘
                                  ┌─────────▼─────────┐
                                  │   VOICE agent      │  render in owner's voice
                                  │ (voice-skill tree) │  (medium- & phase-aware)
                                  └─────────┬─────────┘
                                  ┌─────────▼─────────┐
                                  │ REFLECTION agent   │  judge against medium rubric
                                  │ (LLM-as-judge)     │  PASS │ REVISE │ ESCALATE
                                  └───┬────────┬───────┘
                                REVISE│        │PASS
                                      │        ▼
                            (retry, bounded)   ship draft → save to vault
                                               ESCALATE → human-in-the-loop
```

Bounded retries mirror `MAX_LOOP_ROUNDS`: a new `max_author_rounds` (default 3, bounded
[1, 10]) caps reflection→revise cycles. On exhausting retries the desk **escalates to a human**
rather than shipping a sub-threshold draft (the "Ask David still asks real David when it
matters" principle).

### 4.3 Where it lives (per decision)

- **Core** in `creek-tools`: `creek/author/` package (conductor, agents, medium contracts,
  reflection). Single source of truth.
- **CLI**: `creek author --medium research --query "..." --vault <path>`.
- **MCP**: a new `author` verb in the MCP surface (`creek_mcp/`) so Claude Code and CrawDad
  call the identical desk over stdio.
- **CrawDad adapter**: the `/crawdad draft` slash command (FEAT-016) routes to the MCP `author`
  verb instead of the inline composer when the request is an authoring task.

### 4.4 Anthropic SDK managed-agents implementation (per decision)

- The **Conductor** is the orchestrating agent; specialists are **sub-agents** it invokes as
  tools. Use the Anthropic SDK's managed-agents / tool-use pattern with **prompt caching** on
  the large static context (ontology spec, medium contract, voice skills) to control cost.
- Each specialist is a typed tool contract: input = sub-task + scope, output = structured
  evidence (claims + `source_fragments` provenance). No specialist returns free prose to the
  conductor; they return *structured evidence*, so synthesis stays grounded.
- The **reflection agent** runs as a separate judge call (cheaper model acceptable for the
  rubric pass; escalate to a stronger model on borderline scores) returning a verdict enum
  (`PASS | REVISE | ESCALATE`) + structured findings the conductor feeds back into a retry.
- Model tiering: Haiku for routing/classification sub-tasks, Sonnet for synthesis/voicing,
  reflection configurable. All model IDs live in config (`crawdad.yaml` / creek config), never
  hard-coded.

---

## 5. Output mediums (the medium skill tree)

Mediums are declared as **medium contracts** — small skill files the conductor loads, analogous
to the schema-skill tree. Each contract specifies: structure/format, which specialists to
weight, the citation norm, the target privacy default, and the **reflection rubric** for that
medium. They compose *in tandem* with the existing schema-skill tree and the FEAT-040 voice
skills.

Proposed medium contracts (build order):

1. **`research`** *(FIRST BUILD)* — encyclopedic / wiki answer about APTITUDE & Archetypal
   Wavelength. Graph + Ontology agents weighted heavily; every claim cited to the ontology spec
   (`00-Creek-Meta/Ontology/`), frequency/wavelength index pages, or fragments. Reflection
   prioritizes ontological accuracy and citation completeness over voice flourish.
2. **`chat`** — short voiced reply. Lowest latency; may bypass full retrieval for trivial turns
   (this is the existing FEAT-015 loop, re-homed as a medium).
3. **`essay`** — long-form, thought-provoking Substack-style piece. Voice + Retrieval weighted;
   reflection prioritizes voice fidelity and narrative coherence.
4. **`research-piece`** — externally-facing analytical writing; heavier citation discipline,
   may draw on `11-Other-Authors/` source material with explicit attribution.
5. **`book-report`** — synthesizes a `11-Other-Authors/` work against the owner's threads/eddies
   ("how does this book resonate with what I already believe?").
6. **`how-to`** — agentic-coding guide; Graph + Ontology weighted toward Technical fragments and
   Praxis; structured, example-driven; reflection checks runnability/accuracy.

Skill-tree placement: `00-Creek-Meta/Skills/mediums/<medium>.MEDIUM.md`, deployed from
`creek-tools/creek/templates/skills/mediums/`, same ≤1500-token budget and lint discipline as
the schema skills.

---

## 6. Knowledge-graph navigation

The desk's retrieval is graph-native, not flat search. Specialists exploit Obsidian structure:

- **Backlink walk:** from a seed page (e.g. `F6-Pluralism`), the Graph agent follows
  `[[wiki-links]]` in/out to gather connected threads, eddies, and fragments — bounded
  breadth/depth so the evidence bundle stays focused.
- **Index entry points:** frequency indexes (`06-Frequencies/F*/`), wavelength phase maps
  (`05-Wavelength/Phase-Maps/`), and eddy clusters (`03-Eddies/`) are first-class seeds.
- **Resonance chains:** the Ontology agent can request resonance neighbors (embedding +
  thematic) to surface non-obviously-linked but semantically related fragments.
- **Liminal awareness:** paradoxes and unnamed patterns (`10-Liminal/`) are surfaced, never
  flattened — the reflection rubric checks that the draft *preserves* contradictions rather than
  resolving them away.

This realizes the "subagent swarm attacking the knowledge base collaboratively" idea: each
specialist enters the graph at a different door, and the conductor merges their walks into one
provenance-tracked evidence bundle.

---

## 7. Vault change: `11-Other-Authors/` (per decision)

### 7.1 Structure

```
11-Other-Authors/
├── _README.md                      # explains attribution model & voice-training exclusion
├── <author-slug>/                  # one folder per author
│   ├── _author.md                  # author manifest (see 7.2)
│   └── <work-slug>/                # one folder per work
│       └── *.md                    # fragments of that work, fully classified
└── ai-as-user/                     # the special author: CrawDad's owner-facing AI output
    ├── _author.md
    └── <piece-slug>/
        └── *.md
```

- Organized **by author**, then by **work** — per the user's model.
- `ai-as-user` is a reserved author slug for AI-authored pieces the owner treats as
  representative of their interests/beliefs.
- Added to the scaffold by creating
  `creek-tools/creek/templates/vault/11-Other-Authors/` (with `.gitkeep` +
  `_README.md` + an example `_author.md` template); `scaffold.py` copies it on next
  `creek init`. No manifest file to edit — the template tree *is* the source of truth.

### 7.2 Author manifest (`_author.md` frontmatter)

```yaml
type: author_manifest
author_slug: "naval-ravikant"        # matches folder name
display_name: "Naval Ravikant"
author_kind: human_source | ai_as_user | collaborator
voice_weight: 0.0                    # eligibility for voice-proxy TRAINING (see 7.4)
representativeness: reference         # how strongly this represents the OWNER's beliefs (7.4)
default_privacy_tier: open
attribution_required: true           # citations must name this author
notes: "Captured for ideas, not voice. Threads on leverage & specific knowledge."
```

### 7.3 Idea classification (key requirement)

Every fragment under `11-Other-Authors/` is **fully classified into the ontology** — `frequency`,
`wavelength.phase/mode/orientation`, primitives (linked into `threads`/`eddies`), `tags` — exactly
like a native fragment, so the *ideas* become navigable graph nodes and can resonate with the
owner's own material via backlinks. What differs is *attribution*, not classification:

- `source.author` = `other` (human source) or `ai` (ai-as-user) — reusing the existing
  `models.py` enum (`self | ai | other | collaborative`).
- New `source.author_slug` = the author folder (links the fragment to its `_author.md`).
- The two new attribution-axis fields (7.4).

### 7.4 Two-axis attribution model

The owner's intuition — "source material reflects ideas not voice; AI-authored pieces are more
representative than source material but less than my intimate writing" — becomes **two orthogonal
axes**:

**Axis A — `voice_weight` (0.0–1.0): eligibility to train the voice proxy.**

| Content | voice_weight | Rationale |
|---|---|---|
| Owner's own writing (`01-Fragments`, `07-Voice`) | 1.0 | This *is* the voice |
| `ai-as-user` pieces | 0.0 (default; configurable, capped low) | Excluded from training to prevent voice-proxy model collapse (AI learning from its own output) |
| Human source authors | 0.0 | Never the owner's voice |

**Axis B — `representativeness` (enum): how strongly the content represents the owner's beliefs.**

| Value | Meaning | Typical content |
|---|---|---|
| `self` | The owner's own belief, in their own words | native fragments |
| `endorsed` | Owner endorses this as representative | `ai-as-user` pieces |
| `aspirational` | Ideas the owner aspires to embody | curated source material |
| `reference` | Captured neutrally; ideas of interest, not necessarily held | most source material |

This cleanly encodes the requested ordering for *belief* weight
(`self > endorsed > aspirational ≈ reference`) while keeping *voice* training strictly the
owner's own. The desk uses Axis B to decide how much weight an idea carries when synthesizing a
"what the owner believes" answer, and Axis A to keep the voice proxy clean.

### 7.5 Voice-training exclusion (safety)

`creek skills generate` (FEAT-040.x) MUST exclude all `11-Other-Authors/` content from the voice
corpus by default (gate on `voice_weight > 0`). `ai-as-user` is excluded by default to avoid
feedback collapse; raising its weight is an explicit, audited opt-in. This is the
voice-fidelity analogue of the privacy fail-closed rule.

---

## 8. Quality gates (the reflection node in detail)

Per medium, the reflection agent scores and returns `PASS | REVISE | ESCALATE`:

1. **Voice fidelity** — reuse FEAT-040.x voice-fidelity lint; flag AI-tells, register drift,
   phase-inappropriate tone. (Weighted high for `essay`/`chat`, lower for `research`.)
2. **Ontological accuracy** — frequencies, phases, modes, orientations, dosage use the canonical
   taxonomy (no synonyms/aliases; INC-019 deprecations respected). Misuse → REVISE.
3. **Citation completeness** — every substantive claim maps to `source_fragments` / a named
   author / the ontology spec. Uncited claim → REVISE. (Hard gate for `research`/`research-piece`.)
4. **Privacy compliance** — no `personal`/`intimate` content leaks into an `open`-destined
   draft without the required `--include-tier` + consent; fail-closed.
5. **Paradox preservation** — contradictions from `10-Liminal/` are surfaced, not resolved.
6. **Attribution correctness** — borrowed ideas attributed to their `11-Other-Authors/` author;
   the owner's voice never falsely claims an idea it's only `reference`-weighted.

Exhausting `max_author_rounds` without PASS → **ESCALATE** (human-in-the-loop), never ship.

---

## 9. Data-model & frontmatter changes

In `creek-tools/creek/models.py`:

- `Fragment.source.author_slug: str | None` — links to a `11-Other-Authors/<slug>/_author.md`.
- `Fragment.voice_weight: float = 1.0` — default 1.0 for native fragments; 0.0 under
  `11-Other-Authors/`.
- `Fragment.representativeness: Literal["self","endorsed","aspirational","reference"] = "self"`.
- New `AuthorManifest` Pydantic model for `_author.md`.
- `MediumContract` model (loaded from the medium skill tree).
- Provenance already exists (`ProvenanceEntry`); the desk's evidence bundle reuses it so saved
  drafts carry `provenance: [{claim, source_fragments}]`.

Backward-compatible: missing fields default to native-fragment semantics (`voice_weight=1.0`,
`representativeness="self"`), so existing vaults are unaffected.

---

## 10. CLI & MCP surface

```bash
# First-build target
creek author --medium research \
  --query "What distinguishes F6 Pluralism medicine from its toxic dose across phases?" \
  --vault ~/Obsidian/Creek-Vault

# Other mediums (same desk)
creek author --medium essay --topic "leverage vs. presence" --vault <path>
creek author --medium book-report --work 11-Other-Authors/naval-ravikant/almanack --vault <path>
creek author --medium how-to --topic "wiring an MCP verb in creek-tools" --vault <path>

# Flags
--include-tier personal|intimate   # privacy gate (audited)
--max-rounds N                      # override max_author_rounds
--dry-run                           # show plan + evidence bundle, don't synthesize
--save 11-Other-Authors/ai-as-user/<slug>   # file the AI-authored output back as ai-as-user
```

- MCP: new `author` tool mirroring the CLI args, returning the draft + provenance + reflection
  verdict.
- CrawDad: `/crawdad draft <topic>` and a new `/crawdad ask <question>` (research medium) route
  to the MCP `author` verb. AI-authored outputs the owner keeps are saved to
  `11-Other-Authors/ai-as-user/` with `representativeness: endorsed`, `voice_weight: 0.0`.

---

## 11. Phased delivery plan

Each phase is independently shippable and stays green (≥90% branch cov, ≥95% docstring, MyPy
strict, Ruff clean, complexity ≤10).

- **FEAT-041.1 — Vault category & attribution model.**
  Add `11-Other-Authors/` to the scaffold; `AuthorManifest` model; `voice_weight` +
  `representativeness` + `source.author_slug` frontmatter; exclude `11-Other-Authors/` from
  voice-proxy training in `creek skills generate`.
  *AC:* `creek init` scaffolds the category with `_README.md` + `_author.md` template; classifier
  accepts and round-trips the new fields; voice corpus provably excludes the category;
  fail-closed default `voice_weight=0.0` under the folder; tests cover each.

- **FEAT-041.2 — Medium contracts + skill tree.**
  `MediumContract` model; `research` and `chat` contracts deployed to
  `00-Creek-Meta/Skills/mediums/`; lint budget enforced.
  *AC:* contracts load and lint clean; research rubric defined; documented.

- **FEAT-041.3 — Specialist agents (read-only).**
  Graph, Retrieval, Ontology agents as typed tools returning structured evidence + provenance;
  backlink-walk with bounded breadth/depth.
  *AC:* each agent returns provenance-tracked evidence for a seed query; graph walk respects
  bounds; deterministic given a fixed vault fixture.

- **FEAT-041.4 — Conductor + synthesis + Voice agent (research medium e2e).**
  Anthropic-SDK conductor with prompt caching; synthesis grounded in evidence; Voice agent via
  the voice-skill tree.
  *AC:* `creek author --medium research` produces a cited answer about APTITUDE/Wavelength on the
  fixture vault; every claim carries provenance; `--dry-run` shows the plan.

- **FEAT-041.5 — Reflection node + bounded retries + escalation.**
  LLM-as-judge with the research rubric; `max_author_rounds`; PASS/REVISE/ESCALATE.
  *AC:* a deliberately bad draft triggers REVISE then improves; exhausted retries ESCALATE
  rather than ship; privacy/citation hard gates enforced.

- **FEAT-041.6 — MCP `author` verb + CrawDad wiring.**
  Expose over MCP; route `/crawdad ask` + `/crawdad draft`; save AI output to
  `ai-as-user/`.
  *AC:* CrawDad answers a research question end-to-end; AI output filed with correct attribution;
  allowlist + graceful degradation honored.

- **FEAT-041.7+ — Remaining mediums.**
  `essay`, `research-piece`, `book-report`, `how-to` contracts + rubrics, reusing the desk.

---

## 12. Testing strategy

- **Fixture vault** with native fragments + a seeded `11-Other-Authors/` author and an
  `ai-as-user` piece, spanning several frequencies/phases and at least one `10-Liminal/` paradox.
- **Specialist unit tests:** evidence bundles are provenance-complete and bound-respecting;
  graph walk is deterministic on the fixture.
- **Reflection tests (mutation-grade):** bad drafts (uncited claim, alias misuse, privacy leak,
  resolved paradox, false attribution) each produce the correct verdict — assert exact verdict +
  finding, not just "not PASS".
- **Voice-exclusion test:** voice corpus generation provably omits `11-Other-Authors/`.
- **e2e (marked):** `creek author --medium research` on the fixture; assert citations resolve to
  real fragment IDs and taxonomy is canonical.
- **Cost guard:** prompt-caching hit assertions on the static context.

## 13. Risks & open questions

- **Cost/latency:** multi-agent + retries is more expensive than one composer. Mitigation: model
  tiering, prompt caching, `chat` medium bypass, bounded rounds. *Open: per-medium cost budget?*
- **Voice-proxy collapse:** `ai-as-user` content must not silently re-enter training. Mitigation:
  `voice_weight=0.0` fail-closed + audited opt-in. *Open: ever allow >0?*
- **Over-citation in conversational mediums:** research-grade citation would make chat replies
  stilted. Mitigation: per-medium rubric weighting.
- **Author-slug collisions / merges** in `11-Other-Authors/`. *Open: canonical slug authority?*
- **Backlink-walk blowup** on a dense graph. Mitigation: breadth/depth bounds + relevance
  pruning. *Open: default bounds?*

## 14. Alternatives considered

- **Extend the FEAT-015 loop in CrawDad** (rejected as primary): Discord-only, duplicates logic
  Claude Code also wants; the desk belongs in the shared toolchain. The loop is retained as the
  `chat` medium.
- **Claude Code Task subagents instead of Anthropic SDK managed agents** (rejected per decision):
  cheaper but harness-bound and not callable from CrawDad; SDK managed agents give one
  implementation both surfaces share.
- **Two separate categories (`Source-Material` + `AI-Authored`)** (rejected per owner's
  reframing): the by-author model with `ai-as-user` as a reserved author is more uniform and
  scales to any number of authors, while the two attribution axes capture the belief/voice
  nuance more precisely than two folders would.

## 15. Files affected (anticipated)

- `creek-tools/creek/templates/vault/11-Other-Authors/**` (new scaffold subtree)
- `creek-tools/creek/templates/skills/mediums/*.MEDIUM.md` (new)
- `creek-tools/creek/author/**` (new package: conductor, agents, mediums, reflection)
- `creek-tools/creek/models.py` (attribution fields, `AuthorManifest`, `MediumContract`)
- `creek-tools/creek/scaffold.py` (no change if template-copy suffices; verify category count)
- `creek-tools/creek/generate/voice.py` (voice-corpus exclusion of `11-Other-Authors/`)
- `creek-tools/creek/cli.py` (`author` command)
- `creek-tools/creek_mcp/**` (`author` MCP verb)
- `crawdad/**` (route `/crawdad ask` + `/crawdad draft` to the `author` verb)
- `docs/Ontology/creek_ontology_agent_prompt.md` + `creek-tools/docs/` (document the category,
  attribution model, and the writing desk)
- `creek-tools/creek/templates/AGENTS.md` (note the new category + medium skill tree)
