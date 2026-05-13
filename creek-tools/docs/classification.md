# Classification

Classification tags every fragment along five dimensions: **frequency** (the APTITUDE F1–F10 system), **archetypal phase** (Rising, Peaking, Withdrawal, Diminishing, Bottoming Out, Restoration), **mode** (Inhabit, Express, Collaborate, Integrate, Absorb), **register**, and **privacy tier**. The classifier writes its decisions into the fragment's frontmatter; downstream stages (linking, generation) consume those tags.

> Routing fragments through the Anthropic provider sends content to a third party. The privacy-tier system gates this — `intimate` fragments stay local — but the broader trade-offs and the hardening done against prompt-injection (SEC-004) are documented in the [threat model](security/threat-model.md).

## Two methods

`creek classify --method <method>` accepts:

| Method   | Backend                        | When to use |
|----------|--------------------------------|-------------|
| `rules`  | Heuristic pattern matchers     | Default. Cheap, deterministic, runs offline. Captures roughly 70% of fragments confidently. **This is Pass 1 work — no network egress.** |
| `llm`    | Ollama (default) or Anthropic  | For the long tail of ambiguous fragments. Slower, requires either a local model or `ANTHROPIC_API_KEY`. **This is Pass 3 work — the only pass that leaves the machine when the Anthropic provider is selected.** |

A common workflow: run `--method rules` over the whole vault first, then run `--method llm` only on fragments whose classification is `unclassified` or whose confidence is below `ClassificationConfig.confidence_threshold`.

