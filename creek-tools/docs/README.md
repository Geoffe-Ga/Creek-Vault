# creek-tools docs

Task-oriented how-to guides for the `creek-tools` pipeline. The top-level [`creek-tools/README.md`](../README.md) is the install/quickstart entry point; the guides here go deep on each workflow.

| Guide | When to read it |
|-------|-----------------|
| [getting-started.md](getting-started.md) | First time running `creek`. Walks you from a clean Obsidian vault to a fully indexed pipeline run. |
| [ingestion.md](ingestion.md) | Picking the right `--type` for each export. Lists the 12 ingestors and their file formats. |
| [redaction.md](redaction.md) | Scanning for secrets/PII before ingestion, applying redactions in place, and rendering the vault-wide review queue. |
| [classification.md](classification.md) | Rule-based vs LLM classification, frequency / phase / privacy-tier tagging, and the review queue. |
| [linking.md](linking.md) | Resonances (embeddings), threads (temporal), eddies (density), and how to read the resulting graph. |
| [generation.md](generation.md) | Voice Skill Tree, idea mining, draft generation, and wavelength reports. |
| [cleaning-and-purge.md](cleaning-and-purge.md) | Vault hygiene (orphans, duplicates, broken links) and right-to-be-forgotten purges. |
| [configuration.md](configuration.md) | Full schema reference for `<vault>/00-Creek-Meta/creek_config.yaml`. |
| [wiring-contract.md](wiring-contract.md) | Adding a CLI command or MCP tool: how to declare the effect it must produce, and how the contract test proves it. |

Every guide is intentionally task-oriented: it answers "how do I do X" rather than "what classes does the module contain" — for the latter, read the module docstrings in [`creek/`](../creek/), which are kept above the 95% interrogate threshold.
