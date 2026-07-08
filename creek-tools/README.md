# creek-tools

The Python pipeline behind the **Creek** knowledge organization system. `creek-tools` ingests semi-structured personal data — chat exports, documents, notes, screenshots — redacts sensitive content, classifies it along the [APTITUDE / Archetypal Wavelength](../docs/Ontology/creek_ontology_agent_prompt.md) ontology, links semantically related fragments, and writes a richly interlinked Obsidian vault.

The pipeline is **local-first by default**: classification uses Ollama, embeddings use sentence-transformers, and no content leaves your machine unless you explicitly opt in to the Anthropic API path.

> Detailed task guides live under [`docs/`](docs/). This README is the entry point — install, run your first pipeline, and skim the command reference.

---

## Install

`creek-tools` requires **Python ≥ 3.11**. From the repository root:

```bash
cd creek-tools
pip install -e .
```

Optional dependencies are imported lazily, so you only need to install the ones whose source types you actually ingest:

| Source type            | Optional dependency          |
|------------------------|------------------------------|
| `.docx` / `.pdf`       | `python-docx`, `pdfminer.six`|
| `.xlsx`                | `openpyxl`                   |
| `.pptx`                | `python-pptx`                |
| Image OCR              | `pytesseract`, `pdf2image`, system-level `tesseract` and `poppler` |
| Google Drive           | `google-api-python-client`, `google-auth-oauthlib` |
| Embeddings             | `sentence-transformers`      |
| LLM classification — Anthropic | `anthropic` (the `[anthropic]` extra) |
| LLM classification — OpenAI    | `openai` (the `[openai]` extra) |
| LLM classification — Gemini    | `google-genai` (the `[gemini]` extra) |
| LLM classification — local     | Ollama running locally — no extra, no key |

