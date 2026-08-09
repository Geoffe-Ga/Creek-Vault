# Configuration

`creek-tools` reads its configuration from `<vault>/00-Creek-Meta/creek_config.yaml`. The file is parsed into the Pydantic models defined in [`creek/config.py`](../creek/config.py); env-vars prefixed with `CREEK_` override individual fields. **API keys are never stored in the YAML — they come from the environment** (e.g. `ANTHROPIC_API_KEY`).

This page is the field-by-field reference. Every section maps to a `BaseModel` you can read in `config.py`.

## Adding a config field: consumed, or declared dormant

Every field of `CreekConfig` is shipped into a user's vault by `creek init`, so a field that no production code reads is a knob wired to nothing — and `ruff`, `mypy --strict`, `radon` and `interrogate` all pass on one. `tests/test_config_contract.py` is the gate that does not (issue #1042). It derives the field list from the live Pydantic model and asserts each leaf field is either:

- **read** somewhere under `creek/` or `creek_mcp/` — the test resolves attribute chains through annotations, `load_config`-style helpers, aliases, `self` attributes, structural `Protocol`s and dynamic `getattr`; or
- **listed in `DORMANT_CONFIG_FIELDS`** with a reason citing the issue that tracks wiring it in.

The allowlist is a ratchet: a field that is both allowlisted *and* read fails as a stale entry, so the list can only shrink. If you add a field and the contract fails, wire it up or declare it — do not delete the assertion. Fields currently on the allowlist are documented below only as *configured*, never as effective.

## The three-pass pipeline

`creek process` runs in three named passes (FEAT-005); **network egress only in Pass 3.**

| Pass | Scope | What it does |
|------|-------|--------------|
| **Pass 1 — deterministic, local** | No network | Ingestion, redaction, rules-based classification, frontmatter generation. |
| **Pass 2 — local model-based** | No network | Embeddings (sentence-transformers), OCR (pytesseract), future Whisper transcription. Local model inference only. |
| **Pass 3 — network if opted in** | Network | LLM classification of residue (`creek classify --method llm`), LLM-driven compile, lint semantic checks. |

Pass 3 is opt-in per run. Use `creek process --no-llm` to run Passes 1 and 2 to completion and skip Pass 3 entirely; the residue (unclassified or low-confidence fragments) is reported in the run summary. The flag wins over `LLMConfig.provider`, so passing `--no-llm` while `provider: anthropic` is configured is safe — no Anthropic call is ever made.

After every run a one-line summary is appended to `<vault>/00-Creek-Meta/Processing-Log/run-summary.jsonl` and printed to stdout, e.g.

```
Deterministic: 7431 classified | Local-model: 9323 embedded/OCR'd | Residue: 1892 (would go to LLM if Pass-3 enabled)
```

The summary is consumed by the audit report (FEAT-006).

## Top-level

```yaml
vault_path: ~/Obsidian/Creek-Vault
source_drive: ~/exports
timezone: America/Los_Angeles
```

| Field          | Default         | Notes |
|----------------|-----------------|-------|
| `vault_path`   | `.`             | Absolute path to the Obsidian vault. |
| `source_drive` | `.`             | Default `--source` for `creek process` / `creek ingest`. |
| `timezone`     | `America/Los_Angeles` | Used for every `datetime` written into frontmatter. |

## `llm` — language model provider

```yaml
llm:
  provider: ollama
  model: mistral          # optional; omit to use the provider's own default
  ollama_url: http://localhost:11434
  batch_size: 50
  max_concurrent: 5
```

| Field            | Default   | Notes |
|------------------|-----------|-------|
| `provider`       | `ollama`  | `ollama` or `anthropic`. |
| `model`          | *(unset)* | Model id understood by the provider; when omitted each provider uses its own default (`mistral` for Ollama). An explicit value is always sent verbatim. |
| `ollama_url`     | `http://localhost:11434` | Base URL when `provider: ollama`. |
| `batch_size`     | `50`      | Items per batch call. |
| `max_concurrent` | `5`       | Concurrent requests in flight. |

When `provider: anthropic`, set `ANTHROPIC_API_KEY` in the environment. Local Ollama models require nothing in the environment beyond the server being running.

