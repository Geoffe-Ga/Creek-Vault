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
| `decision`       | `08-Decisions/<fragment>.md`               | `creek.generate.decisions` — see [`decisions.md`](decisions.md) |
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

The `synchronicity`, `paradox`, `compost`, `unnamed`, and `tags` report types collectively make up the **emergence infrastructure** described in Ontology §10. The exact criteria that decide whether content is surfaced (similarity thresholds, time-gap floors, project-name filters) live in [`emergence.md`](emergence.md).

## State (audit report)

`creek state` writes a single weekly snapshot of the entire vault to `00-Creek-Meta/State/<iso-year>-W<week>.md` (with `latest.md` always pointing at the most recent run). It is a **view**, not a pipeline — it re-reads existing fragments, threads, eddies, praxis, synchronicities, and the latest `run-summary.jsonl` line, and never invokes classify, link, or compile.

```bash
creek state --vault ~/Obsidian/Creek-Vault
```

The report is organised in eleven sections, in this order (FEAT-006 + FEAT-007 + FEAT-008):

1. **Wavelength snapshot** — current dominant phase, mode, dosage shares, and any detected transitions over the trailing 28 days. Placed first because phase context interprets every other section (FEAT-007).
2. **Vault summary** — fragment / eddy / thread counts plus the per-frequency distribution (sorted F1..F10, then UNCLASSIFIED).
3. **Pre-LLM yield** — the most recent line of `00-Creek-Meta/Processing-Log/run-summary.jsonl` (deterministic / local-model / residue counts and the `--no-llm` flag).
4. **Liminal Watch** — recently surfaced content under `10-Liminal/Unnamed/` and `10-Liminal/Paradoxes/` (FEAT-007).
5. **Active eddies** — top 10 by `fragment_count`.
6. **Active threads** — top 10 by `last_seen` recency.
7. **Surprising connections** — synchronicities discovered under `10-Liminal/Synchronicities/`.
8. **Hyperedges** — praxis whose `derived_from` fragments span two or more eddies.
9. **Drift warnings** — broken wiki-links and stale (link-isolated, ≥90-day-old) fragments.
10. **Suggested questions** — 4-5 phase-aware essay prompts derived from `creek.generate.mining.phase_filtered_seeds`. Bottoming Out / Diminishing / Withdrawal surface compost and synchronicity prompts; Rising and Peaking surface drafting prompts; Restoration mixes both (FEAT-007).
11. **Lint summary** — the most recent `creek lint` summary, appended verbatim (FEAT-008).

Empty sections render an explicit `_No surfacing this week._` placeholder rather than disappearing — operators can tell at a glance whether a section had nothing to surface or whether the generator skipped it. Re-running in the same ISO week overwrites the existing file (idempotent).

`latest.md` is created as a symlink where the filesystem supports it (POSIX hosts) and falls back to a copy on Windows or networked filesystems that reject `symlink(2)`. The report header renders the vault's leaf directory name only — never the absolute path — so committing or sharing the artefact does not leak an operator's home directory.

### Size budget (FEAT-007)

`latest.md` is the session-start context for CrawDad and Claude Code: it must fit in a single Claude context window. `./scripts/check-all.sh` enforces a **50,000-token budget** (≈200KB at a conservative four-characters-per-token estimate) via the `creek state-budget` command:

```bash
# Standalone budget check (set CREEK_VAULT or pass --vault).
creek state-budget --vault ~/Obsidian/Creek-Vault
```

A budget failure is **not** a cap to raise. It is a quality signal that the compiled layer is fragmenting; the fix is consolidation via `creek lint`'s synchronicity and tag-cluster checks, not a higher cap. The failure message names the three largest sections so the operator can see at a glance which surface is overgrowing.

## Voice Skill Tree

`creek skills` writes a tree of `SKILL.md` files under `<output>` (default `<vault>/creek-skills/`):

```
creek-skills/
├── frequencies/
│   ├── F1/SKILL.md
│   ├── F2/SKILL.md
│   └── …                                           # F1..F10 (APTITUDE)
├── phases/{rising,peaking,withdrawal,diminishing,bottoming_out,restoration}/SKILL.md
├── modes/{inhabit,express,collaborate,integrate,absorb}/SKILL.md
├── registers/{confessional,analytical,playful,prophetic,instructional,raw,conversational}/SKILL.md
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

# Filter to ideas that fit a current Withdrawal phase.
creek mine --vault ~/Obsidian/Creek-Vault --phase withdrawal --limit 5
```

Each `IdeaSeed` has a strategy, a score, the contributing fragments, and a hint about the angle. You'll typically pick one (`--index N`) to feed into `creek draft`.

### Compiled-layer routing (FEAT-004)

`creek mine` and `creek draft` route through the compiled layer first.
For every thread, eddy, or frequency the strategy needs, the miner
reads the relevant `02-Threads/` / `03-Eddies/` / `06-Frequencies/`
page (produced by `creek compile`, FEAT-003) and uses its body and
provenance instead of rescanning raw fragments. When a compiled page
is missing the miner falls back to fragments and appends a
`compile-needed` entry to `<vault>/00-Creek-Meta/Processing-Log/
compile-gaps.jsonl`; `creek lint` surfaces these gaps later so the
operator can recompile.

The contract is the four-verb rule documented in
`00-Creek-Meta/Skills/query.SKILL.md` (compile-then-query): compile
once, read many. Routine reads should not bypass it.

The escape hatch is `--bypass-compiled` on both commands:

