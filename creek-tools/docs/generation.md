# Generation

The `generate` family of commands turns a classified, linked vault into the artefacts you actually publish or use:

- **Reports** — wavelength snapshots, decision contexts, paradox preservation, compost tracking, the Unnamed Digest.
- **Voice Skill Tree** — one `SKILL.md` per frequency / phase / mode / register / thread / eddy, plus two meta skills.
- **Idea mining** — four discovery strategies that surface essay seeds.
- **Drafting** — turns a mined idea into an essay using the activated skill stack.

## Reports

`creek report --type <kind>` renders one of several report types. Each is implemented under `creek/generate/`:

| `--type`         | Output location                            | Source module |
|------------------|--------------------------------------------|---------------|
| `wavelength`     | `05-Wavelength/<period>-<date>.md`         | `creek.generate.wavelength` |
| `synchronicity`  | `05-Wavelength/Synchronicities/`           | `creek.generate.synchronicity` |
| `decision`       | `08-Decisions/<fragment>.md`               | `creek.generate.decisions` |
| `paradox`        | `04-Praxis/Paradoxes/`                     | `creek.generate.paradox` |
| `compost`        | `04-Praxis/Compost/`                       | `creek.generate.compost` |
| `unnamed`        | `10-Liminal/Unnamed-Digest-<period>.md`    | `creek.generate.unnamed` |
| `tags`           | `00-Creek-Meta/Tags/`                      | `creek.generate.tags` |
| `lexicon`        | `09-Reference/Lexicon.md`                  | `creek.generate.lexicon` |

Periods are `--period weekly` or `--period monthly`. Wavelength reports contain phase-distribution histograms, dosage trends per frequency, and detected phase transitions.

```bash
# Weekly wavelength snapshot.
creek report --type wavelength --period weekly --vault ~/Obsidian/Creek-Vault

# Monthly Unnamed Digest of unclassified fragments.
creek report --type unnamed --period monthly --vault ~/Obsidian/Creek-Vault

# Surprising cross-source resonances.
creek report --type synchronicity --vault ~/Obsidian/Creek-Vault
```

## Voice Skill Tree

`creek skills` writes a tree of `SKILL.md` files under `<output>` (default `<vault>/creek-skills/`):

```
creek-skills/
├── frequencies/
│   ├── amplitude/SKILL.md
│   ├── pitch/SKILL.md
│   └── …
├── phases/{origins,rising,peaking,cresting,receding,composting}/SKILL.md
├── modes/{solo,dialogue,reflective,analytic}/SKILL.md
├── registers/{intimate,personal,public,professional}/SKILL.md
├── threads/<thread-id>/SKILL.md
├── eddies/<eddy-id>/SKILL.md
└── meta/
    ├── voice-core/SKILL.md
    └── style-guide/SKILL.md
```

Each `SKILL.md` is a Claude Code skill — name, description, when to invoke, and 3–5 high-confidence exemplar fragments quoted with provenance. The intent is twofold:

1. **Voice grounding for `creek draft`.** When you mine an idea and ask for a draft, the matching skills are stacked into the prompt as exemplars.
2. **Self-knowledge.** The tree itself is a map of your thinking — what you actually write about, in what register, in which phase.

```bash
creek skills --generate --vault ~/Obsidian/Creek-Vault
```

Re-run any time after ingesting / classifying. The generator is idempotent — only changed exemplars are rewritten.

## Mining

`creek mine` runs four idea-discovery strategies and prints a deduped, score-ranked table of seeds:

| Strategy            | What it finds |
|---------------------|---------------|
| `liminal-cross-eddy` | Fragments that bridge two otherwise-disjoint eddies — boundary objects. |
| `thread-terminus`   | Threads that have gone quiet. The synthesis essay you haven't written. |
| `resonance-chain`   | Long chains of resonances that span a topic across time. |
| `wavelength-phase`  | Fragments whose frequency clusters strongly in the phase you specify. |

```bash
# Print the top 10 ideas across all strategies.
creek mine --vault ~/Obsidian/Creek-Vault --limit 10

# Filter to ideas that fit a current Cresting phase.
creek mine --vault ~/Obsidian/Creek-Vault --phase cresting --limit 5
```

Each `IdeaSeed` has a strategy, a score, the contributing fragments, and a hint about the angle. You'll typically pick one (`--index N`) to feed into `creek draft`.

## Drafting

`creek draft` takes a mined idea, assembles the skill stack (frequency + phase + mode + register skills, plus the voice-core meta skill), gathers the source material (the seed's contributing fragments), prompts the LLM, and saves the draft to `07-Voice/Drafts/<date>-<slug>.md`.

```bash
# Draft the top idea using the currently configured LLM.
creek draft --vault ~/Obsidian/Creek-Vault

# Pick the third-ranked idea and override the phase.
creek draft --vault ~/Obsidian/Creek-Vault --index 2 --phase peaking

# Prepend a voice-core text file to the prompt.
creek draft --vault ~/Obsidian/Creek-Vault --voice-core ./voice-core.md
```

Each draft carries full provenance frontmatter:

```yaml
draft:
  source_idea: idea-7c3a8d
  strategy: liminal-cross-eddy
  contributing_fragments:
    - frag-9c1f3a2b8e02
    - frag-5d4e9c1a7f31
    - frag-2a6b8e3c9d44
  skill_stack:
    - skills/frequencies/amplitude
    - skills/phases/cresting
    - skills/modes/reflective
    - skills/registers/intimate
    - skills/meta/voice-core
  llm:
    provider: ollama
    model: llama3.1
  generated_at: 2026-04-28T17:50:00Z
```

This means every draft can be **re-run** later from the same seed and skill stack — useful for tracking how the same idea drafts differently as the vault grows.

## Local-first ergonomics

All generation flows respect the privacy tier configuration. By default:

- `intimate` fragments are **excluded** from prompts entirely.
- `personal` fragments contribute summaries, not full bodies.
- `open` fragments contribute full content.

You can override with `--include-tier intimate` if you genuinely want intimate content in the prompt — the override is logged in the audit trail.

## Common patterns

```bash
# Weekly cadence.
creek classify --vault ~/Obsidian/Creek-Vault --method rules
creek link     --vault ~/Obsidian/Creek-Vault --method embeddings
creek report   --type wavelength --period weekly --vault ~/Obsidian/Creek-Vault
creek mine     --vault ~/Obsidian/Creek-Vault --limit 10

# When you want to publish.
creek skills --generate --vault ~/Obsidian/Creek-Vault    # refresh the tree
creek draft  --vault ~/Obsidian/Creek-Vault --index 0     # draft top idea
$EDITOR ~/Obsidian/Creek-Vault/07-Voice/Drafts/2026-04-28-*.md
```
