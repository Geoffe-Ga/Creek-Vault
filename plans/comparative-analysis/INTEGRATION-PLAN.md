# Integration Plan

## Strategic recommendation

After this analysis the projects' identity has **sharpened**, not shifted. Creek Vault is a phase-aware knowledge ontology with a voice-skill tree on top, where every other reference system is missing the wavelength layer entirely; that's not a gap to close but the air the project breathes. CrawDad is a Discord-first spiritual companion that consumes the data layer through a typed, MCP-mediated tool surface — its job is reflection, sounding-board, and draft scaffolding, not life-OS, not voice butler, not workflow automation. The single change of identity is that Creek Vault is now committing to **compile-then-query** as a discipline rather than as an aspiration: the compiled layer becomes the canonical query target, queries route through it, good answers file back into it, and `creek lint` keeps it healthy. Everything else is downstream of that commitment.

## v1 — Pre-launch

The pre-launch version of each project should include the candidates that turn the project's stated identity into something operationally true.

### Creek Vault v1

- **Reconcile spec/implementation drift first.** Before any of the candidates below land, the phase-name and mode-name disagreements between `creek_ontology_agent_prompt.md` and `creek-tools/docs/classification.md` need resolving. Compile-then-query depends on one source of truth for the most load-bearing taxonomy in the system. (This is a v1 prerequisite, not a candidate — see `LANDSCAPE.md` closing paragraph and `DELTA-MATRIX.md` synthesis.)
- **ADOPT-001 — Three-layer compiled architecture.** The structural fit is already good; the contract isn't. Commit to it.
- **ADOPT-002 — `creek lint` unified hygiene.** Unify the five existing emergence reports under one named operation; preserve the explicit "do not resolve paradoxes" guard rail.
- **ADOPT-003 — Answer-filing-back loop (`creek save`).** The CLI primitive ships in v1; the CrawDad-mediated invocation lands in v0.2.
- **ADOPT-004 — Deterministic-first pipeline (named).** Mostly already done; document the Pass 1/2/3 vocabulary, add the `--no-llm` end-to-end flag, expose pre-LLM yield in the audit report.
- **ADOPT-005 — `creek state` audit report.** The single document an agent or human reads to know what's in the vault. Wavelength snapshot at the top.
- **ADOPT-007 — `index.md` context-window contract.** Same artifact as `creek state`; pin the size budget.
- **ADAPT-005 — Modular schema skills.** A small root `AGENTS.md` plus `00-Creek-Meta/Skills/` schema-skill tree. Don't try to make the canonical spec be the agent context.

### CrawDad v1

CrawDad doesn't exist yet. v1 is the smallest viable bot that demonstrates the architectural commitments.

- **ADAPT-004 — creek-tools MCP server.** This is the v1 prerequisite for everything else CrawDad does. Privacy-tier ceiling on every read tool, audit log on every call, elevated authorization for purges.
- **ADOPT-008 — Haiku-router + Sonnet-composer agent loop.** Implement the loop, the 5-round cap, the 2000-char history truncation, the intents-as-MCP-tools schema.
- **ADAPT-007 — `/crawdad` slash-command grammar.** Six commands (`reflect`, `checkin`, `surface`, `draft`, `save`, `workflow run`). Conversational mode handles everything else.
- **CrawDad reads `00-Creek-Meta/State/latest.md` at session start.** This is the Graphify-style `PreToolUse` discipline, applied to Discord sessions.

What v1 explicitly does *not* need: the workflow DSL (v0.2), the four-worker scheduling (v0.2), the episodic/semantic memory split (v0.2), the topology clustering (v0.2), multimodal expansion (v0.3).

## v0.2 — Discipline and Density

**Theme:** make the v1 commitments actually run on a cadence; CrawDad gains its second-brain qualities.

- **ADAPT-001 — Topology clustering as complement.** Add Leiden over the resonance graph. Surface as a section in the audit report and as a fifth mining strategy (`community-bridge`).
- **ADAPT-002 — Four-worker decomposition.** Vocabulary refactor (Curator / Janitor / Distiller / Surveyor) plus a scheduled cadence (cron is fine; durable workflows are DEFER-002).
- **ADAPT-003 — Jig-style workflow DSL.** Three reference workflows ship: Substack draft pipeline, weekly Wavelength check-in, Compost surfacing. Workflows declare `phase_aware` and `privacy_tier_floor` as first-class attributes.
- **ADAPT-008 — Episodic / semantic memory for CrawDad.** Episodic store with pruning policy; weekly consolidation surfaces patterns to the vault.
- **ADOPT-006 — Edge confidence tiers.** Resonances and compiled-layer claims carry `extracted | inferred | ambiguous`. `creek draft` exposes the tier mix.
- **ADOPT-003 follow-on — `/crawdad save` interaction.** The Discord-side wrapper for the v1 CLI primitive. Privacy-tier-aware default behavior.

## v0.3 — Reach

**Theme:** widen what the system can ingest and what CrawDad can hold in conversation.

- **ADAPT-006 — Multimodal scope (Whisper + vision).** Audio ingestor (`faster-whisper`, local-only). Video as audio-extraction. Vision pass on images as opt-in. Voice-memo fragments default to `intimate` tier.
- **Wavelength-aware Leiden.** Research direction surfaced in ADAPT-001 — incorporate phase as a node attribute in topology clustering. May not land in v0.3 if the research doesn't pan out; flagged here as the natural follow-on.
- **Voice-skill tree refinement.** Once the compiled layer is mature and audit reports surface drift, the voice-skill tree's exemplar fragments become more confident; a periodic refresh becomes meaningful.

## v0.4+ — Optional and Conditional

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