Run `creek process --no-llm` for an end-to-end run that completes Passes 1 and 2 but never invokes the LLM — every fragment the rules left uncertain shows up as **residue** in the run summary instead. See [the three-pass pipeline](configuration.md#the-three-pass-pipeline) in the configuration reference for the full vocabulary.

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
  frequency: F1              # one of F1..F10 (APTITUDE)
  phase: rising              # rising | peaking | withdrawal | diminishing | bottoming_out | restoration
  mode: inhabit              # inhabit | express | collaborate | integrate | absorb
  orientation: do            # do | feel | do_feel
  dosage: medicine           # medicine | toxic | ambiguous
  register: confessional     # confessional | analytical | playful | prophetic | instructional | raw | conversational
  confidence: 0.82
  method: rules              # rules | llm | manual
  classified_at: 2026-04-28T17:30:00Z
privacy:
  tier: personal             # open | personal | intimate
  reasoning: "Mentions financial detail; default-personal."
classification_reasoning: |  # FEAT-017: present only for --method llm at open/personal tiers
  This is F3 because the operator owns the decision; rising phase because the
  energy is accelerating into commitment; express mode because the speech is
  outward; analytical register and forming confidence.
```

Privacy tiers are enforced by `creek.classify.privacy.PrivacyClassifier`. The default policy is **fail-closed**: ambiguous fragments are tagged `personal` (not `open`), and `intimate` requires explicit signals.

## The two-step LLM pipeline (FEAT-017)

`--method llm` runs use a chain-of-thought prompt that asks the model first for a short reasoning trace, then for the YAML tuple. The classifier splits the response into:

- a **reasoning preamble** persisted as `classification_reasoning` in fragment frontmatter (truncated to 400 characters) for `open` and `personal` tiers, and
- a **YAML payload** whose fields drive the standard `classification:` block.

For `intimate`-tier fragments the reasoning preamble is **never** stored in the fragment file. The full trace lands in `<vault>/00-Creek-Meta/Processing-Log/classify-llm-trace.jsonl` — gitignorable per FEAT-019, so vault sharing cannot leak intimate-tier model traces.

The prompt also embeds a **few-shot example block** sampled deterministically per fragment ID from `creek/classify/examples/<dimension>.yaml`. Same fragment → same examples on every re-run (reproducible classifications); different fragments rotate through the corpus.

### Default-unclassified bias

The new prompt asks the model to emit a `confidence_scores` map alongside its picks:

```yaml
confidence_scores:
  mode: 0.7
  orientation: 0.4
  dosage: 0.9
```

For **Mode**, **Orientation**, and **Dosage** specifically, any reported confidence below `LLMConfig.unclassified_threshold` (default `0.55`) downgrades the model's pick to `unclassified` rather than guessing. **Frequency**, **Phase**, and **Voice Register** are not gated by this bias — they are more stable signals empirically and using them is the point of running an LLM pass.

Per-fragment LLM classification is inherently noisy. The reliable wavelength signal is the **weekly aggregation** of many fragments (consumed by the `creek state` audit report in FEAT-006/007), not any single-fragment certainty.

### Recommended model floor

Mistral via Ollama and Haiku via Anthropic are too small for reliable 7-dimensional picks on subtle content. Reach for **Sonnet** (`claude-sonnet-4-6`) when classification quality matters. The prompt itself is model-agnostic; only the achievable agreement rate changes.

## Configuration

The relevant sections of `<vault>/00-Creek-Meta/creek_config.yaml` are `classification` (`ClassificationConfig`) and `llm` (`LLMConfig` — top-level, not nested):

```yaml
classification:
  confidence_threshold: 0.7              # below this -> review queue
  auto_classify_sources: [claude, chatgpt, discord]
  human_review_sources: [journal]

llm:
  provider: ollama                       # or "anthropic"
  model: mistral
  batch_size: 50
  max_concurrent: 5
  unclassified_threshold: 0.55           # FEAT-017: bias for Mode / Orientation / Dosage
```

The CLI selects rules vs LLM via `--method`. Rule-based classification reads frequency / phase keyword atlases bundled with the package; you can override or extend them at runtime by editing the relevant module data.

## Calibration (FEAT-017b)

Per-fragment LLM classification is noisy. To turn that noise into a gauge an operator can act on, `creek classify --calibrate` runs the configured LLM against a hand-labelled fixture and prints per-dimension agreement rates:

```bash
creek classify --calibrate \
  --calibration-fixture tests/fixtures/classification/calibration_set.yaml
```

The fixture (`tests/fixtures/classification/calibration_set.yaml`) ships with ≥30 hand-labelled entries covering every Frequency, Phase, Mode, Orientation, Dosage, and Voice Register value. Re-label entries or add to the set as your corpus's tone drifts.

Add `--enforce-floors` to exit non-zero when any dimension regresses below the FEAT-017 floor — drop this in CI on PRs that touch `creek/classify/`:

```bash
creek classify --calibrate --enforce-floors
```

### Default per-dimension floors (`creek.classify.calibration.DEFAULT_FLOORS`)

| Dimension       | Floor | Rationale |
|-----------------|------:|-----------|
| Frequency       |  60%  | F1..F10 is high-cardinality but the values are well-separated. |
| Phase           |  60%  | Six values, fairly stable signal. |
| Mode            |  40%  | Subject to the FEAT-017 unclassified bias; lenient on purpose. |
| Orientation     |  40%  | Three values but the boundary is fuzzy in real text. |
| Dosage          |  40%  | Medicine / Toxic / Ambiguous is genuinely hard from the body alone. |
| Voice Register  |  75%  | Most stable signal — the tone usually leaks into the first sentence. |

These are starter values calibrated for the v1.0 prompt + few-shot set against Sonnet. A model swap or a major prompt edit warrants re-running the calibration to re-baseline. Adjust `DEFAULT_FLOORS` in the source rather than monkey-patching it at runtime.

### Cadence

- **PR-time (CI):** the calibration code is exercised by `tests/test_calibration.py` against a deterministic stub so the scoring mechanism itself can never silently regress. Real-LLM calibration is operator-run because CI does not carry API keys.
- **Operator-run (`--calibrate`):** run after every prompt edit, every fixture addition, every model swap. Compare the new per-dimension rates against the prior run committed in the fixture.

## The review queue

Anything with `confidence < classification.confidence_threshold` is added to the **review queue**:

```bash
creek review --vault ~/Obsidian/Creek-Vault
```

`creek review` prints a TUI of pending fragments, lets you accept / override / defer each one, and writes the human decisions back to frontmatter as `method: manual`. Manual decisions are stable across re-classification — `creek classify` will not overwrite a `method: manual` field unless you pass `--force`.

## Crash recovery and resume (OPS-001)

LLM classification of a 10k-fragment vault takes hours. To make a partial run survivable, `creek classify` writes each fragment back to disk **the moment the LLM call returns** — not at end-of-batch. If the process is killed (laptop closed, network blip, OOM), every fragment classified up to that point keeps its result on disk.

Re-running `creek classify --method llm` after a crash is therefore the resume command — there is no separate `--resume` flag. The engine skips any fragment whose `classification_method` is already `llm` (or `manual`); only fragments still at `rules` or unclassified are sent to the provider. This means you do not pay the Anthropic provider twice for the same fragment.

Pass `--force` if you genuinely want to re-classify everything (for example, after a model upgrade).

The engine also appends each classified fragment ID to `<vault>/00-Creek-Meta/Processing-Log/llm-progress.jsonl` for observability (newline-delimited JSON, one `{"id": ...}` object per line). The file is informational — the per-fragment frontmatter is the source of truth.

## LLM provider details

### Ollama (default, local)

Make sure `ollama` is running and the model is pulled:

```bash
ollama serve &
ollama pull mistral
```

Configure the model name in `LLMConfig.model` (default `mistral`). Latency on a CPU is ~2–4 s per fragment; expect a few hours for a vault of 10k fragments.

### Anthropic API (opt-in)

Set `LLMConfig.provider: anthropic` and export `ANTHROPIC_API_KEY`. Set `LLMConfig.model` to the canonical model ID — `claude-haiku-4-5-20251001` for cost-sensitive runs, `claude-sonnet-4-6` for higher accuracy.

The Anthropic path is **not** the default — opt in deliberately. Cost on a 10k-fragment vault is roughly $1–3 with Haiku, $10–30 with Sonnet.

## Privacy tiers in detail

| Tier       | Default fields shown | Default fields redacted | Use case |
|------------|---------------------|-------------------------|----------|
| `open`     | All                 | Patterns from `RedactionConfig` | Public writing, blog drafts, technical notes. |
| `personal` | All                 | + named entities, locations, financial detail | Daily notes, journals, decisions. |
| `intimate` | Title + tags only   | + body                  | Therapy / health / relationship reflections. |

Generation flows (`mine`, `draft`, `report`, `skills`) honour the tier via the shared filter in `creek/classify/privacy_filter.py`: `intimate` fragments are excluded by default; `personal` fragments are included with their body replaced by a title-only summary; `open` (or `public`) fragments pass through unredacted. Override with `--include-tier {open,personal,intimate,all}` if you genuinely want a richer scope; any value above the default writes an audit entry to `<vault>/00-Creek-Meta/audit/privacy.jsonl`.

## Re-classifying after taxonomy changes

Edit `06-Frequencies/_keyword_atlas.yaml`, then re-run with `--force` so the rule classifier overwrites the prior decisions:

```bash
creek classify --vault ~/Obsidian/Creek-Vault --method rules --force
```

`method: manual` decisions are still preserved even with `--force`.
