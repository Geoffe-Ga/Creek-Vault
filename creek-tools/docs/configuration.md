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
```

| Field          | Default         | Notes |
|----------------|-----------------|-------|
| `vault_path`   | `.`             | Absolute path to the Obsidian vault. |
| `source_drive` | `.`             | Default `--source` for `creek process` / `creek ingest`. |

**`timezone` was removed in #1339.** It had zero production readers, and a
configurable anchor would have been actively harmful: `generate_fragment_id`
(`creek/ingest/base.py`) hashes `timestamp.isoformat()`, whose rendered UTC
offset changes with the zone, so a settable `timezone` would mint a
different `frag-…` id for the same instant depending on operator config —
reopening the id-derivation bug #1329 already required a vault migration
(`creek/ingest/pin_ids.py`) to fix. Creek anchors every timestamp it writes
to **America/Los_Angeles** by ontology mandate §8.3 (`creek/time.py`,
`LA_TZ`) — a design invariant, not a per-operator preference. If your
`creek_config.yaml` still has a `timezone:` line, the file still loads, but
you'll see a `WARNING` on load: the key is ignored and safe to delete.

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

**Every field in this block is dormant.** All four are on
`DORMANT_CONFIG_FIELDS` in `tests/test_config_contract.py` — "declared
dormant: OCR wire-in tracked by #1041" — and that allowlist is a ratchet
which fails the moment a listed field *is* read, so their being on it is
proof no production path consults them. `creek init` still writes the
block, and it still loads, but **editing these values changes nothing
today**: setting `enabled: false` does not stop `creek ingest --type
image` from attempting OCR, and `languages` does not reach Tesseract.

Read "dormant" precisely: it means *this key is not consulted*, **not**
that the behaviour behind it is absent. The confidence threshold in
particular is live — see the `min_confidence` row.

| Field            | Default        | Status |
|------------------|----------------|-------|
| `enabled`        | `true`         | **Dormant.** Intended as the master switch for the OCR pass. |
| `engine`         | `pytesseract`  | **Dormant.** Custom engines are injected at the API level today — see `creek.ingest.images.OcrEngine`. |
| `languages`      | `[eng]`        | **Dormant.** Intended for Tesseract language codes. |
| `min_confidence` | `0.6`          | **Dormant — but the behaviour it names is live.** Editing this number changes nothing; the threshold in force is `_DEFAULT_MIN_CONFIDENCE` (`creek/ingest/images.py`). A low-confidence page really *is* tagged `review: pending_review` today — executed with an injected `OcrEngine` returning `0.42`, whose frontmatter came back `review: pending_review`. Range `[0.0, 1.0]`. No command filters on the resulting key. |

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

## Seeding: what is and is not configurable

Getting source material into a vault is documented end to end in
[seeding.md](./seeding.md). This section is the config-side companion, and it
is deliberately short: **the seeding epic ([#1523](https://github.com/Geoffe-Ga/Creek-Vault/issues/1523))
added no new `creek_config.yaml` fields.** What it added is a network surface
whose controls are environment variables and request headers. Listing knobs
that do nothing is the failure mode this repository tracks as
[#1041](https://github.com/Geoffe-Ga/Creek-Vault/issues/1041), so nothing
below is listed unless it was observed changing behaviour.

### Controls that work

| Control | Kind | Observed effect |
|---------|------|-----------------|
| `CREEK_MCP_CONSUMER_TOKENS` | Environment variable | **Required.** With it unset, `create_app()` raises `ValueError: CREEK_MCP_CONSUMER_TOKENS is not set (consumer=token pairs). /v1 has no anonymous access, so it refuses to serve without authentication configured.` A token under 32 characters is refused at startup too, naming the length and the rotation recipe. An unauthenticated request gets `401 unauthenticated`. Format and rotation: [api.md](./api.md). |
| `X-Creek-Tier-Ceiling` | Request header | Sets the tier ceiling for one `/v1` request; absent, it is `open`. A `tier: personal` upload at the default ceiling is refused `403 privacy_refused`; the identical upload with `X-Creek-Tier-Ceiling: personal` returns `200` and the fragment lands `privacy_tier: personal`. `intimate` is not an accepted value — `GET /v1/capabilities` advertises `ceilings: ["open", "personal"]` with `intimate_never_egresses: true`. |
| `tier` (per upload) | Request field | Required on every `POST /v1/uploads` and every `creek.upload` call, never defaulted, and checked **before any byte is decoded**. |
| [`google_drive`](#google_drive) | YAML block | The Drive connector's credential, token, scope and staging paths. Unchanged by this epic. Note `creek gdrive` has **no `--vault` flag**: without `CREEK_CONFIG` or `--config` it runs on built-in defaults and prints `Config file creek_config.yaml not found; running with built-in defaults.` |
| [`sources`](#sources--default-input-directories) | YAML block | Default input directories for `creek process`, relative to `source_drive`. |

### Deliberately not configurable

These come up as "where do I set this?" and the answer is that you cannot —
stated here so nobody goes looking for a field that does not exist.

| Not configurable | Where it is fixed | Why it matters |
|------------------|-------------------|----------------|
| The **10 MiB** upload cap | `MAX_UPLOAD_BYTES` in `creek_mcp/tools/upload.py` | Checked on the encoded length, then the decoded length, before anything is written. |
| The **tier a CLI-seeded fragment gets** | `creek ingest` has no tier or privacy option at all — grepping its `--help` for `tier` or `privacy` returns nothing, and no config key supplies a default | Every fragment `creek ingest` writes is `privacy_tier: unclassified`. See [seeding.md § Privacy of seeded content](./seeding.md#privacy-of-seeded-content). |
| **Which ingestor an uploaded extension routes to** | `_EXTENSION_ROUTES` in `creek/ingest/gdrive.py`, pinned by `tests/test_seeding_docs_capability_set.py` | There is deliberately no `source_type` override on the upload surface. |
| Whether an export **archive** is unpacked | `ARCHIVE_SUFFIXES` in `creek/ingest/archive.py` — `.zip` only | `.tar` and friends are refused. **The refusal text differs by surface**: the `creek.upload` MCP tool names the remedy — *"Creek unpacks .zip export archives, so re-pack this one as .zip and send that"* — while the identical bytes to `POST /v1/uploads` come back as a `415` carrying the generic `unsupported_source` message, the same one a `.json` gets, with no repack remedy. Both executed. |

## `voice_audience_weighting` — graduated voice authority

```yaml
voice_audience_weighting:
  enabled: true
  privacy_tier_authority:
    open: 1.5          # public-facing work carries the most authority
    personal: 1.0      # baseline
    unclassified: 0.75
    intimate: 0.0      # also excluded from the corpus entirely, upstream
  representativeness_authority:
    self: 1.0
    endorsed: 0.9
    aspirational: 0.6
    reference: 0.3     # borrowed material keeps near-zero influence
  platform_authority:
    essay: 2.0          # audience-facing platforms outrank private ones
    substack: 2.0
    journal: 0.1
    messages: 0.4
    discord: 0.4
    chatgpt: 0.3
    claude: 0.3
  audience_authority:
    audience-facing: 2.0   # the #634 classifier's primary signal
    private: 0.1
    mixed: 1.0              # the unclassified default; falls back to platform_authority
