# Integration Plan

## Strategic recommendation

After this analysis the projects' identity has **sharpened**, not shifted. Creek Vault is a phase-aware knowledge ontology with a voice-skill tree on top, where every other reference system is missing the wavelength layer entirely; that's not a gap to close but the air the project breathes. CrawDad is a Discord-first spiritual companion that consumes the data layer through a typed, MCP-mediated tool surface — its job is reflection, sounding-board, and draft scaffolding, not life-OS, not voice butler, not workflow automation. The single change of identity is that Creek Vault is now committing to **compile-then-query** as a discipline rather than as an aspiration: the compiled layer becomes the canonical query target, queries route through it, good answers file back into it, and `creek lint` keeps it healthy. Everything else is downstream of that commitment.

## Version-scheme note

Milestones below read **`v1.0 → v1.1 → v1.2 → v1.3+`** as a chronological sequence: `v1.0` is the first launched version of each project, and subsequent dot-releases extend it in order. Earlier drafts of this plan used `v1` followed by `v0.2 / v0.3 / v0.4+`, which inverted SemVer ordering and confused sequencing. The numbering is fixed; the candidate assignments are unchanged.

## Candidates index

| ID | Verdict | Pattern | Affects | Roadmap |
|---|---|---|---|---|
| [ADOPT-001](candidates/ADOPT-001-three-layer-compiled-architecture.md) | ADOPT | Three-layer compiled architecture (raw / compiled / schema) | Creek Vault | v1.0 |
| [ADOPT-002](candidates/ADOPT-002-creek-lint-unified-hygiene.md) | ADOPT | `creek lint` — unified vault hygiene operation | Creek Vault | v1.0 |
| [ADOPT-003](candidates/ADOPT-003-answer-filing-back-loop.md) | ADOPT | Answer-filing-back loop (`creek save`) | Both | v1.0 / v1.1 |
| [ADOPT-004](candidates/ADOPT-004-deterministic-first-pipeline.md) | ADOPT | Deterministic-first pipeline (named, audit-able) | Creek Vault | v1.0 |
| [ADOPT-005](candidates/ADOPT-005-audit-report-as-artifact.md) | ADOPT | `creek state` audit report as primary artifact | Creek Vault | v1.0 |
| [ADOPT-006](candidates/ADOPT-006-edge-confidence-tiers.md) | ADOPT | Edge confidence tiers (`computed` / `inferred` / `ambiguous`) | Creek Vault | v1.1 |
| [ADOPT-007](candidates/ADOPT-007-index-as-context-window-contract.md) | ADOPT | `index.md` as context-window contract | Creek Vault | v1.0 |
| [ADOPT-008](candidates/ADOPT-008-haiku-router-sonnet-composer.md) | ADOPT | Haiku-router + Sonnet-composer for CrawDad | CrawDad | v1.0 |
| [ADAPT-001](candidates/ADAPT-001-topology-clustering-complement.md) | ADAPT | Topology clustering (Leiden) as embedding complement | Creek Vault | v1.1 |
| [ADAPT-002](candidates/ADAPT-002-four-worker-decomposition.md) | ADAPT | Four-worker decomposition (Curator / Janitor / Distiller / Surveyor) | Both | v1.1 |
| [ADAPT-003](candidates/ADAPT-003-jig-style-workflow-dsl.md) | ADAPT | Jig-style workflow DSL for composite commands | CrawDad | v1.1 |
| [ADAPT-004](candidates/ADAPT-004-mcp-server-as-interface.md) | ADAPT | creek-tools MCP server as CrawDad's interface | Both | v1.0 |
| [ADAPT-005](candidates/ADAPT-005-modular-skill-files-as-schema.md) | ADAPT | Modular schema-skill files replace monolithic CLAUDE.md | Both | v1.0 |
| [ADAPT-006](candidates/ADAPT-006-multimodal-whisper-ocr-expansion.md) | ADAPT | Multimodal scope (Whisper + vision) | Creek Vault | v1.2 |
| [ADAPT-007](candidates/ADAPT-007-slash-command-grammar.md) | ADAPT | `/creek` and `/crawdad` slash-command grammar | Both | v1.0 / v1.1 |
| [ADAPT-008](candidates/ADAPT-008-episodic-semantic-memory.md) | ADAPT | Episodic / semantic memory split for CrawDad | CrawDad | v1.1 |
| [REJECT-001](candidates/REJECT-001-no-embeddings-no-vector-db.md) | REJECT | "No embeddings, no vector DB" — embeddings stay | Creek Vault | — |
| [REJECT-002](candidates/REJECT-002-n8n-as-agent-substrate.md) | REJECT | n8n as CrawDad's agent substrate | CrawDad | — |
| [REJECT-003](candidates/REJECT-003-screenless-voice-first-ambition.md) | REJECT | "Screenless" / voice-first interaction ambition | CrawDad | — |
| [REJECT-004](candidates/REJECT-004-compiler-as-deterministic-binary-metaphor.md) | REJECT | "Compile = deterministic binary" metaphor | Creek Vault | — |
| [DEFER-001](candidates/DEFER-001-multi-agent-client-mesh.md) | DEFER | Multi-agent client mesh | Both | v1.3+ |
| [DEFER-002](candidates/DEFER-002-temporal-durable-workflow-engine.md) | DEFER | Temporal as durable-workflow engine | Both | v1.3+ |
| [DEFER-003](candidates/DEFER-003-hybrid-bm25-vector-graph-retrieval.md) | DEFER | Hybrid BM25 + vector + graph retrieval at scale | Creek Vault | v1.3+ |

