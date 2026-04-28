# Configuration

`creek-tools` reads its configuration from `<vault>/00-Creek-Meta/creek_config.yaml`. The file is parsed into the Pydantic models defined in [`creek/config.py`](../creek/config.py); env-vars prefixed with `CREEK_` override individual fields. **API keys are never stored in the YAML — they come from the environment** (e.g. `ANTHROPIC_API_KEY`).

This page is the field-by-field reference. Every section maps to a `BaseModel` you can read in `config.py`.

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
  model: llama3.1
  ollama_url: http://localhost:11434
  batch_size: 50
  max_concurrent: 5
```

| Field            | Default   | Notes |
|------------------|-----------|-------|
| `provider`       | `ollama`  | `ollama`, `anthropic`, or `openai`. |
| `model`          | `mistral` | Model id understood by the provider. |
| `ollama_url`     | `http://localhost:11434` | Base URL when `provider: ollama`. |
| `batch_size`     | `50`      | Items per batch call. |
| `max_concurrent` | `5`       | Concurrent requests in flight. |

When `provider: anthropic`, set `ANTHROPIC_API_KEY` in the environment. When `provider: openai`, set `OPENAI_API_KEY`. Local Ollama models require nothing in the environment beyond the server being running.

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
```

| Field       | Default        | Notes |
|-------------|----------------|-------|
| `enabled`   | `true`         | If `false`, `creek ingest --type images` will skip OCR. |
| `engine`    | `pytesseract`  | Engine name. (Custom engines are injected at the API level — see `creek.ingest.images.OcrEngine`.) |
| `languages` | `[eng]`        | Tesseract language codes. |

## `linking` — resonance / thread / eddy thresholds

```yaml
linking:
  temporal_window_hours: 168
  thread_min_fragments: 3
  eddy_min_fragments: 5
```

| Field                  | Default | Notes |
|------------------------|---------|-------|
| `temporal_window_hours`| `168`   | Sliding-window width for thread detection (1 week). |
| `thread_min_fragments` | `3`     | Minimum chain length for a thread. |
| `eddy_min_fragments`   | `5`     | Minimum cluster size for an eddy. |

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
```

| Field                       | Default | Notes |
|-----------------------------|---------|-------|
| `enabled`                   | `true`  | Master switch. |
| `dry_run`                   | `false` | When `true`, `--apply` plans but doesn't write. |
| `custom_patterns`           | `{}`    | Extra regex name → pattern map merged with built-ins. |
| `false_positive_allowlist`  | `[]`    | Substrings that, when present in the surrounding context, suppress a match. |
| `supported_extensions`      | (text)  | File extensions the scanner walks. |
| `exclude_patterns`          | (vcs)   | Path globs to skip. |

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

## Environment variables

Every leaf field can be overridden by a `CREEK_…` env-var. Nested keys use double underscores:

```bash
export CREEK_LLM__PROVIDER=anthropic
export CREEK_LLM__MODEL=claude-haiku-4-5
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
| Audit log (purges, redactions) | `<vault>/00-Creek-Meta/audit/` |
| Review queue                   | Frontmatter on each fragment (`review: pending`) |
| Skill tree                     | `<vault>/creek-skills/` (override with `creek skills --output`) |
