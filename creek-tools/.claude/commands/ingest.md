---
description: Ingest a source into the vault (`creek ingest` via MCP).
argument-hint: "--type TYPE --input PATH"
---

# /creek ingest

Run the ingestion pipeline by calling the `creek.ingest` MCP tool. Supports the same 12 source platforms the CLI does: Claude exports, ChatGPT exports, Discord, Google Drive, markdown, PDF, DOCX, XLSX/CSV, PPTX, code, OCR images, generic text.

## What I will do

1. Call `creek.ingest` with the supplied `--type` and `--input` (a path under the user's data root).
2. The MCP tool runs the source-specific parser, applies the redaction pass, and writes fragments under `<vault>/01-Fragments/`.
3. Display the count of new fragments, the count flagged for review, and any redaction matches.

## When to use

- After exporting a fresh Claude / ChatGPT conversation.
- To process a new batch of journals, documents, or screenshots.
- After updating the redaction rules in `creek_config.yaml`.

## Related

- `/creek state --render` after ingestion to refresh the audit report.
- `/creek lint` to catch classification regressions on the new fragments.