## v1.0 — First launch

The first launched version of each project should include the candidates that turn the project's stated identity into something operationally true.

### Creek Vault v1.0

- **Reconcile spec/implementation drift first.** Before any of the candidates below land, the phase-name, mode-name, and frequency-name disagreements between `creek_ontology_agent_prompt.md` and `creek-tools/docs/` need resolving. Compile-then-query depends on one source of truth for the most load-bearing taxonomy in the system. **Tracked as [`INC-019`](../git-issues/INC-019-spec-impl-drift-phase-mode-frequency-taxonomy.md)** — file the issue and resolve it before any ADOPT/ADAPT candidate is acted on. (See `LANDSCAPE.md` closing paragraph and `DELTA-MATRIX.md` synthesis for context.)
- **ADOPT-001 — Three-layer compiled architecture.** The structural fit is already good; the contract isn't. Commit to it.
- **ADOPT-002 — `creek lint` unified hygiene.** Unify the five existing emergence reports under one named operation; preserve the explicit "do not resolve paradoxes" guard rail.
- **ADOPT-003 — Answer-filing-back loop (`creek save`).** The CLI primitive ships in v1.0; the CrawDad-mediated invocation lands in v1.1.
- **ADOPT-004 — Deterministic-first pipeline (named).** Mostly already done; document the Pass 1/2/3 vocabulary, add the `--no-llm` end-to-end flag, expose pre-LLM yield in the audit report.
- **ADOPT-005 — `creek state` audit report.** The single document an agent or human reads to know what's in the vault. Wavelength snapshot at the top.
- **ADOPT-007 — `index.md` context-window contract.** Same artifact as `creek state`; pin the size budget.
- **ADAPT-005 — Modular schema skills.** A small root `AGENTS.md` plus `00-Creek-Meta/Skills/` schema-skill tree. Don't try to make the canonical spec be the agent context.

### CrawDad v1.0

CrawDad doesn't exist yet. v1.0 is the smallest viable bot that demonstrates the architectural commitments.

- **ADAPT-004 — creek-tools MCP server.** This is the v1.0 prerequisite for everything else CrawDad does. Privacy-tier ceiling on every read tool, audit log on every call, elevated authorization for purges.
- **ADOPT-008 — Haiku-router + Sonnet-composer agent loop.** Implement the loop, the 5-round cap, the 2000-char history truncation, the intents-as-MCP-tools schema.
- **ADAPT-007 — `/crawdad` slash-command grammar.** Six commands (`reflect`, `checkin`, `surface`, `draft`, `save`, `workflow run`). Conversational mode handles everything else.
- **CrawDad reads `00-Creek-Meta/State/latest.md` at session start.** This is the Graphify-style `PreToolUse` discipline, applied to Discord sessions.

What v1.0 explicitly does *not* need: the workflow DSL (v1.1), the four-worker scheduling (v1.1), the episodic/semantic memory split (v1.1), the topology clustering (v1.1), multimodal expansion (v1.2).

## v1.1 — Discipline and Density

**Theme:** make the v1.0 commitments actually run on a cadence; CrawDad gains its second-brain qualities.

