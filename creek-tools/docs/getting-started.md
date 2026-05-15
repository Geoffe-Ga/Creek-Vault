# Getting started

End-to-end walkthrough: clone the repo, install `creek-tools`, configure your vault, and run your first pipeline against a small export.

## 1. Install

```bash
git clone https://github.com/Geoffe-Ga/Creek-Vault.git
cd Creek-Vault/creek-tools
pip install -e .
```

The pipeline imports optional dependencies lazily, so you only need to install the ones you actually use. For the smoke test in this guide we'll stick to **markdown** — no extras required.

## 2. Initialize the vault

Per FEAT-019, the repository is the *toolchain* — your personal vault lives elsewhere on disk. Pick a vault path *outside* this repo (suggested default: `~/Obsidian/Creek-Vault/`), then scaffold it:

```bash
creek init --vault ~/Obsidian/Creek-Vault
```

`--vault` is required (no implicit default — that's how personal data ends up in repos). `creek init` refuses paths inside a git repository by default; pass `--allow-in-repo` to override with a warning.

The scaffold materialises the canonical folder topology, copies the ontology spec, the schema-skill tree, and `AGENTS.md`, and writes a starter `creek_config.yaml`:

```
~/Obsidian/Creek-Vault/
├── 00-Creek-Meta/{Ontology,Skills,Templates,Scripts,Processing-Log,creek_config.yaml}
├── 01-Fragments/                  # One MD per ingested unit
├── 02-Threads/, 03-Eddies/, 04-Praxis/, 05-Wavelength/
├── 06-Frequencies/, 07-Voice/, 08-Decisions/, 09-Reference/, 10-Liminal/
└── AGENTS.md
```

After upgrading `creek-tools`, re-deploy upstream schema-skill changes with `creek skills sync --vault <path>` (add `--force` to overwrite local edits). To re-copy templates without touching user content, run `creek init --vault <path> --refresh`.

A minimal starter `creek_config.yaml` (already written by `creek init`):

```yaml
llm:
  provider: ollama
  model: mistral
embeddings:
  model: all-MiniLM-L6-v2
  similarity_threshold: 0.78    # cosine cutoff for a resonance edge
classification:
  confidence_threshold: 0.7
```

The full schema with every field is documented in [configuration.md](configuration.md).

## 3. Run a smoke test

Drop a couple of markdown files into a temporary source directory:

```bash
mkdir -p /tmp/creek-smoke
cat > /tmp/creek-smoke/idea.md <<'EOF'
# A note on attention

When I notice the urge to refresh a feed, that's information. The body wants
something. Often it isn't the feed.
EOF
```

Run the full pipeline:

```bash
creek process --source /tmp/creek-smoke --vault ~/Obsidian/Creek-Vault
```

`creek process` chains `ingest → redact → classify → link → index`. When it completes you should see:

- A new fragment under `01-Fragments/` with deterministic frontmatter (`fragment_id`, `source.platform`, `ingested`, classification stubs).
- An entry in the review queue (printed by `creek review`) if the classifier wasn't confident.
- A wavelength snapshot under `05-Wavelength/` if you've configured `report.wavelength` (covered in [generation.md](generation.md#reports)).

## 4. The four-stage ingestor contract

Every ingestor — markdown, Discord, Drive, OCR, the lot — follows the same contract. If you're writing a new one, this is the shape you implement:

```
discover(path)              -> list[RawDocument]
parse(raw)                  -> list[ParsedFragment]
convert_to_markdown(frag)   -> str
generate_frontmatter(frag)  -> dict[str, Any]
```

`creek process` walks `discover()`, hands the bytes through `parse()`, and the resulting fragments are written via the vault writer. Fragment IDs are hashed from `(source, timestamp, content)` so re-running the pipeline against the same input is **idempotent** — content that hasn't changed isn't re-emitted.

## 5. Run individual stages

Each stage has its own command for incremental work. Common patterns:

```bash
# Re-ingest one source type after fixing a parser bug.
creek ingest --type discord --input ~/exports/discord.zip --vault ~/Obsidian/Creek-Vault

# Re-classify with a different method without re-ingesting.
creek classify --vault ~/Obsidian/Creek-Vault --method llm --batch-size 25

# Add new resonances and refresh thread/eddy detection.
creek link --vault ~/Obsidian/Creek-Vault --method embeddings

# Print a wavelength snapshot.
creek report --type wavelength --period weekly --vault ~/Obsidian/Creek-Vault
```

## 6. Where to go next

| You want to… | Read |
|--------------|------|
| Pick the right `--type` for a specific export | [ingestion.md](ingestion.md) |
| Scan secrets *before* ingesting | [redaction.md](redaction.md) |
| Tune classification | [classification.md](classification.md) |
| Connect ideas across sources | [linking.md](linking.md) |
| Generate the Voice Skill Tree, mine ideas, draft essays | [generation.md](generation.md) |
| Edit `creek_config.yaml` confidently | [configuration.md](configuration.md) |