The cloud LLM backend is selectable — see [LLM providers](#llm-providers) below. `[all]` (and therefore `[dev]`) pulls in every cloud extra so the test suite can mock each SDK.

To install everything required for development plus the full quality toolchain:

```bash
./scripts/dev-setup.sh          # uv sync --all-extras + pre-commit install
```

The setup script wraps the canonical install path (`uv sync --all-extras` against the pinned `uv.lock`) and installs the pre-commit hooks. If you prefer plain pip:

```bash
pip install -e '.[dev]'         # `[dev]` includes `[all]`, so this is self-contained
pre-commit install
```

Either path produces an environment whose `./scripts/check-all.sh` matches CI on the same commit.

---

## Quickstart

```bash
# 0. Scaffold your vault somewhere OUTSIDE this repo (FEAT-019).
creek init --vault ~/Obsidian/Creek-Vault

# 1. Scan for secrets before anything else.
creek redact --scan --source ~/exports --report

# 2. Apply redactions (writes a queue under <source>/.creek-redactions/).
creek redact --apply --source ~/exports --dry-run    # preview
creek redact --apply --source ~/exports              # commit

# 3. Run the full pipeline (ingest -> redact -> classify -> link -> index).
creek process --source ~/exports --vault ~/Obsidian/Creek

# Or run individual stages:
creek ingest   --type chatgpt  --input ~/exports/chatgpt.zip --vault ~/Obsidian/Creek
creek classify --vault ~/Obsidian/Creek --method rules
creek link     --vault ~/Obsidian/Creek --method embeddings
creek report   --type wavelength --period weekly --vault ~/Obsidian/Creek
```

Every command is also documented under [`docs/`](docs/) with end-to-end examples.

---

## Command reference

`creek` is a [Typer](https://typer.tiangolo.com) CLI with 13 top-level commands. Run `creek <command> --help` for full option listings.

### Pipeline

| Command | Purpose | Doc |
|---------|---------|-----|
| `creek process`  | Run the full pipeline (ingest → redact → classify → link → index) on a source directory. | [getting-started](docs/getting-started.md) |
| `creek ingest`   | Run a single ingestor against an input path. Pick the source type with `--type` (e.g. `chatgpt`, `discord`, `markdown`, `documents`, `images`, `code`, `claude`, `gdrive`, `spreadsheet`, `presentation`, `generic`). | [ingestion](docs/ingestion.md) |
| `creek gdrive`   | Read-only Google Drive downloader. Mirrors files into a staging directory; subsequent runs are incremental. | [ingestion](docs/ingestion.md#google-drive) |

### Privacy & safety

| Command | Purpose | Doc |
|---------|---------|-----|
| `creek redact --scan`   | Scan a source for secrets, API keys, and PII. Writes a structured report. | [redaction](docs/redaction.md) |
| `creek redact --apply`  | Apply queued redactions to source files. Supports `--dry-run` and `--yes`. | [redaction](docs/redaction.md) |
| `creek redact --review` | Render the review queue for a vault. | [redaction](docs/redaction.md) |
| `creek purge fragment`  | Delete a fragment and scrub every reference (right-to-be-forgotten). | [cleaning-and-purge](docs/cleaning-and-purge.md) |
| `creek purge source`    | Delete every fragment ingested from a given source. | [cleaning-and-purge](docs/cleaning-and-purge.md) |
| `creek purge classifications` | Reset every fragment's classification fields to `unclassified`. | [cleaning-and-purge](docs/cleaning-and-purge.md) |
| `creek purge daterange` | Delete fragments created within a date range. | [cleaning-and-purge](docs/cleaning-and-purge.md) |
| `creek purge vault`     | Nuclear option: destroy every fragment, thread, and eddy. Refuses non-interactive use unless `--force-non-interactive`; otherwise prompts for the absolute vault path. | [cleaning-and-purge](docs/cleaning-and-purge.md) |
| `creek gdrive --revoke` | Revoke the cached Google Drive OAuth token (best-effort remote revoke + secure local erase). | [configuration](docs/configuration.md#google_drive) |

### Analysis

| Command | Purpose | Doc |
|---------|---------|-----|
| `creek classify` | Classify vault fragments with rule-based heuristics or an LLM (`--method rules` or `--method llm`). | [classification](docs/classification.md) |
| `creek link`     | Link fragments by embedding similarity, temporal proximity, or shared tags. | [linking](docs/linking.md) |
| `creek report`   | Generate vault-state reports (wavelength snapshots, weekly/monthly summaries, etc.). | [generation](docs/generation.md#reports) |
| `creek review`   | Interactive review queue for fragments that need human attention. | [classification](docs/classification.md#review-queue) |

### Generation

| Command | Purpose | Doc |
|---------|---------|-----|
| `creek skills generate` | Generate the **Voice Skill Tree** under `<vault>/creek-skills/` (one `SKILL.md` per frequency, phase, mode, register, plus thread/eddy/meta skills). Previously `creek skills`; see [`CHANGELOG.md`](CHANGELOG.md) for the FEAT-019 rename. | [generation](docs/generation.md#voice-skill-tree) |
| `creek skills sync` | Re-deploy the canonical schema-skill tree from `creek-tools/creek/templates/skills/` into `<vault>/00-Creek-Meta/Skills/`. Refuses to overwrite locally-modified skill files unless `--force` is passed. | [generation](docs/generation.md#voice-skill-tree) |
| `creek mine`     | Mine blog/essay seed ideas from the vault using four discovery strategies (liminal cross-eddy, thread terminus, resonance chain, wavelength-phase window). | [generation](docs/generation.md#mining) |
| `creek draft`    | Draft an essay from a mined idea using the activated skill stack. Saves to `07-Voice/Drafts/` with full provenance. | [generation](docs/generation.md#drafting) |

### Vault hygiene

| Command | Purpose | Doc |
|---------|---------|-----|
| `creek clean orphans`       | Identify fragments with zero links after N days. | [cleaning-and-purge](docs/cleaning-and-purge.md) |
| `creek clean stale-reviews` | Find review queue items older than N days. | [cleaning-and-purge](docs/cleaning-and-purge.md) |
| `creek clean broken-links`  | Scan fragments for wiki-links pointing to nonexistent files. | [cleaning-and-purge](docs/cleaning-and-purge.md) |
| `creek clean duplicates`    | Run the dedup sweep and emit a review report. | [cleaning-and-purge](docs/cleaning-and-purge.md) |
| `creek clean report`        | Summary statistics on vault health. | [cleaning-and-purge](docs/cleaning-and-purge.md) |

---

## Security

Creek is **local-first** but **not encrypted at rest**. Vault content,
the embedding cache, and the Google Drive OAuth token are plaintext
on disk; the only filesystem-level defence is the `0o600` mode on the
OAuth token. If you're trusting Creek with intimate journal content,
read the threat model below before doing anything else.

- **[`docs/security/threat-model.md`](docs/security/threat-model.md)** — what
  Creek protects against, what it doesn't, and the explicit non-goals.
- **Audit log** — `<vault>/00-Creek-Meta/audit/` records every purge
  and redaction-apply (treat as a journal, not a tamper-evident log
  yet — see SEC-005).
- **Hygiene tldr:** enable disk encryption (FileVault / LUKS), keep
  the vault out of cloud-sync directories, run
  `creek gdrive --revoke` after any token exposure, and prefer
  `creek redact --scan` before importing any third-party content.

## Configuration

`creek-tools` is configured via Pydantic models defined in [`creek/config.py`](creek/config.py). The active configuration lives in `<vault>/00-Creek-Meta/creek_config.yaml` and can be edited by hand. Every section maps directly to a `BaseModel` you can find in `config.py`:

| Section | Class | What it controls |
|---------|-------|------------------|
| `llm`            | `LLMRoutingConfig`     | Per-stage provider+model routing (`default` / `classification` / `generation` / `frontend` + a `writing_desk` role map), or a legacy flat `LLMConfig` block. See [LLM providers](#llm-providers). |
| `embeddings`     | `EmbeddingsConfig`     | Sentence-transformer model, similarity thresholds. |
| `ocr`            | `OCRConfig`            | Tesseract path, languages, PSM mode. |
| `linking`        | `LinkingConfig`        | Embedding/temporal/eddy thresholds. |
| `classification` | `ClassificationConfig` | Auto-classify sources, confidence threshold, review-required sources. |
| `context`        | `ContextConfig`        | How non-user content (others' messages, collaborative docs) is handled. |
| `redaction`      | `RedactionConfig`      | Pattern enable list, exclusion globs, allow-list. |
| `google_drive`   | `GoogleDriveConfig`    | OAuth token cache, scopes, staging directory. |
| `cleaning`       | `CleaningConfig`       | Per-source filters (Discord, ChatGPT, Drive, Markdown) plus `validation`, `quality`, `deduplication`, `hygiene` sub-sections. |

See [`docs/configuration.md`](docs/configuration.md) for the full schema with examples.

### LLM providers

The cloud LLM backend is selectable via `llm.provider` in `creek_config.yaml`. The default `ollama` runs fully locally and needs no key or consent. Each cloud provider's **API key is read from the environment by its SDK** — it must **never** be written into `creek_config.yaml`, committed to the repo, or logged. Cloud egress additionally requires explicit consent (Ollama is exempt).

| Provider | `llm.provider` | Env key | Cloud-egress consent |
|---|---|---|---|
| Ollama (local) | `ollama` | — | not required |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | `CREEK_CLOUD_CONSENT=1` |
| OpenAI | `openai` | `OPENAI_API_KEY` (+ optional `OPENAI_BASE_URL`, or `llm.api_base`) | `CREEK_CLOUD_CONSENT=1` |
| Gemini | `gemini` | `GOOGLE_API_KEY` or `GEMINI_API_KEY` | `CREEK_CLOUD_CONSENT=1` |
| GPU-CC enclave | `enclave` | — (attestation config, not a key) | not required (attested) |

`CREEK_CLOUD_CONSENT=1` (also accepts `true` / `yes`) acknowledges that fragment content leaves the device; the legacy `CREEK_ANTHROPIC_CONSENT` is still honored as an alias. `llm.api_base` is OpenAI-specific (an OpenAI-compatible gateway); other providers ignore it. Install the matching optional extra (`[openai]` / `[gemini]`) for the cloud SDKs — see [Install](#install).

**BYOK (bring your own key).** Any stage can point at a **user-supplied** provider key with `llm.<stage>.api_key_env` — the **name** of an environment variable holding the key (e.g. `CREEK_BYOK_ANTHROPIC_KEY`), never the key value. This lets a user run the `generation` stage on their own Anthropic/OpenAI/Gemini subscription instead of an operator key; the key stays in the environment and is passed straight to the SDK (never persisted in config or logged). Cloud-consent still applies, and BYOK does **not** relax the tier gate — INTIMATE fragments are still redirected to a local/enclave provider by the `ModelRouter` chokepoint before any key is read. Omit `api_key_env` to use the provider's default env var (unchanged).

**Attested GPU-CC enclave (`enclave`).** An operator-run confidential-compute endpoint that gets GPU-quality output for the most private tier. It is classified `is_cloud=False` — and so is INTIMATE-eligible — because it **cryptographically verifies remote attestation before every completion**, and fails closed otherwise. Configure it (none of these is a secret — the pubkey is a *public* key) with `llm.enclave_url` (must be `https://`), `llm.enclave_expected_measurement` (the expected enclave image), and `llm.enclave_attestation_pubkey` (the hex Ed25519 **public** trust-root key). Each call challenges the enclave with a fresh nonce and requires an Ed25519 signature over `measurement || nonce` that validates under the configured public key — an impersonator without the private key, a replayed quote, a non-TLS URL, or a measurement mismatch all refuse and send nothing. The trust model and its boundaries (operator-provisioned root key, not yet a full vendor certificate chain) are recorded in [ADR-0006](docs/architecture/ADR/0006-enclave-attestation-trust-model.md).

The architectural decision to keep creek-tools' and CrawDad's provider abstractions decoupled is recorded in [ADR-0003](docs/architecture/ADR/0003-decoupled-provider-abstractions.md).

#### Per-stage model routing

The `llm` block routes **different pipeline stages to different backends in one run**. It accepts two shapes:

- **Legacy flat block** — `{ provider, model, … }` — still valid; it is promoted to the `default` stage, so every stage uses it (pre-routing behaviour, unchanged).
- **Per-stage map** — a `default` plus optional `classification` / `generation` / `frontend` overrides and a `writing_desk` role map. Unset stages fall back to `default`.

```yaml
llm:
  default:        { provider: ollama,    model: qwen3:8b }       # fallback for any unset stage
  classification: { provider: ollama,    model: qwen3:8b }       # local, Intimate-safe
  generation:     { provider: anthropic, model: claude-sonnet-4-6 }
  frontend:       { provider: ollama,    model: qwen3:14b }      # reserved (OpenClaw); no consumer yet
  writing_desk:                                                  # FEAT-041 subagent roles
    outliner:          { provider: ollama,    model: qwen3:8b }
    voice_drafter:     { provider: anthropic, model: claude-sonnet-4-6 }
    voice_line_editor: { provider: anthropic, model: claude-sonnet-4-6 }  # the 2nd voice role
```

Stage and role routing is centralised in `ModelRouter` (`creek/classify/llm/router.py`): a stage resolves to its own config or the `default`; a Writing Desk role resolves role → `generation` → `default`. The voice-role `voice_model` model-id override (below) is applied separately by `AuthorLLMClient.for_role()` (`creek/author/client.py`). Keys remain environment-only — never put an API key in any stage.

**Intimate-tier fragments never reach a cloud provider.** This is enforced at the single `ModelRouter` chokepoint: when an `Intimate` fragment would route to a cloud provider, it is **redirected to the local `default`** (with a `WARNING` for the audit trail); if even `default` is a cloud provider, the run **fails loudly** with `IntimateRoutingError` rather than egressing intimate content. The cloud-consent preflight likewise checks **every** stage, so a cloud provider configured for any stage requires `CREEK_CLOUD_CONSENT=1`.

For the Writing Desk voice roles (`voice_drafter` / `voice_line_editor`), an explicit `writing_desk` entry wins; otherwise the legacy `author.voice_model` fills the model id; otherwise the `generation` model stands.

**Migration.** No action is required — a flat `llm: { provider, model }` block keeps working as the `default`. To route per-stage, replace it with the map above. Environment override uses the JSON-string form (e.g. `CREEK_LLM='{"generation":{"provider":"anthropic"}}'`); the dotted `CREEK_LLM__PROVIDER` form is not wired (no `env_nested_delimiter`), so a flat `CREEK_LLM='{"provider":"anthropic"}'` is the supported flat override and is promoted to `default`.

**Embeddings are deliberately not provider-swappable.** `llm.provider` selects the *completion* backend only; the resonance/linking path always embeds locally via sentence-transformers (`embeddings.model`), so vault text never leaves the device for linking and the vector index stays stable. The decision and its reopening criteria are recorded in [ADR-0004](docs/architecture/ADR/0004-embeddings-stay-local.md).

**Provider capability notes.** The abstraction normalizes text, stop reason, and usage — three behaviors intentionally remain per-provider:

- **Rate limits**: a vendor 429 surfaces with the server's `Retry-After` hint preserved (never request state), and the classifier's retry loop honors it instead of retrying on the fixed delay.
- **Prompt caching** (cost asymmetry): Anthropic gets an explicit ephemeral `cache_control` block on the static system prefix; OpenAI relies on its vendor-side automatic prompt caching; Gemini sends the prefix plain (no explicit caching wired). Repeated static prefixes therefore bill differently per provider.
- **Streaming**: the abstraction is non-streaming by design — fine for batch classification and current consumers; a token-streaming UX would need a protocol extension.

**Live smoke test (model onboarding).** Unit tests mock every vendor SDK, so a model id is only proven by a real call. With the provider's key (and consent) in the env, one command makes a single tiny live request and asserts the normalized round-trip:

```bash
./scripts/test.sh --integration -k openai                                  # smoke the provider's default model
CREEK_SMOKE_MODEL=some-new-model ./scripts/test.sh --integration -k gemini # smoke a candidate model id
```

Each smoke skips cleanly when its key is absent, and the `integration` marker keeps them out of the default test selection and CI entirely.

---

## Source platforms

The ingestion pipeline currently ships **11** registered `Ingestor`s plus a read-only Google Drive downloader that routes mirrored files back through the matching ingestor:

`claude`, `chatgpt`, `discord`, `code`, `document` (.docx / .pdf), `markdown`, `spreadsheet` (.xlsx / .csv), `presentation` (.pptx), `image` (with OCR), `substack` (newsletter exports), and `generic` (fallback for unknown text).

`gdrive` is a downloader, not an ingestor — it stages files locally and dispatches each one to the appropriate ingestor by extension. The `other` enum value on `SourcePlatform` is reserved for downstream consumers (e.g. fragments synthesised from praxes) and has no parser.

Each ingestor follows the same four-stage contract — `discover` → `parse` → `convert_to_markdown` → `generate_frontmatter` — and writes one fragment per logical unit (per chat thread, per sheet, per slide deck, per file). See [`docs/ingestion.md`](docs/ingestion.md) for which is right for which export.

The pinned count is exercised by `tests/test_ingest_registry.py` so any change to `INGESTOR_REGISTRY` lights up red until this paragraph is updated alongside it.

---

## Development

All quality gates run from `creek-tools/` via the project scripts:

```bash
./scripts/check-all.sh          # Run all 7 gates (lint, format, typecheck, complexity, security, tests, coverage)
./scripts/fix-all.sh             # Auto-fix lint + format
./scripts/test.sh                # Unit tests (default)
./scripts/test.sh --all          # Unit + integration + e2e
./scripts/test.sh --coverage     # With coverage report
./scripts/lint.sh --fix          # Ruff lint with auto-fix
./scripts/format.sh --check      # Format check
./scripts/typecheck.sh           # MyPy strict
./scripts/security.sh            # Bandit + pip-audit
./scripts/complexity.sh          # Radon / Xenon
```

Quality thresholds are non-negotiable — see [`CLAUDE.md`](CLAUDE.md) for the full standards. Workflow is TDD-first with a 4-gate flow (tests → local → CI → review LGTM).

---

## License

MIT.
