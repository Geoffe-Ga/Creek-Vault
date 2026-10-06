# Creek

Creek is a command-line tool that takes the pile of personal data you already have — chat exports, documents, notes, screenshots, messages — and turns it into a linked [Obsidian](https://obsidian.md/) vault that sorts itself by theme and finds the connections you'd miss on your own.

## What it does

The pipeline runs in five stages:

1. **Redaction** — pattern-based scanning for secrets, API keys, and PII *before* anything else touches the data.
2. **Ingestion** (`creek ingest`) — reads eleven kinds of source (Claude/ChatGPT exports, Discord, markdown, PDF/DOCX, XLSX/CSV, PPTX, code, images via OCR, Substack, plain text), plus a read-only Google Drive downloader. Whatever goes in comes out as plain markdown notes with a small metadata header Obsidian understands.
3. **Classification** — rule-based pre-classification plus opt-in LLM-assisted tagging across multiple dimensions (topic, voice register, frequency, archetypal phase, privacy tier, confidence).
4. **Linking** — finds notes that say similar things, notes written around the same time, and the clusters where a topic keeps pulling your attention (we call those eddies), across every source.
5. **Generation** — index notes, weekly/monthly wavelength reports, the Voice Skill Tree, blog-idea mining, and skill-stack-driven essay drafting.

## Key capabilities

- **Eleven source ingestors and a Google Drive downloader.** Claude, ChatGPT, Discord, code, documents (DOCX/PDF), markdown, spreadsheets (XLSX/CSV), presentations (PPTX), images (OCR), Substack, and a generic text fallback. `gdrive` is a read-only downloader, not an ingestor — it stages mirrored files locally and dispatches each one to the matching ingestor by extension.
- **Local-first by default.** Classification runs on Ollama; embeddings on `sentence-transformers`. The Anthropic API path is opt-in.
- **Privacy-tiered with two distinct consent surfaces.** Fragments carry an `Open` / `Personal` / `Intimate` tier. *Ingestion* gates the first run for each source on an explicit consent prompt, logged once to `00-Creek-Meta/Processing-Log/consent-log.json`. *Downstream stages* (generation, mining, drafts, MCP queries) filter or refuse fragments by tier independently. A tamper-evident log records every time a privacy tier is overridden, something is redacted, or something is purged.
- **Right-to-be-forgotten.** `creek purge` deletes a note, a whole source, a date range, or the entire vault — and cleans up after itself: every link to the deleted note, every mention of its ID in other notes (including the sources list of a draft that drew on it), and its row in the search index go too. If the note was intimate-tier, the stub under `10-Liminal/Compost/intimate-stubs/` holding its full body is deleted as well. The only thing kept is the tamper-evident log that says a purge happened.
- **Safe to re-run.** Each note's ID comes from where it came from, when, and what it says — so running Creek twice over the same export never creates duplicates.
- **Voice-aware generation.** The `creek skills` / `creek mine` / `creek draft` flow turns vault contents into a per-frequency Voice Skill Tree and uses it to draft new essays in your style.

## Repository topology

This repository is the **toolchain** plus the **canonical reference material**. Your personal vault — fragments, threads, journal, voice exemplars — lives *elsewhere on disk* and is never checked in.

```
Creek-Vault/                            # This repo (toolchain + canonical material)
├── creek-tools/                        # Python CLI + pipeline + canonical templates
│   └── creek/templates/
│       ├── vault/                      # Canonical vault scaffold (.gitkeep markers)
│       ├── skills/                     # Canonical schema-skill tree (*.SKILL.md)
│       └── AGENTS.md                   # Canonical agent contract template
├── crawdad/                            # Discord bot — chat-side interface to the vault (consumes creek-tools-mcp)
├── docs/Ontology/                      # Canonical ontology specification
├── CLAUDE.md                           # Repo guidance for Claude Code
└── README.md                           # You are here
```

```
~/Obsidian/Creek-Vault/                 # Your vault (NOT this repo). Scaffolded by `creek init`.
├── 00-Creek-Meta/{Ontology,Skills,...} # Per-vault: ontology copy, schema skills, config, logs
├── 01-Fragments/                       # Atomic content units (journal, conversations, etc.)
├── 02-Threads/, 03-Eddies/, 04-Praxis/ # Compiled narrative / cluster / actionable layers
├── 05-Wavelength/, 06-Frequencies/     # APTITUDE / Archetypal Wavelength notes
├── 07-Voice/, 08-Decisions/            # Voice skill tree, decision frameworks
├── 09-Reference/, 10-Liminal/          # External references, in-between content
└── AGENTS.md                           # Per-vault agent contract (deployed from template)
```

## Quickstart

```bash
pip install -e creek-tools
creek init --vault ~/Obsidian/Creek-Vault     # Required: pick a vault path OUTSIDE this repo.
creek skills sync --vault ~/Obsidian/Creek-Vault   # Re-deploy upstream skills after upgrades.
```

`creek init` refuses paths inside a git repository by default; pass `--allow-in-repo` to override (with a warning).

See [`creek-tools/README.md`](creek-tools/README.md) for the full command reference and configuration. End-to-end task guides are under [`creek-tools/docs/`](creek-tools/docs/):

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

## Hosting Creek for someone else

Running Creek on a server for someone else? See the [container runtime guide](creek-tools/docs/container-runtime.md).

## Status

Phase-3 of the implementation plan is complete: eleven registered ingestors plus the Google Drive downloader, rule-based and LLM-assisted classification, embeddings + temporal + eddy linking, the Voice Skill Tree, idea mining, draft generation, weekly/monthly reports, redaction, and right-to-be-forgotten purges (including embedding-cache rows and YAML/body fragment-ID mentions). Refactor follow-ups (typed parse intermediates, configurable header detection) are tracked in the issue backlog.

## License

MIT.

## Knowledge graph

This repo publishes a [graphify](https://github.com/Graphify-Labs/graphify)
knowledge graph as assets on the rolling
[`knowledge-graph` release](https://github.com/Geoffe-Ga/Creek-Vault/releases/tag/knowledge-graph)
(never committed — the ~30 MB weekly-regenerated artifact would bloat git
history) so the [adepthood](https://github.com/Geoffe-Ga/adepthood) hub can
merge it into the ecosystem pan-graph. A weekly workflow rebuilds it
(AST-only, free) and refreshes the semantic layer over `docs/Ontology/**` and
`docs/decisions/**` when an `ANTHROPIC_API_KEY` secret is configured — see
`.github/workflows/graph-update.yml`. To query locally, build with
`graphify extract . --code-only` or download the release's `graph.json`.

```bash
pip install graphifyy==0.9.17
graphify query "how does the link engine detect eddies"
graphify update .   # after code changes; no API key needed
```