- **ADAPT-001 — Topology clustering as complement.** Add Leiden over the resonance graph. Surface as a section in the audit report and as a fifth mining strategy (`community-bridge`).
- **ADAPT-002 — Four-worker decomposition.** Vocabulary refactor (Curator / Janitor / Distiller / Surveyor) plus a scheduled cadence (cron is fine; durable workflows are DEFER-002).
- **ADAPT-003 — Jig-style workflow DSL.** Three reference workflows ship: Substack draft pipeline, weekly Wavelength check-in, Compost surfacing. Workflows declare `phase_aware` and `privacy_tier_floor` as first-class attributes.
- **ADAPT-008 — Episodic / semantic memory for CrawDad.** Episodic store with pruning policy; weekly consolidation surfaces patterns to the vault.
- **ADOPT-006 — Edge confidence tiers.** Resonances and compiled-layer claims carry `computed | inferred | ambiguous`. `creek draft` exposes the tier mix.
- **ADOPT-003 follow-on — `/crawdad save` interaction.** The Discord-side wrapper for the v1.0 CLI primitive. Privacy-tier-aware default behavior.

## v1.2 — Reach

**Theme:** widen what the system can ingest and what CrawDad can hold in conversation.

- **ADAPT-006 — Multimodal scope (Whisper + vision).** Audio ingestor (`faster-whisper`, local-only). Video as audio-extraction. Vision pass on images as opt-in. Voice-memo fragments default to `intimate` tier.
- **Wavelength-aware Leiden.** Research direction surfaced in ADAPT-001 — incorporate phase as a node attribute in topology clustering. May not land in v1.2 if the research doesn't pan out; flagged here as the natural follow-on.
- **Voice-skill tree refinement.** Once the compiled layer is mature and audit reports surface drift, the voice-skill tree's exemplar fragments become more confident; a periodic refresh becomes meaningful.

## v1.3+ — Optional and Conditional

- **DEFER-001 — Multi-agent client mesh.** Revisit only if interaction surfaces multiply.
- **DEFER-002 — Temporal durable-workflow engine.** Revisit when the cron file becomes unreadable.
- **DEFER-003 — Hybrid BM25 + vector + graph retrieval.** Revisit when fragment count crosses ~50K or CrawDad needs keyword-anchored citations.

## CrawDad design implications, by interaction mode

This section organizes the candidates by the three CrawDad interaction styles the user has named. It is the input to the eventual CrawDad design prompt.

### Standard skill commands

The atomic, slash-invocable surface. These map 1:1 to MCP tools:

- `/crawdad surface` (read-only) → `creek.state.read` MCP tool (ADOPT-005, ADAPT-004)
- `/crawdad checkin` → `creek.report.wavelength` MCP tool
- `/crawdad save <type>` → `creek.save` MCP tool (ADOPT-003, ADAPT-004)
- `/crawdad draft <topic>` → `creek.mine` + `creek.draft` MCP tools chained
- Help discoverable from any prefix (ADAPT-007).

The Haiku router (ADOPT-008) maps natural-language paraphrases onto the same intents — "what's surfacing this week?" goes to the same intent as `/crawdad surface`.

### Workflow-driven commands

Composite operations declared as Jig-style workflow files (ADAPT-003), invoked via `/crawdad workflow run <id>` or recognized by the Haiku router as workflow-running intents:

- `substack-draft-phase-transitions.jig.yaml` — mine thread-terminus seeds in the current phase, draft, file to `07-Voice/Drafts/`.
- `aptitude-module-exercise.jig.yaml` — given a target phase and frequency, surface relevant praxis material and compose an exercise.
- `wavelength-checkin-weekly.jig.yaml` — Sunday cadence, reads last week's fragments, generates a phase summary, files to `05-Wavelength/`.
- `compost-surface.jig.yaml` — reads `10-Liminal/Compost/`, surfaces three abandoned ideas with current relevance.

Each workflow declares `phase_aware: true` and `privacy_tier_floor`. The dispatcher refuses to run when constraints aren't met. Voice fidelity comes from the `creek.draft` step's skill-stack assembly, not from the dispatcher.

### Conversational chat

Reflective mode, sounding-board work, idea surfacing. The Haiku router (ADOPT-008) emits intents like `creek.state.read`, `creek.mine`, `creek.surface_paradox`, but the response is composed by Sonnet conditioned on the voice-skill tree's `voice-core/SKILL.md` plus phase-appropriate skills.

Three behavioral commitments here that follow from the non-negotiables:

1. **Phase-aware reflection.** CrawDad never urges high-energy action when the user's recent fragments cluster in Bottoming Out. The audit report's wavelength snapshot conditions the response.
2. **Paradox-tolerant reflection.** When CrawDad notices a contradiction, it names it and routes to `10-Liminal/Paradoxes/` (via `creek.save`); it does not propose a resolution.
3. **Voice-faithful reflection.** Sonnet composition always activates `voice-core/SKILL.md` plus the relevant register skill (default `confessional` for reflective conversation, `playful` for surfacing-mode interactions).