The flat block above is the `default` stage. The `llm` block also accepts **per-stage routing** — a `default` plus optional `classification` / `generation` / `frontend` overrides and a `writing_desk` role map — so classification can run locally while generation uses a cloud model, with `Intimate`-tier fragments guaranteed never to reach a cloud provider. The flat block keeps working unchanged. See [Per-stage model routing](../README.md#per-stage-model-routing) for the full shape, the privacy guarantee, and migration notes.

## `embeddings` — semantic similarity

```yaml
embeddings:
  model: all-MiniLM-L6-v2
  similarity_threshold: 0.75
  cache_dir: null
  batch_size: 32
```

| Field                  | Default              | Notes |
|------------------------|----------------------|-------|
| `model`                | `all-MiniLM-L6-v2`   | Sentence-transformer model. |
| `similarity_threshold` | `0.75`               | Cosine cutoff for a resonance edge. |
| `cache_dir`            | `null`               | Override for the HuggingFace cache. |
| `batch_size`           | `32`                 | Texts per encode batch. |

## `ocr` — image / PDF OCR

```yaml
ocr:
  enabled: true
  engine: pytesseract
  languages: [eng]
  min_confidence: 0.6
```

| Field            | Default        | Notes |
|------------------|----------------|-------|
| `enabled`        | `true`         | If `false`, `creek ingest --type images` will skip OCR. |
| `engine`         | `pytesseract`  | Engine name. (Custom engines are injected at the API level — see `creek.ingest.images.OcrEngine`.) |
| `languages`      | `[eng]`        | Tesseract language codes. |
| `min_confidence` | `0.6`          | Per-page OCR confidence below which the resulting fragment is tagged `review: pending_review` in frontmatter and surfaced by `creek redact --review`. Range `[0.0, 1.0]`. |

## `linking` — resonance / thread / eddy thresholds

```yaml
linking:
  # Temporal proximity linker
  temporal_window_hours: 168

  # Minimum sizes
  thread_min_fragments: 3
  eddy_min_fragments: 5

  # Eddy (DBSCAN) detector thresholds
  eddy_eps: 0.3
  eddy_min_samples: 5
  eddy_correlation_threshold: 0.3

  # Thread detector thresholds
  thread_window_days: 30
  thread_similarity_threshold: 0.6
  thread_union_without_embeddings: false

  # Cluster-size guardrail
  cluster_size_ceiling: 500
  cluster_max_fraction: 0.10
  cluster_split_max_depth: 3
  eddy_split_eps_step: 0.05
  thread_split_similarity_step: 0.1

  # Message-stream segmentation
  stream_platforms: [discord, email]
  stream_episode_max_gap_hours: 24
  stream_episode_max_span_days: 30
```

### Windows and minimum sizes

| Field                  | Default | Notes |
|------------------------|---------|-------|
| `temporal_window_hours`| `168`   | Sliding-window width (1 week) for the **temporal proximity linker** only (`creek link --method temporal`). It has never influenced thread detection — see `thread_window_days` for that. |
| `thread_min_fragments` | `3`     | Minimum chain length for a thread. |
| `eddy_min_fragments`   | `5`     | Minimum cluster size for an eddy. |

### Detector thresholds

Previously hard-coded module constants, exposed by [ADR-0008](architecture/ADR/0008-bounding-cluster-degeneration-in-message-streams.md). Every default equals the constant it replaced, so a vault whose `creek_config.yaml` predates these keys clusters identically after the upgrade.

| Field                             | Default | Notes |
|-----------------------------------|---------|-------|
| `eddy_eps`                        | `0.3`   | Maximum cosine **distance** for a DBSCAN neighbour — `0.3` means an edge at cosine similarity ≥ `0.70`. Range `(0.0, 1.0)`. Lowering it tightens every cluster but cannot, on its own, separate a continuous message stream (that is what `stream_platforms` is for). |
| `eddy_min_samples`                | `5`     | Minimum neighbourhood size for a DBSCAN core point. Minimum `1`. |
| `eddy_correlation_threshold`      | `0.3`   | Absolute Spearman ceiling above which a candidate cluster is judged *thread-like* (content drift correlates with chronological rank) and filtered out of the eddy set. Range `[0.0, 1.0]`. |
| `thread_window_days`              | `30`    | Sliding-window width, in days, for **thread detection**. Distinct from `temporal_window_hours`. Minimum `1`. |
| `thread_similarity_threshold`     | `0.6`   | Cosine similarity a pair must *strictly exceed* to join one thread. Range `[0.0, 1.0]`. |
| `thread_union_without_embeddings` | `false` | Whether a pair missing an embedding may union on frequency agreement alone. On a *partially* embedded vault this fallback silently chains fragments the similarity gate would have rejected, so it is closed by default. (A vault with **no** embeddings at all still falls back to frequency agreement by design.) |

### Cluster-size guardrail

The last line of defence against a single eddy or thread swallowing the vault. It is a guardrail, not the cure — see ADR-0008.

| Field                          | Default | Notes |
|--------------------------------|---------|-------|
| `cluster_size_ceiling`         | `500`   | Absolute member count below which a cluster is never split. The effective ceiling is `max(cluster_size_ceiling, floor(corpus_size × cluster_max_fraction))`, so the absolute floor dominates ordinary vaults and the guardrail stays inert until a corpus is large enough for degeneration to matter. Minimum `1`. |
| `cluster_max_fraction`         | `0.10`  | Largest share of the corpus a single eddy or thread may hold. Range `(0.0, 1.0]`; **`1.0` is the documented opt-out** — one cluster may then span everything. |
| `cluster_split_max_depth`      | `3`     | Re-clustering rounds allowed per oversized cluster. `0` disables splitting entirely, sending any oversized cluster straight to noise. Minimum `0`. |
| `eddy_split_eps_step`          | `0.05`  | Amount `eddy_eps` is tightened by on each re-clustering round. Range `(0.0, 1.0)`. |
| `thread_split_similarity_step` | `0.1`   | Amount `thread_similarity_threshold` is raised on each re-clustering round. Range `(0.0, 1.0)`. |

A cluster still over the ceiling once **both** bounds are exhausted — the depth budget, and the tightening schedule leaving its valid range (epsilon may never reach `0.0`, similarity may never reach `1.0`) — is **discarded to noise**: its members carry no `eddies:` / `threads:` link at all. Each discard logs a `WARNING` naming the clustering domain and the size, and `creek link` reports the total as `N fragment(s) discarded as unsplittable`.

### Message-stream segmentation

A continuous chat stream violates the precondition both detectors rely on — clusters separated by low-density regions — so no threshold value can separate it. Stream fragments are instead partitioned into independent clustering domains (conversation episodes) *before* any similarity graph is built.

| Field                          | Default              | Notes |
|--------------------------------|----------------------|-------|
| `stream_platforms`             | `[discord, email]`   | Source platforms whose fragments are cut into conversation episodes, keyed on `(platform, series, episode_index)` where `series` is the channel, else the conversation id, else the interlocutor. The default names the two platforms Creek already routes to `01-Fragments/Messages/`; chat *transcripts* (`claude`, `chatgpt`) stay long-form material in the shared domain. An **empty list disables segmentation**. Every value must name a `creek.models.SourcePlatform` member — a typo is rejected at load rather than silently disabling segmentation. |
| `stream_episode_max_gap_hours` | `24`                 | Inactivity gap, in hours, that ends a conversation episode — the primary, conversational rule. Inclusive: a gap exactly this long does not cut. Minimum `1`. |
| `stream_episode_max_span_days` | `30`                 | Maximum span, in days, of a single episode — the backstop for a channel that never falls idle, so a permanently-busy channel yields channel-month units rather than one multi-year blob. Inclusive. Minimum `1`. |

Every fragment that is not from a `stream_platforms` platform lands in one shared domain, so cross-source resonance, cross-platform eddies and multi-year threads over long-form material behave exactly as before.

## `classification` — auto-classify thresholds

```yaml
classification:
  confidence_threshold: 0.7
  auto_classify_sources: [claude, chatgpt, discord]
  human_review_sources: [journal]
```

| Field                   | Default                          | Notes |
|-------------------------|----------------------------------|-------|
| `confidence_threshold`  | `0.7`                            | Below this, a fragment lands in the review queue. |
| `auto_classify_sources` | `[claude, chatgpt, discord]`     | Sources whose fragments pass through without review. |
| `human_review_sources`  | `[journal]`                      | Sources whose fragments **always** require review. |

## `context` — non-user content

```yaml
context:
  mode: context_metadata
  quality_penalty: 0.5
```

| Field             | Default              | Notes |
|-------------------|----------------------|-------|
| `mode`            | `context_metadata`   | `context_metadata` (others' content is metadata only), `low_priority` (separate fragments, reduced quality), or `skip` (drop entirely). |
| `quality_penalty` | `0.5`                | Multiplier applied to the quality score in `low_priority` mode. |

## `redaction`

```yaml
redaction:
  enabled: true
  dry_run: false
  custom_patterns:
    employer_id: "EMP-\\d{6}"
  false_positive_allowlist:
    - "test@example.com"
  supported_extensions:
    - .md
    - .txt
    - .json
  exclude_patterns:
    - .git/
    - node_modules/
  min_confidence: 0.6
  replacement_template: "[REDACTED:{name}]"
```

| Field                       | Default                | Notes |
|-----------------------------|------------------------|-------|
| `enabled`                   | `true`                 | Master switch. |
| `dry_run`                   | `false`                | When `true`, `--apply` plans but doesn't write. |
| `custom_patterns`           | `{}`                   | Extra regex name → pattern map merged with built-ins. |
| `false_positive_allowlist`  | `[]`                   | Substrings that, when present in the surrounding context, suppress a match. |
| `supported_extensions`      | (text)                 | File extensions the scanner walks. |
| `exclude_patterns`          | (vcs)                  | Path globs to skip. |
| `min_confidence`            | `0.6`                  | Threshold for the generic high-entropy detector; range `[0.0, 1.0]`. Higher demands more entropy before flagging. |
| `replacement_template`      | `"[REDACTED:{name}]"`  | Marker template for `--apply`. Must contain the `{name}` placeholder; other placeholders are rejected at config-load time. |

## `google_drive`

```yaml
google_drive:
  credentials_file: credentials.json
  token_file: token.json
  scopes:
    - https://www.googleapis.com/auth/drive.readonly
  staging_dir: google-drive-export/
```

| Field              | Default                            | Notes |
|--------------------|------------------------------------|-------|
| `credentials_file` | `credentials.json`                 | OAuth client credentials JSON downloaded from Google Cloud. |
| `token_file`       | `token.json`                       | Refresh token cache. Created with mode `0o600`. |
| `scopes`           | `[drive.readonly]`                 | OAuth scopes; **read-only by default and recommended**. |
| `staging_dir`      | `google-drive-export/`             | Where mirrored files land. |

### Security considerations

The `token_file` stores a long-lived OAuth **refresh token** in
plaintext. It is written at mode `0o600`, which keeps other Unix users
out, but anything running as the same user — malware, an Obsidian
plugin, an unencrypted backup, or a clipboard manager that snapshots
the file — can lift the token. The token grants `drive.readonly`
access until you revoke it.

Recommended hygiene:

- **Encrypt the disk.** Enable FileVault (macOS) or LUKS (Linux) so an
  offline attacker cannot read the token from a stolen device. This is
  the single most important mitigation; the rest of this section
  assumes the disk is already encrypted at rest.
- **Treat `token.json` as a secret.** Add it to `.gitignore` and to
  any backup-tool exclusion list.
- **Rotate or revoke after exposure.** If the file is ever copied off
  the host (shared screenshot, accidental commit, third-party sync),
  run `creek gdrive --revoke` to invalidate it both locally and at
  Google. The command best-effort posts to
  <https://oauth2.googleapis.com/revoke>, then overwrites the local
  file with zeros and unlinks it. You can also revoke manually from
  <https://myaccount.google.com/permissions>.
- **Re-authorise sparingly.** Each `--download` run reuses the cached
  token; you only need to re-authorise after a `--revoke` or after the
  token expires.

For the broader picture of what is and isn't protected, see
[`security/threat-model.md`](security/threat-model.md).

## `cleaning` — per-source filters

```yaml
cleaning:
  discord:
    filter_bot_messages: true
    strip_emoji: false
    filter_commands: true
    min_message_length: 10
  chatbot:
    filter_system_prompts: true
    filter_tool_outputs: true
    filter_regenerations: true
    min_human_turn_length: 20
    code_block_threshold: 0.9
    max_abandoned_turns: 2
  markdown:
    skip_empty_files: true
    min_body_length: 50
  google_drive:
    deduplicate: true
    filter_empty_docs: true
    max_collaboration_ratio: 0.9
  validation:
    min_characters: 20
    min_words: 5
    max_stop_word_ratio: 0.8
    require_metadata: true
  quality:
    accept_threshold: 0.7
    skip_threshold: 0.3
  deduplication:
    strategy: fuzzy           # exact | fuzzy | semantic
    similarity_threshold: 0.85
  hygiene:
    track_orphans: true
    staleness_days: 90
```

The cleaning sub-tree controls source-specific filters that run **during** ingestion (not just on demand). Tune these when an export is too noisy or too sparse.

## `sources` — default input directories

```yaml
sources:
  claude: chatbot-exports/claude/
  chatgpt: chatbot-exports/chatgpt/
  discord: discord-export/
  gdrive: google-drive-export/
  aptitude: projects/aptitude/course-files/
  essays: writing/substack/
  journal: personal/journal/
  code: projects/
```

These are **relative to `source_drive`**. `creek process` walks them in order. Override on the CLI with `--input` for one-off runs.

## `voice_audience_weighting` — graduated voice authority

```yaml
voice_audience_weighting:
  enabled: true
  privacy_tier_authority:
    open: 1.5          # public-facing work carries the most authority
    personal: 1.0      # baseline
    unclassified: 0.75
    intimate: 0.0      # also excluded from the corpus entirely
  representativeness_authority:
    self: 1.0
    endorsed: 0.9
    aspirational: 0.6
    reference: 0.3     # borrowed material keeps near-zero influence
```

Each voice exemplar's ranking score is multiplied by `privacy_tier_authority[tier] × representativeness_authority[value]`, so an `OPEN` essay outweighs a `PERSONAL` chat turn and dominates the patterns that shape drafts. Setting `enabled: false` makes every authority `1.0` (the pre-weighting ranking). Missing keys default to `1.0`.

## `ai_style` — voice fidelity / de-slop

```yaml
ai_style:
  enabled: true
  voice_distance_upper: 0.35   # accepted-divergence ceiling
  voice_distance_target: 0.25  # the de-slop rewrite loop drives toward this
  min_fingerprint_fragments: 5 # below this the fingerprint is "thin"
```

`voice_distance_target` is the value the post-composition de-slop rewrite loop drives toward; it is distinct from `voice_distance_upper` (the accepted ceiling) and is **clamped** to the ceiling if configured above it. The guard always stamps a `voice_guard_status` on the draft (`rewritten`, `measured_only:*`, or `skipped:*`) — it never silently passes a mannered draft through. See [generation](./generation.md#voice-fidelity-feat-040).

## Environment variables

Every leaf field can be overridden by a `CREEK_…` env-var. Nested keys use double underscores:

```bash
export CREEK_LLM__PROVIDER=anthropic
export CREEK_LLM__MODEL=claude-haiku-4-5-20251001
export CREEK_EMBEDDINGS__SIMILARITY_THRESHOLD=0.78
export ANTHROPIC_API_KEY=sk-ant-...
```

Env-vars take precedence over the YAML file. This is how CI runs `creek-tools` against synthetic configs without touching repo files.

## Where things live

| Concern                        | Path |
|--------------------------------|------|
| YAML config                    | `<vault>/00-Creek-Meta/creek_config.yaml` |
| OAuth refresh token            | `<google_drive.token_file>` (default `token.json`, mode `0o600`) |
| Embedding cache                | `<vault>/00-Creek-Meta/embeddings.parquet` |
| Audit log (purges)             | `<vault>/00-Creek-Meta/audit/purge.jsonl` (hash-chained JSONL) |
| Audit log (redactions)         | `<vault>/00-Creek-Meta/audit/redact.jsonl` (hash-chained JSONL) |
| Audit log (privacy overrides)  | `<vault>/00-Creek-Meta/audit/privacy.jsonl` (hash-chained JSONL) |
| Provenance log (ingest)        | `<vault>/00-Creek-Meta/Processing-Log/provenance.jsonl` (operational; not compliance-grade) |
| Review queue                   | Frontmatter on each fragment (`review: pending`) |
| Skill tree                     | `<vault>/creek-skills/` (override with `creek skills --output`) |