```

`platform_authority` and `audience_authority` are seeded into every vault by `creek init` alongside the two maps above, but **not every report reads all four** — this is the part of the section that trips people up:

| Path | Formula | Reads |
|------|---------|-------|
| **Exemplar ranking** — `creek report --type voice`, `--type rhetorical-patterns`, and the register samples `creek fill` writes to `07-Voice/Register-Samples/` | `privacy_tier_authority[tier] × representativeness_authority[value]` | Only these two maps. `platform_authority` and `audience_authority` have no effect here. |
| **Fingerprint** — `creek report --type fingerprint` | `audience_authority[audience] × privacy_tier_authority[tier] × representativeness_authority[value] × platform_authority[platform]` | All four maps. |

So an `OPEN` essay outweighs a `PERSONAL` chat turn on both paths, but scoping the fingerprint to audience-facing platforms via `platform_authority` or `audience_authority` does nothing to which exemplars get ranked or persisted as register samples. Setting `enabled: false` makes every authority `1.0` on both paths (the pre-weighting, uniform ranking). Missing keys default to `1.0` on every map, so a new privacy tier, representativeness value, platform, or audience classification never silently zeroes a fragment out.

**A `0.0` authority means two different things depending on the path.** On the fingerprint path it is a membership gate: `_eligible_texts` drops a fragment from the corpus outright when its combined weight is not greater than zero, so `intimate: 0.0` (the default) keeps intimate fragments out of the fingerprint. Note when that actually bites: `_eligible_texts` already skips intimate fragments outright unless its caller passes `include_intimate=True`, so on a default run the zero authority is a redundant second gate. It becomes the operative one precisely when an operator opts intimate content in — at which point the shipped `0.0` quietly overrides that opt-in. If you deliberately want intimate writing in your fingerprint, you must raise `privacy_tier_authority.intimate` above zero as well. On the exemplar path a `0.0` only de-ranks: `rank_exemplars` always returns the top `max_per_register` fragments by score, whatever those scores are, so a zero-weighted fragment can still be selected if the register has room. **Do not set `intimate: 0.0` (or any authority to zero) expecting it to exclude fragments from the exemplar corpus, register samples, or lexicon — it will not.** Exclusion there is the job of the privacy-tier ceiling and the self-authorship/consent gates, described next.

**This is not a privacy control.** Setting `enabled: false`, or editing any of the four maps, never widens who is eligible for the voice corpus. Membership is decided upstream and independently of this section — by the privacy-tier ceiling (`--include-tier`, #968) and the self-authorship/consent gates in `VoiceExemplarCollector._eligible_register` — before any authority multiplier is ever applied. No value of `voice_audience_weighting`, not even `intimate: 10.0`, can admit an intimate or above-ceiling fragment into the corpus.

What it *does* change is which fragments survive the top-`max_per_register` cut on the exemplar path, and that matters because the cut decides which fragment bodies get duplicated into the vault verbatim. There are **two** such surfaces, not one:

- a persisted register sample is the source fragment's file **copied byte for byte** into `07-Voice/Register-Samples/<register>/`;
- a rendered profile embeds up to `max_exemplars` (10) **full exemplar bodies** under `### Sample Passages` in `07-Voice/<register>-profile.md`. This is the surface the MCP `report_type="voice"` tool writes — it deliberately does not write register samples — so an MCP-only operator is still exposed to this even though the first bullet does not apply to them.

