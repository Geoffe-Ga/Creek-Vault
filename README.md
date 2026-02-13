# Creek Tools

A Python CLI and data pipeline for organizing large volumes of semi-structured personal data — chat exports, documents, notes, messages — into an interlinked [Obsidian](https://obsidian.md/) knowledge base with semantic classification and NLP-driven discovery.

## What It Does

Creek Tools ingests data from multiple sources, normalizes it, classifies it along configurable taxonomies, discovers semantic connections between documents, and outputs a richly interlinked Obsidian vault.

**Pipeline stages:**

1. **Redaction** — Pattern-based scanning for secrets, API keys, and PII before any processing
2. **Ingestion** — Source-specific parsers (chat exports, Discord, Google Drive, PDFs, images via OCR) normalize to UTF-8 markdown with structured YAML frontmatter
3. **Classification** — Rule-based pre-classification + LLM-assisted tagging across multiple dimensions (topic, emotional tone, confidence level, voice register)
4. **Linking** — Embedding-based semantic similarity, temporal proximity analysis, and cluster detection to surface connections across sources
5. **Indexing** — Generated index notes, reports, and Dataview queries for vault navigation

**Key capabilities:**

- Multi-source ingestion (Claude/ChatGPT exports, Discord, Google Drive, markdown, PDF, DOCX, XLSX, PPTX, images)
- Local-first architecture — default LLM inference via Ollama, embeddings via sentence-transformers, no cloud calls required
- Privacy-tiered processing with consent gating and full audit trail
- Deterministic fragment IDs for idempotent re-processing
- Configurable taxonomy system with support for multi-label classification

## Architecture

```
creek-tools/              # Monorepo root
├── creek-tools/          # Python package & pipeline
│   ├── creek/            # Source (flat layout)
│   │   ├── ingest/       # Source-specific parsers
│   │   ├── redact/       # Secret detection & redaction
│   │   ├── classify/     # Rule-based + LLM classification
│   │   ├── link/         # Embedding similarity & clustering
│   │   ├── generate/     # Index, report, and voice generation
│   │   ├── vault/        # Markdown + frontmatter writer
│   │   └── cli.py        # Typer CLI entry point
│   ├── tests/
│   ├── scripts/          # Quality check scripts
│   └── pyproject.toml
├── scripts/Ontology/     # System specification & taxonomy definitions
├── .github/workflows/    # CI/CD (GitHub Actions)
└── CLAUDE.md             # AI-assisted development context
```

## Tech Stack

| Layer | Tools |
|---|---|
| Language | Python 3.11+ |
| CLI | Typer, Rich |
| Data models | Pydantic v2 |
| NLP / Embeddings | sentence-transformers (local), scikit-learn |
| LLM classification | Ollama (local, default) or Anthropic API (opt-in) |
| Document parsing | python-docx, python-pptx, openpyxl, pdfminer.six, pytesseract |
| Vault output | Markdown + YAML frontmatter (Obsidian-compatible) |
| CI/CD | GitHub Actions — lint, type check, test, security scan, complexity analysis |
| Quality | Ruff, Black, MyPy (strict), Bandit, Safety, Radon/Xenon, pytest (90%+ coverage) |

## Development

All commands run from `creek-tools/`:

```bash
cd creek-tools
pip install -r requirements-dev.txt
pre-commit install
```

```bash
./scripts/check-all.sh      # Run all quality checks (do this before every commit)
./scripts/test.sh            # Unit tests
./scripts/test.sh --coverage # Tests with coverage report
./scripts/lint.sh            # Ruff + MyPy
./scripts/format.sh --fix    # Auto-format
./scripts/security.sh        # Bandit + Safety
```

### Quality Standards

- **Test coverage:** ≥90% (branch coverage)
- **Type safety:** MyPy strict mode, all functions typed
- **Complexity:** ≤10 cyclomatic complexity per function
- **Security:** Zero Bandit/Safety findings
- **Style:** Ruff + Black + isort, zero violations

### Workflow

TDD-first, 4-gate process: write tests → local checks green → CI green → code review LGTM. See [`creek-tools/CLAUDE.md`](creek-tools/CLAUDE.md) for detailed development guidelines.

## Status

Early development. Foundation tooling and quality infrastructure are in place. Pipeline modules are being built incrementally per the [implementation plan](scripts/Ontology/creek_ontology_agent_prompt.md#14-implementation-plan).

## License

MIT
