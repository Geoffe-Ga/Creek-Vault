# Creek

A Python CLI and pipeline for organizing large volumes of semi-structured personal data — chat exports, documents, notes, screenshots, messages — into an interlinked [Obsidian](https://obsidian.md/) knowledge base with semantic classification and NLP-driven discovery.

## What it does

The pipeline runs in five stages:

1. **Redaction** — pattern-based scanning for secrets, API keys, and PII *before* anything else touches the data.
2. **Ingestion** — source-specific parsers (Claude/ChatGPT exports, Discord, Google Drive, markdown, PDF, DOCX, XLSX/CSV, PPTX, code, images via OCR, generic text) normalize everything to UTF-8 markdown with structured YAML frontmatter.
3. **Classification** — rule-based pre-classification plus opt-in LLM-assisted tagging across multiple dimensions (topic, voice register, frequency, archetypal phase, privacy tier, confidence).
4. **Linking** — embedding-based semantic similarity, temporal proximity, and density-based eddy detection surface connections across sources.
5. **Generation** — index notes, weekly/monthly wavelength reports, the Voice Skill Tree, blog-idea mining, and skill-stack-driven essay drafting.

## Key capabilities

- **Twelve source platforms** wired into a single registry — Claude, ChatGPT, Discord, Google Drive, code, documents (DOCX/PDF), markdown, spreadsheets (XLSX/CSV), presentations (PPTX), images (OCR), generic text, plus a fallback `other`.
- **Local-first by default.** Classification runs on Ollama; embeddings on `sentence-transformers`. The Anthropic API path is opt-in.
- **Privacy-tiered.** `Open` / `Personal` / `Intimate` privacy tiers with consent gating and a full audit trail.
- **Right-to-be-forgotten.** `creek purge` removes a fragment, source, date range, or the entire vault, scrubbing every reference along the way.
- **Deterministic.** Fragment IDs are hashed from `(source, timestamp, content)` so re-processing is idempotent.
- **Voice-aware generation.** The `creek skills` / `creek mine` / `creek draft` flow turns vault contents into a per-frequency Voice Skill Tree and uses it to draft new essays in your style.

## Repository layout

```
Creek-Vault/                       # The Obsidian vault itself (this repo)
├── 00-Creek-Meta/Ontology/        # Master ontology spec + APTITUDE / Wavelength docs
├── 01-Fragments/                  # Atomic content units (one MD per chat / doc / sheet / slide deck)
├── 02-Threads/                    # Narrative currents
├── 03-Eddies/                     # Topic clusters
├── 04-Praxis/                     # Actionable insights
├── 05-Wavelength/                 # Phase reports
├── 06-Frequencies/                # APTITUDE frequency notes
├── 07-Voice/                      # Voice Skill Tree, drafts, register profiles
├── 08-Decisions/                  # Decision context notes
├── 09-Reference/                  # External reference material
├── 10-Liminal/                    # In-between content awaiting classification
├── creek-tools/                   # The Python CLI + pipeline (this is where the code lives)
└── CLAUDE.md                      # Top-level repo guidance for Claude Code
```

The Obsidian vault and the `creek-tools` codebase share one git repository, so changes to the pipeline and to the vault content are versioned together.

## Quickstart

See [`creek-tools/README.md`](creek-tools/README.md) for install instructions, the full command reference, and configuration. The end-to-end task guides are under [`creek-tools/docs/`](creek-tools/docs/):

| If you want to… | Read |
|-----------------|------|
| Run your first pipeline end to end | [`docs/getting-started.md`](creek-tools/docs/getting-started.md) |
| Understand which ingestor fits which export | [`docs/ingestion.md`](creek-tools/docs/ingestion.md) |
| Scan and apply redactions before you ingest | [`docs/redaction.md`](creek-tools/docs/redaction.md) |
| Configure rule-based vs LLM classification | [`docs/classification.md`](creek-tools/docs/classification.md) |
| Surface resonances, threads, and eddies | [`docs/linking.md`](creek-tools/docs/linking.md) |
| Generate reports, mine ideas, draft essays | [`docs/generation.md`](creek-tools/docs/generation.md) |
| Keep the vault tidy or exercise right-to-be-forgotten | [`docs/cleaning-and-purge.md`](creek-tools/docs/cleaning-and-purge.md) |
| Edit `<vault>/00-Creek-Meta/creek_config.yaml` confidently | [`docs/configuration.md`](creek-tools/docs/configuration.md) |

## Tech stack

| Layer | Tools |
|-------|-------|
| Language | Python 3.11+ (CI tests 3.11, 3.12, 3.13) |
| CLI | Typer, Rich |
| Data models | Pydantic v2 |
| NLP / embeddings | `sentence-transformers` (local), scikit-learn |
| LLM classification | Ollama (default, local) or Anthropic API (opt-in) |
| Document parsing | `python-docx`, `python-pptx`, `openpyxl`, `pdfminer.six`, `pytesseract` |
| Vault output | Markdown + YAML frontmatter (Obsidian-compatible) |
| CI / CD | GitHub Actions — lint, type check, test, security scan, complexity analysis, automated Claude review |
| Quality | Ruff, MyPy (strict), Bandit, pip-audit, Radon/Xenon, pytest (≥90 % branch coverage) |

## Status

Phase-3 of the implementation plan is complete: full ingestion across twelve source platforms, rule-based and LLM-assisted classification, embeddings + temporal + eddy linking, the Voice Skill Tree, idea mining, draft generation, weekly/monthly reports, redaction, and right-to-be-forgotten purges. Refactor follow-ups (typed parse intermediates, configurable header detection) are tracked in the issue backlog.

## License

MIT.
