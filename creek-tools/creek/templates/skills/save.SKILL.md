# save.SKILL.md

**Verb:** `creek save`
**Layer:** schema → writes to raw, compiled, or liminal layer depending on destination
**Budget:** ≤1500 tokens
**Loaded by:** Claude Code, CrawDad, any agent that wants to file a good answer back into the vault

## What `creek save` does

`creek save` files a good answer — a reflection, a comparison, a connection, a draft, a named contradiction — back into the vault. It is the mechanic that differentiates "wiki" from "stored chat history." Without it, every conversation re-derives the same insights from scratch.

The command takes a body, a target type, optional fragment provenance, and writes a properly-classified note with full frontmatter to the right destination.

## The destination decision

Six destinations. The agent (or human) picks one explicitly per save call; ambiguity routes to `unnamed`.

| Destination | Folder | When to pick it |
|---|---|---|
| **thread** | `02-Threads/Active/` (or `Dormant/`, `Resolved/`) | A reflection that names a known *narrative current* in a fresh way — temporal, has a direction. |
| **eddy** | `03-Eddies/` | A reflection that names a *topic cluster* — a recurring concern, project, or area of attention. |
| **praxis** | `04-Praxis/Daily/` (or `Seasonal/`, `Situational/`) | A reflection that produces an *actionable insight* or practice the human can deepen. |
| **paradox** | `10-Liminal/Paradoxes/` | A reflection that names a contradiction. **Save preserves it; never resolves it.** |
| **unnamed** | `10-Liminal/Unnamed/` | A pattern that's clearly real but doesn't fit anywhere yet. The default for ambiguous routing. |
| **draft** | `07-Voice/Drafts/` | A draft fragment of writing — essay seed, blog idea, Substack outline — that the human may polish. |

The simplest viable v1 is to ask the human: `creek save --target {thread|eddy|praxis|paradox|unnamed|draft}`. The smarter v1.1 is for CrawDad to propose a target and let the human confirm, using the same rules-then-LLM pipeline as `creek classify`.

## Privacy-tier defaults

Privacy tiers come from spec §13.2 and gate what `creek save` will write. The defaults:

| Tier | Sources (examples) | Auto-save default |
|---|---|---|
| **Open** | Published essays, public Discord messages | Full save (title + body + frontmatter). |
| **Personal** | Chatbot conversations, private Discord messages | Title + summary only by default. Body saves only when the human passes `--include-body` per call. |
| **Intimate** | Journal entries, recovery-related content | **Never auto-saves.** Human must invoke save explicitly with `--tier intimate --consent` per call; otherwise the operation refuses. Never enters voice-proxy generation. |

Privacy tier is inherited from the source conversation/fragment. A reflection generated during a conversation that touched intimate content is intimate by default; the agent must explicitly down-tier with human consent.

## Required frontmatter

Every saved note carries:

```yaml
type: thread | eddy | praxis | paradox | unnamed | draft
id: {primitive}-{uuid-short}
title: "{auto-generated, human-overridable}"
created: "YYYY-MM-DDTHH:MM:SS-08:00"
saved_by: claude-code | crawdad | human
saved_from:
  source: claude-code-conversation | discord | journal | other
  conversation_id: "{if applicable}"
  message_id: "{Discord message ID, if applicable}"
provenance:
  contributing_fragments:
    - frag-{id}        # fragments that informed this answer
    - frag-{id}
  activated_skills:
    - voice-core
    - {phase-skill, mode-skill, frequency-skill, register-skill}
privacy_tier: open | personal | intimate
consent: explicit | inherited      # required when tier is intimate
wavelength:
  phase: rising | peaking | withdrawal | diminishing | bottoming_out | restoration
  mode: inhabit | express | collaborate | integrate | absorb
  orientation: do | feel | do_feel
  dosage: medicine | toxic
  observed_phase: rising | peaking | withdrawal | diminishing | bottoming_out | restoration  # optional; written when source conversation contradicts inherited phase
frequency:
  primary: F1 | F2 | F3 | F4 | F5 | F6 | F7 | F8 | F9 | F10 | unclassified
  secondary: []
```

CrawDad knows the human's recent wavelength position; saved notes inherit it unless the conversation explicitly contradicts. When the conversation suggests a different phase/mode, save both as `wavelength.phase` and `wavelength.observed_phase` so the next compile can reconcile.

## Canonical taxonomy

Save uses these names verbatim (INC-019 reconciliation):

- **Phases (six):** Rising, Peaking, Withdrawal, Diminishing, Bottoming Out, Restoration.
- **Modes (five):** Inhabit, Express, Collaborate, Integrate, Absorb.
- **Orientations:** Do, Feel, Do/Feel.
- **Dosage:** Medicine, Toxic.
- **Frequencies (ten):** F1 Agency, F2 Receptivity, F3 Self-Love / Power, F4 Community Love / Conformity, F5 Achievism, F6 Pluralism, F7 Integration, F8 True Self / Transcendence, F9 Unity, F10 Emptiness.

## Operating discipline

1. **Auto-filing is opt-in per command.** A `/crawdad reflect` that auto-files everything is the wrong default. Save is explicit, both for privacy and because the human owns what enters the vault.
2. **Paradoxes route to `paradox`, never to `thread` or `eddy`.** A regression test (FEAT-009) verifies this. Saving a contradiction as a synthesis page is a bug, not a feature.
3. **Provenance is required, not optional.** A save without `contributing_fragments` provenance is rejected. If the answer didn't draw on fragments, it's not a save target — it's a new fragment, route through `creek ingest` instead.
4. **The human can always overwrite.** The auto-generated title, the proposed destination, the inherited tier — all are defaults. The human's explicit choice wins.
5. **Liminal destinations are first-class.** `paradox` and `unnamed` are not consolation prizes; they are the right destinations when they're the right destinations. Honor them.

## What save does not do

- Does not compile. Saved Threads/Eddies/Praxis appear in the compiled layer but are seeds, not rollups; `creek compile` later integrates them.
- Does not lint. Saved notes get inspected by `creek lint` like any other compiled-layer artifact.
- Does not bypass `creek purge`. Saved notes are deletable like any other; saving never grants permanence.
- Does not file intimate content silently. Refuses without explicit consent.
