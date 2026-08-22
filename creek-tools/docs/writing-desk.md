# Writing Desk

The Writing Desk (`creek author`) turns a classified, linked vault into a
grounded draft written in the vault owner's voice. Where `creek draft` (see
[`generation.md`](generation.md)) expands a single mined idea into an essay, the
Writing Desk runs a multi-agent pipeline: specialists gather structured
evidence, a synthesis step grounds it, a voice agent renders it in the owner's
register, and a reflection node judges the result against a rubric — looping or
escalating rather than shipping a sub-threshold draft.

## What the desk is

The desk is a `Conductor` that orchestrates four kinds of collaborator:

- **Specialists** — `graph`, `retrieval`, and `ontology`. Each reads the vault
  and returns *structured evidence* (claims traced to fragment ids), never free
  prose.
- **Synthesis** — merges every specialist's bundle into one grounded bundle.
  Any claim that does not trace to a source fragment is dropped (and logged),
  never fabricated.
- **Voice** — renders the grounded evidence into draft prose in the owner's
  voice, conditioned by the `creek-skills` voice-skill stack.
- **Reflection** — judges the drafted body against the six-dimension rubric and
  returns `PASS`, `REVISE`, or `ESCALATE`.

## CLI usage

```bash
# Author a research answer from the vault.
creek author --query "What is F6 Pluralism?" --medium research --vault ~/Obsidian/Creek-Vault

# Preview the pipeline and evidence counts without authoring (dry run).
creek author --query "What is F6 Pluralism?" --medium research --vault ~/Obsidian/Creek-Vault --dry-run
```

`--query` is required for every medium except `book-report`, which derives its
query from a `--work` path (an explicit `--query` is appended when given).

### Mediums

The wired medium set is:

| `--medium`        | Use |
|-------------------|-----|
| `research`        | A grounded answer to a question; voiced with a light touch. |
| `research-piece`  | A longer research treatment; also light-touch voiced. |
| `chat`            | A short conversational reply (post-generation character ceiling). |
| `essay`           | A full essay in the owner's voice. |
| `book-report`     | A report derived from a `--work` path. |
| `how-to`          | A procedural guide. |

An unwired medium is rejected with an error naming the supported set.

## The pipeline

For one run the conductor:

1. Runs the specialist roster (`graph` → `retrieval` → `ontology`), then the
   `synthesize` step, producing one grounded `EvidenceBundle`.
2. Enters the voice/reflect loop:
   - **voice** renders the grounded evidence into a draft body.
   - **reflect** judges that body and returns a verdict.
   - On `REVISE` the loop retries, up to `max_author_rounds`.
3. **Bounded retries → escalate.** A draft still in `REVISE` once the round
   budget is exhausted is escalated to a human (`ESCALATE`) rather than
   shipped. The final round's reflection findings are carried on the draft so
   the escalation is actionable.

`creek author --dry-run` runs the specialists and synthesis step only and
reports the plan plus evidence counts; it does not enter the voice/reflect loop
and produces no draft.

## Attribution

Content borrowed from other authors lives under `11-Other-Authors/<slug>/` and
travels with an `author_slug`. The voice agent excludes borrowed claims from the
owner-voiced material, so the owner's voice never presents another author's
words as its own. Borrowed entries carry a `voice_weight` (default `0.0`),
meaning a borrowed fragment contributes nothing to the generated voice unless an
operator deliberately raises it.

## Reflection dimensions and escalation

The reflection node scores a draft across six rubric dimensions:

- `voice_fidelity`
- `ontological_accuracy`
- `citation_completeness`
- `privacy_compliance`
- `paradox_preservation`
- `attribution_correctness`