The episodic/semantic memory split (ADAPT-008) keeps short-term continuity (last 14 days of conversation) without bloating context, and weekly consolidation surfaces patterns to the vault for human review.

## Voice fidelity through the stack

The user's killer features — Substack drafts, APTITUDE essays, Archetypal Wavelength course content — depend on voice fidelity, which is downstream of data discipline. The connections, made explicit:

- **Compile-then-query (ADOPT-001) feeds voice fidelity** because compiled-layer pages carry phase / mode / dosage / frequency forward to drafting time. A draft that knows it's writing into a Withdrawal-phase exemplar set sounds different from one that knows only the topic.
- **Provenance preservation (ADOPT-001 + ADOPT-006) feeds voice fidelity** because exact-quote retrieval back to fragments is what prevents the lossy-compression risk that would otherwise compound through synthesis pages into drafts. The user's prose voice has specific phrasings; compile-time summarization eats them; provenance lets the drafter recover them.
- **The lint pass NOT resolving paradoxes (ADOPT-002) feeds voice fidelity** because the user's voice is paradox-tolerant. A wiki that flattened contradictions would produce drafts that sound resolved and confident in places the user is genuinely held in tension. That's not voice fidelity; that's the wrong voice.
- **The audit report's wavelength snapshot (ADOPT-005, ADOPT-007) feeds voice fidelity** because phase context is part of voice. The same idea sounds different in Rising than in Diminishing; surfacing the phase to the drafter is what makes phase-appropriate prose possible.
- **The four-worker decomposition's Surveyor (ADAPT-002) feeds voice fidelity** because high-quality resonance discovery is what produces the exemplar sets the voice-skill tree draws on.
- **CrawDad's MCP-mediated `creek draft` (ADAPT-004) feeds voice fidelity** because draft generation is centralized in `creek-tools` where the skill stack lives, not duplicated in the bot. There's one skill-stack-assembly pathway, exercised by both the developer and CrawDad, with one definition of "voice."

The integration is not just data discipline → prose quality at the end. Each layer in the stack is a voice-fidelity gate.

## Distinctiveness watchlist

Five things to *not* lose during integration, in priority order matching the four non-negotiables:

1. **Phase awareness as the lens, not as a tag.** Every borrowed pattern (lint, audit report, workflow DSL, slash commands, episodic memory) must surface phase as primary context, not as one filter among many.
2. **Paradox preservation as the response to contradiction.** Karpathy's lint resolves; Creek's lint routes-and-holds. Every reference-system pattern that flags contradictions must be adapted to honor `10-Liminal/Paradoxes/`.
3. **Voice fidelity as the integration gate, not the post-hoc filter.** Voice fidelity is a stack property: compile, provenance, phase, register, skill stack. It can be lost at any layer; protect it at every layer.
4. **Liminal as a fourth layer, not a wiki sub-folder.** Karpathy's three-layer model gets adapted to four because `10-Liminal/` is a different kind of thing — content that is *deliberately* uncategorized. Force-classifying it for completeness is a non-negotiable violation.
5. **Privacy / sovereignty by construction at the MCP boundary.** The user's data should not leave the machine without a tier-appropriate consent path; the MCP server is the place to make this load-bearing rather than a downstream check.

## Open questions for the human

The user has dialed in most decisions upfront. The remaining open questions are values calls, not technical ones:

1. **What's the right verb for the compile operation?** `creek compile`, `creek synthesize`, or fold into `creek lint --rebuild`? Karpathy used "compile"; the metaphor is rejected (REJECT-004) but the word might still be the right shorthand. Naming choice.
2. **Should the four-worker decomposition (ADAPT-002) replace existing CLI command names or alias them?** Replacing is cleaner; aliasing avoids breaking habits. Migration choice.
3. **Workflow file format — YAML, markdown frontmatter, or a custom syntax?** The candidate (ADAPT-003) recommends YAML; if the user prefers markdown frontmatter (closer to the rest of the vault), that's also fine. Aesthetic choice.
4. **Schema-skills location — `00-Creek-Meta/Skills/` or sibling to the voice-skill tree?** The candidate (ADAPT-005) puts them in `00-Creek-Meta/Skills/` to keep separation from voice; if the user wants one unified `creek-skills/` with sub-trees, that's also fine. Vault-organization choice.
5. **Whether `creek_ontology_agent_prompt.md` itself should be split into per-section skills.** It's 1358 lines and over a context window already. Worth doing, but only the user can authorize moving the canonical spec around. Editorial choice.

These are best resolved as candidate-by-candidate decisions during v1 implementation rather than upfront.