```bash
# Diagnostic: read fragments directly, skipping the compiled layer.
creek mine  --vault ~/Obsidian/Creek-Vault --bypass-compiled
creek draft --vault ~/Obsidian/Creek-Vault --bypass-compiled --index 0
```

When set, the flag emits a stderr warning ("the compiled-layer-first
contract is being side-stepped"). Use it for verification — for
example, comparing a compiled page against its source fragments — not
as a default mode of operation.

## Drafting

`creek draft` takes a mined idea, assembles the skill stack (frequency + phase + mode + register skills, plus the voice-core meta skill), gathers the source material (compile-first: the relevant compiled-layer page bodies, with fragments retained for exact-quote provenance traversal), prompts the LLM, and saves the draft to `07-Voice/Drafts/<date>-<slug>.md`.

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
    - skills/frequencies/F1
    - skills/phases/withdrawal
    - skills/modes/integrate
    - skills/registers/confessional
    - skills/meta/voice-core
  llm:
    provider: ollama
    model: mistral
  generated_at: 2026-04-28T17:50:00Z
```

This means every draft can be **re-run** later from the same seed and skill stack — useful for tracking how the same idea drafts differently as the vault grows.

### Voice fidelity (FEAT-040)

The goal of generation is text that sounds like **you**, measured from your own writing — not text that merely avoids a generic "AI" checklist. Three pieces make that vault-relative:

- **The fingerprint.** `creek report --type fingerprint` profiles your genuinely-authored vault content into a `VoiceFingerprint` (`00-Creek-Meta/voice-fingerprint.json`): a per-feature rate (em-dash density, transition-opener rate, AI-vocabulary rate, triad rate, …) plus the fragment support behind each. An **authorship filter** weights sources by how much of the text is really yours — journal and long-form markdown count fully; only the *your-turn* side of chat exports survives, and at a lower weight — so the baseline reflects you, not the assistants you talked to.
- **The guard.** During `creek draft`, after composition the draft is sanitized, scored for **voice distance** against the fingerprint, and — when distance exceeds `ai_style.voice_distance_upper` — rewritten *toward* your measured voice (bounded, regression-guarded, and never at the cost of grounding). Pass `--no-llm` to sanitize and measure only, with no rewrite hop.
- **The stamp.** The final `voice_distance`, the residual `voice_findings`, and a machine-readable `voice_guard_status` are written into the draft's frontmatter, so the `voice-fidelity` lint check (and you) can later see how far each draft sits from your voice without re-measuring.

Three refinements make the proxy reflect the *right* you:

- **Audience-weighted authority.** Voice exemplars are ranked by a graduated multiplier (`voice_audience_weighting`): public-facing (`OPEN`) work outranks `PERSONAL` writing, and the `representativeness` axis (self → endorsed → aspirational → reference) further tunes influence, so the patterns of your published work dominate how drafts sound. `INTIMATE` stays excluded.
- **Citation density.** The proxy measures how often you reference and quote sources (quotation spans, attribution phrases, `[1]` / author-year markers, links) — audience-weighted so the heavy-citation habit of public work shapes drafts. When it is prevalent, the generated voice skill says so explicitly.
- **A faithful, loud de-slop pass.** The rewrite loop drives toward `ai_style.voice_distance_target` (distinct from, and never above, the `voice_distance_upper` ceiling), so a mannered draft below the ceiling is still stripped — not merely measured. Every outcome stamps `voice_guard_status`: `rewritten`, `measured_only:{below_target,above_target,no_llm,no_rewriter}`, or `skipped:{disabled,no_fingerprint,thin_fingerprint}`. A skip is loud (stderr + frontmatter), never silent.

**The diagnostic.** `creek voice-authenticity --vault <path> [--draft <file>] [--json]` is a read-only audit: it reports the audience mix and whether weighting is active, the AI-corpus-leak fraction (claude/chatgpt content still feeding the proxy), and — given `--draft` — the recorded de-slop status. Use it after a `--refresh-ai-chat` migration to confirm the leak dropped.

> **Voice distance is not an AI detector.** It is a *probabilistic, vault-relative* signal — how far a draft sits from **your own measured writing**, not whether text "is AI". A high distance is a prompt to revise toward your voice, never proof of authorship. Humans and tools are unreliable at AI detection; these scores are for *your* review, never an accusation.

## Local-first ergonomics

All generation flows (`mine`, `draft`, `report`, `skills`) respect the privacy tier configuration via the shared filter in `creek/classify/privacy_filter.py`. By default:

- `intimate` fragments are **excluded** from prompts entirely.
- `personal` fragments contribute a title-only summary, not the full body.
- `open` (or `public`) fragments contribute full content.
- `unclassified` fragments are treated as `personal` — a title-only summary, same as `personal` (changed in #876; they used to pass through with their full body). Untiered content is content nobody has vouched for, so an unreviewed body can no longer reach an LLM prompt by default. Since #961 the MCP read surface agrees: an untiered fragment needs a `personal` ceiling there too. **Run `creek classify` before generation flows** so each fragment carries an explicit tier — `creek fill` prints a hint counting the untiered fragments it finds. Fragments carrying a tier value the classifier doesn't recognise (e.g. hand-edited frontmatter, a forward-incompatible schema migration) fail **closed** to `intimate` and emit a warning that names the fragment ID.

You can override with `--include-tier {open,personal,intimate,all}` on any of those commands. The default (`open` or omitting the flag) keeps the policy above. `personal` lets personal bodies through unredacted; `intimate` and `all` let intimate bodies through as well. Any value that elevates inclusion above the default appends an entry to `<vault>/00-Creek-Meta/audit/privacy.jsonl` capturing the operator, command, fragment IDs, and timestamp.

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
