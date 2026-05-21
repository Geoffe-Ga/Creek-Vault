# Operational Checklist for OpenClaw Agents

## Session start checklist

1. Confirm working directory and branch.
2. Confirm Creek CLI is available:
   - `creek --help`
   - if missing: `pip install -e ./creek-tools`
3. Confirm target vault path exists and has expected structure.
4. Identify whether task is read-only, write-to-vault, or write-to-repo.

## Decision matrix

- Need structured synthesis from raw fragments → `creek compile`
- Need direct answer with provenance awareness → `creek query`
- Need hygiene / gap / contradiction detection → `creek lint`
- Need to persist outcome into vault → `creek save`
- Need canonical tree in new vault → `creek init`
- Need upstream schema-skill refresh → `creek skills sync`
- Need voice-conditioning tree regeneration → `creek skills generate`

## Evidence discipline

For each execution block, record:
- Intent
- Command
- Exit status
- Material outputs
- Interpretation

## Quality gate checklist (before finalizing)

- Commands used are canonical and reproducible.
- Output summary references real files/paths.
- Privacy-tier assumptions are explicit.
- Contradictions are not flattened into fake consensus.
- Any bypass flags (`--bypass-compiled`, etc.) are justified.
