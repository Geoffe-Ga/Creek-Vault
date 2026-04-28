# creek-tools

The Python pipeline behind the **Creek** knowledge organization system. `creek-tools` ingests semi-structured personal data — chat exports, documents, notes, screenshots — redacts sensitive content, classifies it along the [APTITUDE / Archetypal Wavelength](../00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md) ontology, links semantically related fragments, and writes a richly interlinked Obsidian vault.

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
| LLM classification     | Ollama running locally, or `ANTHROPIC_API_KEY` for the cloud path |

To install everything required for development plus the full quality toolchain:

```bash
pip install -r requirements-dev.txt
pre-commit install
```

---

## Quickstart

```bash
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
| `creek purge vault`     | Nuclear option: destroy every fragment, thread, and eddy. Asks for explicit confirmation. | [cleaning-and-purge](docs/cleaning-and-purge.md) |

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
| `creek skills`   | Generate the **Voice Skill Tree** under `<vault>/creek-skills/` (one `SKILL.md` per frequency, phase, mode, register, plus thread/eddy/meta skills). | [generation](docs/generation.md#voice-skill-tree) |
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

## Configuration

`creek-tools` is configured via Pydantic models defined in [`creek/config.py`](creek/config.py). The active configuration lives in `<vault>/00-Creek-Meta/config.yaml` and can be edited by hand. Every section maps directly to a `BaseModel` you can find in `config.py`:

| Section | Class | What it controls |
|---------|-------|------------------|
| `llm`            | `LLMConfig`            | Provider (`ollama` or `anthropic`), model name, batch size, retries. |
| `embeddings`     | `EmbeddingsConfig`     | Sentence-transformer model, similarity thresholds. |
| `ocr`            | `OCRConfig`            | Tesseract path, languages, PSM mode. |
| `linking`        | `LinkingConfig`        | Embedding/temporal/eddy thresholds. |
| `classification` | `ClassificationConfig` | Rules vs LLM, batch size, review thresholds. |
| `redaction`      | `RedactionConfig`      | Pattern enable list, exclusion globs, allow-list. |
| `gdrive`         | `GoogleDriveConfig`    | OAuth token cache, root folder, mime allow-list. |
| `cleaning`       | `CleaningConfig`       | Per-source filters (Discord, ChatGPT, Drive, Markdown). |
| `validation`     | `ValidationConfig`     | Required frontmatter fields, encoding policy. |
| `quality`        | `QualityConfig`        | Minimum content length, deduplication strategy. |

See [`docs/configuration.md`](docs/configuration.md) for the full schema with examples.

---

## Source platforms

The ingestion pipeline currently supports **12** source platforms, each backed by a registered `Ingestor`:

`claude`, `chatgpt`, `discord`, `gdrive`, `code`, `documents` (.docx / .pdf), `markdown`, `spreadsheet` (.xlsx / .csv), `presentation` (.pptx), `images` (with OCR), `generic` (fallback for unknown text), and `other`.

Each ingestor follows the same four-stage contract — `discover` → `parse` → `convert_to_markdown` → `generate_frontmatter` — and writes one fragment per logical unit (per chat thread, per sheet, per slide deck, per file). See [`docs/ingestion.md`](docs/ingestion.md) for which is right for which export.

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
