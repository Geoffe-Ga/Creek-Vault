# compile.SKILL.md

**Verb:** `creek compile`
**Layer:** schema → produces compiled-layer pages
**Budget:** ≤1500 tokens
**Loaded by:** Claude Code, CrawDad, any agent running rollups

## What `creek compile` does

Compile rolls up fragments from the **raw** layer (`01-Fragments/`) into curated pages on the **compiled** layer (`02-Threads/`, `03-Eddies/`, `04-Praxis/`, `06-Frequencies/`). It is the operation that makes the vault compounding rather than session-bound.

Compile does **not** classify fragments (`creek classify` does). It does **not** resolve contradictions (`creek lint` routes them; `compile` refuses to flatten them). It does **not** ingest new sources (`creek ingest` does).

## The four-layer architecture

`creek compile` is the boundary operation between two of the four layers:

| Layer | Folder(s) | Compile's relationship |
|---|---|---|
| **Raw** | `01-Fragments/` | Read-only inputs. Never modified by compile. |
| **Compiled** | `02-Threads/`, `03-Eddies/`, `04-Praxis/`, `06-Frequencies/` | Compile's only write target. Rewritten in full per run; previous version archived. |
| **Schema** | `00-Creek-Meta/Ontology/`, `00-Creek-Meta/Skills/`, `AGENTS.md` | Read-only contract. Compile honors it; never edits it. |
| **Liminal** | `10-Liminal/` (Paradoxes, Unnamed, Synchronicities, Compost) | Compile defers here when synthesis would falsify. See "When compile refuses." |

## Operating discipline

Three rules, in order of precedence:

### 1. Per-source

Each compile run targets one source page at a time — a single Thread, a single Eddy, a single Praxis note, a single Frequency index. Bulk recompile is allowed but is a sequence of per-source runs, not a monolithic pass. This bounds blast radius and makes provenance auditable.

### 2. Interactive (manual trigger)

Compile is human-triggered, not on-write or on-schedule. The human invokes `creek compile <target>` (or accepts a CrawDad proposal); the agent reads the relevant fragments, drafts the compiled page, and presents the diff for human review before writing. No silent recompiles. No auto-recompile on fragment append.

### 3. Provenance-preserving

Every claim on a compiled page must trace back to the fragment(s) that produced it. The compiled page's frontmatter carries:

```yaml
type: thread | eddy | praxis | frequency_index
sources:
  - frag-{id}      # every fragment that contributed
  - frag-{id}
compiled_at: "YYYY-MM-DDTHH:MM:SS-08:00"
compiled_by: claude-code | crawdad | human
compiled_from: ["01-Fragments/Conversations/...", ...]
wavelength:
  phase: rising | peaking | withdrawal | diminishing | bottoming_out | restoration
  mode: inhabit | express | collaborate | integrate | absorb
  orientation: do | feel | do_feel
  dosage: medicine | toxic
frequency:
  primary: F1 | F2 | F3 | F4 | F5 | F6 | F7 | F8 | F9 | F10 | unclassified
  secondary: []
```

Inline claims that paraphrase a fragment carry a per-claim provenance pointer (e.g., `[^frag-abc12]`) so a downstream consumer (`creek draft`, CrawDad reflection) can verify against source. Lossy compression here is the load-bearing risk for voice fidelity; treat provenance loss as a compile bug.

## Canonical taxonomy

Compile uses these names verbatim. INC-019 reconciled drift; do not paraphrase.

- **Phases (six):** Rising, Peaking, Withdrawal, Diminishing, Bottoming Out, Restoration.
- **Modes (five):** Inhabit, Express, Collaborate, Integrate, Absorb.
- **Orientations:** Do, Feel, Do/Feel.
- **Dosage:** Medicine, Toxic.
- **Frequencies (ten):** F1 Agency, F2 Receptivity, F3 Self-Love / Power, F4 Community Love / Conformity, F5 Achievism, F6 Pluralism, F7 Integration, F8 True Self / Transcendence, F9 Unity, F10 Emptiness.

Compiled pages must inherit these tags from their source fragments. When source fragments disagree on phase/mode/dosage, surface both — never collapse to the majority.

## When compile refuses

Compile actively defers to `10-Liminal/` rather than synthesize across:

| Situation | Destination |
|---|---|
| Fragments contradict on a settled claim | `10-Liminal/Paradoxes/` (link both fragments; do not pick a winner) |
| Pattern present in fragments but no Thread or Eddy fits | `10-Liminal/Unnamed/` (let it cluster; recompile after the next Unnamed Digest) |
| Cross-source semantic identity > 0.9 across >30 days, different sources | `10-Liminal/Synchronicities/` |
| Thread has gone dormant or resolved with abandoned energy | `10-Liminal/Compost/` |

These are not error states. They are the system honoring the spec's §10 emergence infrastructure. A compile run that produces compiled-page output **and** Liminal-folder output for the same target is correct, not broken.

## What good output looks like

A compiled page is correct when:

- Every paragraph of synthesis carries a provenance pointer to ≥1 fragment.
- Phase, mode, orientation, dosage, frequency in the frontmatter match the canonical taxonomy verbatim.
- No contradictions are silently resolved — divergent stances are either preserved on the page or routed to `10-Liminal/Paradoxes/`.
- The page reads as the human's voice, not a generic summary. Use the voice-skill tree at `creek-skills/` to condition tone.
- The previous version is archived (not overwritten in place) so the audit trail through `00-Creek-Meta/Processing-Log/` stays append-only.

## What compile does not do

- Does not classify. `creek classify` writes per-fragment frontmatter; compile reads it.
- Does not lint. `creek lint` checks hygiene; compile produces the artifacts that lint inspects.
- Does not save. `creek save` files good answers back into the vault as new fragments or compiled pages; compile only consumes existing fragments.
- Does not delete. Compile is additive (with archive); deletion is `creek purge` and requires elevated auth.
- Does not bypass privacy tiers. Intimate-tier fragments may appear in compiled pages only when the human has explicitly opted in for that target.