Both then seed generated drafts.

Concretely, with the shipped defaults: the only live factor on the exemplar path is `privacy_tier_authority` (`open 1.5` / `personal 1.0` / `unclassified 0.75`), so when candidates exceed the cap, `enabled: false` flattens every authority to `1.0` and lets a `personal` body displace an `open` one out of the cohort — and get copied verbatim in its place. The starker version needs an edited corpus rather than an edited knob: give a fragment `representativeness: reference` in its own frontmatter and it scores `0.3`, so `enabled: true` keeps one `self`-authored fragment ahead of borrowed material that `enabled: false` would let crowd it out entirely. (Fragments Creek itself files under `11-Other-Authors/` are excluded from the voice corpus outright and are not the case at issue here.)

Turning the weighting off can therefore *increase* how much borrowed — or, with a permissive tier ceiling, private — prose is duplicated into the vault and fed to drafts. An operator-directed and permissible choice, but one worth knowing you are making.

**Upgrade note.** The values `creek init` writes above are byte-identical to the code defaults the exemplar path was already using before this section was wired to the vault's file (issue #1313 — the vault's `enabled: false` or edited maps were previously ignored on that path). Activating the fix changes exemplar-ranking output only for operators who had deliberately edited this block, which is exactly the behaviour those edits asked for; everyone still running the shipped defaults sees no change.

## `ai_style` — voice fidelity / de-slop

```yaml
ai_style:
  enabled: true
  voice_distance_upper: 0.35   # accepted-divergence ceiling
  voice_distance_target: 0.25  # the de-slop rewrite loop drives toward this
  min_fingerprint_fragments: 5 # below this the fingerprint is "thin"
```

`voice_distance_target` is the value the post-composition de-slop rewrite loop drives toward; it is distinct from `voice_distance_upper` (the accepted ceiling) and is **clamped** to the ceiling if configured above it. The guard always stamps a `voice_guard_status` on the draft (`rewritten`, `measured_only:*`, or `skipped:*`) — it never silently passes a mannered draft through. See [generation](./generation.md#voice-fidelity-feat-040).

## `author` — the Writing Desk

```yaml
author:
  max_author_rounds: 3
  graph_breadth_bound: 25
  graph_depth_bound: 2
  retrieval_top_k: 5
  max_reproduced_tier: open
  voice_model: null           # falls back to llm.model
  synthesis_model: null       # reserved; not yet read (#474)
  reflection_model: null      # reserved; not yet read (#474)
```

| Field                 | Default   | Notes |
|------------------------|-----------|-------|
| `max_author_rounds`    | `3`       | Max Conductor voice/reflect rounds before escalation. Bounded `[1, 10]`. |
| `graph_breadth_bound`  | `25`      | Max fragments the Graph specialist's backlink walk expands at each depth level. Minimum `1`. |
| `graph_depth_bound`    | `2`       | Max backlink hops the Graph specialist walks from its seed (`0` = seed only). Minimum `0`. |
| `retrieval_top_k`      | `5`       | How many top-ranked fragments the Retrieval specialist surfaces as evidence. Minimum `1`. |
| `max_reproduced_tier`  | `"open"`  | Highest privacy tier a finished draft may reproduce **verbatim**. One of `open` \| `personal` \| `intimate` \| `unclassified`; `open` is the *strictest* value, not the loosest. The HARD `privacy_compliance` gate enforces the more restrictive of this key and the medium contract's `default_privacy_tier` — a contract can only narrow the gate, never widen it. An unrecognised or null value fails closed to `open`. Full discussion, including the trust-boundary and drift-detection caveats: [writing-desk.md § The reproduction ceiling](./writing-desk.md#the-reproduction-ceiling). |
| `voice_model`          | `null`    | Per-agent model override for the voice call. `null` (default) falls back to `llm.model`. |
| `synthesis_model`      | `null`    | Reserved override for the synthesis step. Synthesis is deterministic today (no LLM call), so this is documented but **dormant**: falls back to `llm.model` and nothing reads it yet. |
| `reflection_model`     | `null`    | Reserved override for the reflection step. Reflection is a deterministic judge today, so like `synthesis_model` this is documented but **dormant**. |

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
