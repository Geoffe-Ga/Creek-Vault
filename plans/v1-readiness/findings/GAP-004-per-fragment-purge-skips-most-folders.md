# GAP-004 — Per-fragment / per-source purge skips reference scrubbing in most vault folders

- **Severity:** High
- **Prod-readiness criterion threatened:** data safety, doc honesty

## Evidence

`creek-tools/creek/purge/engine.py:143-190` (`_purge_single`, the worker
called by both `purge_fragment` and `purge_source`):

```python
# Decrements counts only inside 02-Threads and 03-Eddies:
self._decrement_counts("02-Threads", thread_ids)   # line 177-180
self._decrement_counts("03-Eddies", eddy_ids)      # line 181-184
# Scrubs wikilinks vault-wide (this part is fine), then unlinks:
self._scrub_wikilinks(title, exclude=frag_file)    # line 176
frag_file.unlink()                                  # line 186-187
```

`_scrub_wikilinks` does walk every `.md` in the vault — that part is
already in good shape. The gap is everything *else* that a fragment can
be referenced by, namely:

- `04-Praxis/` — actionable insights derived from fragments
- `05-Wavelength/` — weekly/monthly wavelength reports that name
  fragments by ID and excerpt (see
  `creek-tools/creek/cli.py:1056-1076`,
  `creek/generate/wavelength.py`)
- `06-Frequencies/` — frequency index pages
- `07-Voice/Drafts/` — generated drafts carry full
  `source_fragments: [...]` provenance frontmatter (see
  `creek-tools/creek/cli.py:1765-1856` and the agent verification of
  the `creek draft` flow)
- `08-Decisions/`, `09-Reference/`, `10-Liminal/` — user-curated
  content that may reference fragments
- `<vault>/creek-skills/` — the Voice Skill Tree; per-frequency
  SKILL.md files carry exemplar fragment references

Wikilink scrubbing depends on the `[[…]]` syntax. References stored as
YAML fields (`source_fragments: [frag-9c1f3a2b8e02, …]`,
`exemplar_fragment: frag-…`) are **not** wikilinks and would survive
`_scrub_wikilinks` even if it walked these directories.

The README's promise (line 20): *"`creek purge` removes a fragment,
source, date range, or the entire vault, scrubbing every reference along
the way."* The cleaning-and-purge doc (line 6) repeats it.

## Why it matters

RTBF is binary from the user's perspective: either the trace is gone or
it isn't. The current contract silently exempts the folders most likely
to carry generated/derived content of the deleted fragment — exactly the
content that retains the fragment's *meaning*, not just its title.

If a user purges an intimate-tier fragment that was the seed for a draft
under `07-Voice/Drafts/`, the draft remains, the
`source_fragments: [frag-…]` frontmatter remains, and the body
(synthesized from the fragment's content) remains. The user thinks the
trace is gone; an MCP query or vault grep still surfaces it.

## Reproduction

```bash
cd creek-tools
creek init --vault /tmp/gap004-vault
echo "seed content" > /tmp/gap004-src.md
creek ingest --type markdown --input /tmp/gap004-src.md --vault /tmp/gap004-vault
FRAG=$(ls /tmp/gap004-vault/01-Fragments | head -1 | sed 's/.md$//')

# Stage a Praxis note + a Drafts note that reference the fragment by ID:
mkdir -p /tmp/gap004-vault/04-Praxis /tmp/gap004-vault/07-Voice/Drafts
cat > /tmp/gap004-vault/04-Praxis/p1.md <<EOF
---
source_fragments: [$FRAG]
---
A praxis derived from $FRAG.
EOF
cat > /tmp/gap004-vault/07-Voice/Drafts/d1.md <<EOF
---
source_fragments: [$FRAG]
---
A draft built from $FRAG (no [[wikilink]]).
EOF

creek purge fragment --id "$FRAG" --vault /tmp/gap004-vault \
    --confirm-text "I understand this is irreversible"

# Today: both files still contain the fragment ID.
grep -r "$FRAG" /tmp/gap004-vault/04-Praxis /tmp/gap004-vault/07-Voice
# Expected post-fix: no matches, or the file is removed, or both files
# have a documented `[purged: <id>]` placeholder.
```

Failing test outline:

```python
def test_purge_fragment_scrubs_yaml_provenance_in_drafts_and_praxis(tmp_path):
    vault = _seed_vault(tmp_path)
    frag_id = _seed_fragment(vault, "content")
    _seed_draft_with_provenance(vault, frag_id)
    _seed_praxis_with_provenance(vault, frag_id)
    engine = PurgeEngine(vault_path=vault, confirmation=VAULT_PURGE_CONFIRMATION)
    engine.purge_fragment(frag_id)
    for path in vault.rglob("*.md"):
        assert frag_id not in path.read_text(), f"{path} still references purged fragment"
```

## Acceptance criteria

Closed when **one of** the following holds (the choice is a product
decision, not a correctness one):

A. **Scrub everywhere.** `_scrub_wikilinks` (or a sibling)
   walks every folder in `_VAULT_CONTENT_FOLDERS` plus
   `<vault>/creek-skills/`, and additionally scrubs YAML frontmatter
   fields known to carry fragment IDs (`source_fragments`,
   `affected_fragments`, `exemplar_fragment`, etc. — enumerate from
   `creek/models.py`). Replace with `[purged]` marker rather than
   silently removing entries, so the user can see what was lost.

B. **Document the restriction.** README, cleaning-and-purge.md, and the
   `creek purge --help` text are rewritten to state that only
   `02-Threads/` and `03-Eddies/` (and wiki-links anywhere) are
   scrubbed, and derived content under `04-Praxis/`, `05-Wavelength/`,
   `07-Voice/`, etc. survives purge by design.

(A) is the right answer for a tool that markets RTBF. (B) is the
acceptable answer if (A) is too expensive for this release — but the
docs must be updated either way.

If (A): tests cover scrubbing of YAML provenance + wiki-links in every
relevant folder.

If (B): the README's "scrubbing every reference" bullet is rewritten
verbatim and the CLI `--help` for `purge fragment` / `purge source`
names the un-scrubbed folders.

## Files affected

- `creek-tools/creek/purge/engine.py`
- `creek-tools/creek/models.py` (if YAML field enumeration lives there)
- `creek-tools/tests/test_purge.py`
- `creek-tools/docs/cleaning-and-purge.md`
- `creek-tools/creek/cli.py` (CLI help text for `purge` subcommands)
- `README.md` (top-level — see GAP-005 for the consolidated rewrite)

## Dependencies / blockers

If approach (A) is chosen, plan after GAP-001 — both touch the same
purge codepath and the same audit semantics.
