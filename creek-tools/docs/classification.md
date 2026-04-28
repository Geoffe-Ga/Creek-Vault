# Classification

Classification tags every fragment along five dimensions: **frequency** (the APTITUDE 10-frequency system), **archetypal phase** (Origins, Rising, Peaking, Cresting, Receding, Composting), **mode**, **register**, and **privacy tier**. The classifier writes its decisions into the fragment's frontmatter; downstream stages (linking, generation) consume those tags.

## Two methods

`creek classify --method <method>` accepts:

| Method   | Backend                        | When to use |
|----------|--------------------------------|-------------|
| `rules`  | Heuristic pattern matchers     | Default. Cheap, deterministic, runs offline. Captures roughly 70% of fragments confidently. |
| `llm`    | Ollama (default) or Anthropic  | For the long tail of ambiguous fragments. Slower, requires either a local model or `ANTHROPIC_API_KEY`. |

A common workflow: run `--method rules` over the whole vault first, then run `--method llm` only on fragments whose classification is `unclassified` or whose confidence is below `ClassificationConfig.review_threshold`.

```bash
# Rules pass.
creek classify --vault ~/Obsidian/Creek-Vault --method rules

# LLM pass (only touches unclassified / low-confidence fragments).
creek classify --vault ~/Obsidian/Creek-Vault --method llm --batch-size 25
```

## What gets written

For each fragment the classifier appends frontmatter like:

```yaml
classification:
  frequency: amplitude       # one of the 10 APTITUDE frequencies
  phase: rising              # one of 6 archetypal phases
  mode: solo                 # solo | dialogue | reflective | analytic
  register: intimate         # intimate | personal | public | professional
  confidence: 0.82
  method: rules              # rules | llm | manual
  classified_at: 2026-04-28T17:30:00Z
privacy:
  tier: personal             # open | personal | intimate
  reasoning: "Mentions financial detail; default-personal."
```

Privacy tiers are enforced by `creek.classify.privacy.PrivacyClassifier`. The default policy is **fail-closed**: ambiguous fragments are tagged `personal` (not `open`), and `intimate` requires explicit signals.

## Configuration

`ClassificationConfig` in `<vault>/00-Creek-Meta/config.yaml`:

```yaml
classification:
  method: rules               # default for `creek classify` and `creek process`
  batch_size: 50
  review_threshold: 0.6       # below this -> review queue
  llm:
    provider: ollama          # or "anthropic"
    model: llama3.1
    max_retries: 3
  rules:
    frequency_keyword_path: 06-Frequencies/_keyword_atlas.yaml
```

The keyword atlas is a YAML file you maintain — it maps phrases / terms / metaphors to their primary frequency. The atlas ships with a starter, but tuning it to your vocabulary is one of the highest-leverage things you can do.

## The review queue

Anything with `confidence < review_threshold` is added to the **review queue**:

```bash
creek review --vault ~/Obsidian/Creek-Vault
```

`creek review` prints a TUI of pending fragments, lets you accept / override / defer each one, and writes the human decisions back to frontmatter as `method: manual`. Manual decisions are stable across re-classification — `creek classify` will not overwrite a `method: manual` field unless you pass `--force`.

## LLM provider details

### Ollama (default, local)

Make sure `ollama` is running and the model is pulled:

```bash
ollama serve &
ollama pull llama3.1
```

Configure the model name in `LLMConfig.model`. Latency on a CPU is ~2–4 s per fragment; expect a few hours for a vault of 10k fragments.

### Anthropic API (opt-in)

Set `LLMConfig.provider: anthropic` and export `ANTHROPIC_API_KEY`. The provider uses `claude-haiku-4-5` by default for cost reasons; bump to `claude-sonnet-4-6` for higher accuracy.

The Anthropic path is **not** the default — opt in deliberately. Cost on a 10k-fragment vault is roughly $1–3 with Haiku, $10–30 with Sonnet.

## Privacy tiers in detail

| Tier       | Default fields shown | Default fields redacted | Use case |
|------------|---------------------|-------------------------|----------|
| `open`     | All                 | Patterns from `RedactionConfig` | Public writing, blog drafts, technical notes. |
| `personal` | All                 | + named entities, locations, financial detail | Daily notes, journals, decisions. |
| `intimate` | Title + tags only   | + body                  | Therapy / health / relationship reflections. |

Generation flows (`mine`, `draft`, `report`) honour the tier: `intimate` fragments are excluded by default; `personal` fragments are included but have body text replaced with summaries; `open` fragments pass through unredacted. Override with `--include-tier intimate` if you genuinely want intimate fragments fed to the LLM (this is logged in the audit trail).

## Re-classifying after taxonomy changes

Edit `06-Frequencies/_keyword_atlas.yaml`, then re-run with `--force` so the rule classifier overwrites the prior decisions:

```bash
creek classify --vault ~/Obsidian/Creek-Vault --method rules --force
```

`method: manual` decisions are still preserved even with `--force`.