`privacy_compliance` is a HARD gate, and it polices **every** subtree the
specialists draw evidence from — `01-Fragments`, `09-Reference` and
`11-Other-Authors`. Both sides read one definition of that list
(`creek.vault.reader.CORPUS_SUBDIRS`), so the gate cannot fall behind the
specialists it polices. A cited fragment id present in more than one subtree
resolves to its **most restrictive** tier, and every body stored under that id
is checked against the draft — the lower-tier twins' bodies included, because
the tier is a property of the id rather than of the file that happened to win.
The ceiling a cited fragment's tier is checked against has two sources and is
the more restrictive of them — see [The reproduction
ceiling](#the-reproduction-ceiling) below.

A clean draft returns `PASS`. One or more findings return `REVISE`, which the
conductor retries within the round budget. A draft that cannot be authored at
all — or that never clears `REVISE` before the budget is exhausted — returns
`ESCALATE`, with the offending findings attached for a human to act on.

> Note: `voice_fidelity` is implemented in the reflection node but stays dormant
> in the desk today — the owner's voice fingerprint is not yet threaded into the
> run, so this dimension does not yet fire end-to-end.

## Configuration

Desk settings live under the `author:` section of `creek_config.yaml`
(`AuthorConfig`):

- `max_author_rounds` — maximum voice/reflect rounds before escalation, bounded
  `[1, 10]` (default `3`).
- `graph_breadth_bound` — max fragments the Graph agent expands per depth level
  (default `25`).
- `graph_depth_bound` — max backlink hops from the seed (default `2`).
- `retrieval_top_k` — how many top-ranked fragments the Retrieval agent surfaces
  (default `5`).
- `max_reproduced_tier` — the highest privacy tier a finished draft may
  reproduce **verbatim** (default `"open"`, the strictest rank). See [The
  reproduction ceiling](#the-reproduction-ceiling) below.

### The reproduction ceiling

`author.max_reproduced_tier` (values `open` | `personal` | `intimate` |
`unclassified`, default `"open"`) is the operator's half of the
`privacy_compliance` gate's ceiling. The gate enforces the **more
restrictive** of this key and the medium contract's `default_privacy_tier` —
"more restrictive" is the **lower** rank in the tier ordering `open` (0) <
`personal` (1) < `intimate` (2) < `unclassified` (3), so `open` is the
strictest possible ceiling, not the loosest. A tie between the two sources
(both `open`, at the shipped default) goes to `author.max_reproduced_tier`. An
unrecognised or null value fails closed to `open`, with a warning logged —
never a crash inside a HARD gate.

**No trust boundary is created by this key.** `creek_config.yaml` lives at
`<vault>/00-Creek-Meta/creek_config.yaml` — *inside* the vault, editable by
exactly the same actor who can edit the medium contract at
`00-Creek-Meta/Skills/mediums/<medium>.MEDIUM.md`. This change does not make
widening the reproduction ceiling impossible; it makes it deliberate,
single-sourced, and self-evidently security-relevant — a change to
`author:` in `creek_config.yaml`, not an incidental YAML edit buried in a
medium's contract.

**This does take the widening lever out of `creek skills sync`'s drift
surface.** That command's drift guard covers `00-Creek-Meta/Skills/` only, so
moving the permissive lever into `creek_config.yaml` moves it outside that
detection entirely. `creek skills sync` is not a mitigation here: since #1306
it *refuses* to sync when it detects medium drift, and a refusal **leaves a
tampered contract in place** rather than repairing it.

**All six shipped medium templates declare `default_privacy_tier: open`** —
already the strictest rank — so at the shipped default,
`MediumContract.default_privacy_tier` is **inert** for this gate on every
shipped medium: it narrows nothing until an operator deliberately raises
`max_reproduced_tier` above `open`. The field is retained on purpose and is
not dead: once `max_reproduced_tier` is raised — e.g. to `intimate` — a
contract declaring `personal` narrows the effective ceiling to `personal` for
that medium. The escape hatch for a medium that may legitimately reproduce
above-`open` text is `author.max_reproduced_tier`, and only that: it is
reachable by an operator editing `creek_config.yaml`, never by a skill file
that any template deploys by default.

**This is a different axis from admission.** `creek author --include-tier`
and the MCP `privacy_tier_ceiling` parameter govern what the desk's
specialists may *retrieve* as evidence from the vault. `max_reproduced_tier`
governs what the finished prose may *reproduce verbatim* once retrieved. A
specialist admitted to read intimate content under a broad `--include-tier`
can still be blocked, at reflection time, from shipping that content's exact
words in the draft.

### Model tiers

The desk's LLM model id is never hard-coded. By default the voice call uses the
shared `llm.model`. Per-agent overrides let you point one agent at a different
tier:

- `voice_model` — model for the voice call. `None` (default) falls back to
  `llm.model`.
- `synthesis_model` / `reflection_model` — reserved overrides. Synthesis and
  reflection are deterministic today (no LLM call), so these are documented but
  dormant; they fall back to `llm.model` and nothing reads them yet.

Example `creek_config.yaml` excerpt:

```yaml
llm:
  provider: anthropic
  model: claude-sonnet-4-6

author:
  max_author_rounds: 3
  graph_breadth_bound: 25
  graph_depth_bound: 2
  retrieval_top_k: 5
  # Highest tier a finished draft may reproduce verbatim. "open" (the
  # default) is the strictest rank, and this key is vault-wide: raising it
  # lifts the floor for every medium, and each contract narrows from there.
  max_reproduced_tier: open
  # Point the owner-voice render at a different tier than the rest of the
  # pipeline. Unset (the default) reuses llm.model above.
  voice_model: claude-opus-4-1
```

## Prompt caching and cost

The voice call sends the static `creek-skills` voice-skill sections as a cached
`system` block (Anthropic prompt caching, ephemeral cache control), while the
per-query evidence and ask form the dynamic user prompt. The cached prefix is
billed once and re-read cheaply on subsequent runs against the same vault — the
content the model sees is unchanged, only how it is billed.

Each run surfaces the voice call's token usage on `AuthoredDraft.usage`:

```python
draft = run_author(medium="research", query="What is F6?", vault=vault)
draft.usage  # e.g. {"input_tokens": 50, "output_tokens": 120,
             #       "cache_creation_input_tokens": 40, "cache_read_input_tokens": 38}
```

`usage` carries `input_tokens` and `output_tokens`, plus
`cache_creation_input_tokens` / `cache_read_input_tokens` when prompt caching is
active. A `cache_read_input_tokens` above zero on a repeated run confirms the
static prefix was served from cache. `usage` is `None` when a run takes the
deterministic, offline path (no LLM client wired), which is the default for
`run_author` / the CLI today — the network seam is exercised by injecting a
client into the `Conductor`.
