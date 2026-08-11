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
