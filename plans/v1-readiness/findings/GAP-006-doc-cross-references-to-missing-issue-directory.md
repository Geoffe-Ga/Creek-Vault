# GAP-006 — Documentation cross-references point to a `plans/git-issues/` directory that does not exist in the repo

- **Severity:** High
- **Prod-readiness criterion threatened:** doc honesty, unattended reliability

## Evidence

`creek-tools/docs/security/threat-model.md:144-156`:

```
Issue files live at the repository root under `plans/git-issues/`:

- **SEC-002** — Redaction pattern coverage gaps
- **SEC-003** — Symlink refusal in redaction (resolved)
- **SEC-004** — Prompt injection hardening (resolved)
- **SEC-005** — Audit log tamper-evidence
- **SEC-006** — Privacy-tier enforcement in mine/draft
- **SEC-008** — OAuth token hygiene (resolved)
- **OPS-002** — Non-interactive purge refusal (resolved)

Each is filed under its short ID; search the issue tracker or the
in-repo `plans/git-issues/` directory for the full text.
```

But:

```
$ find . -path ./.git -prune -o -type d -name "git-issues" -print
(no results)

$ find . -path ./.git -prune -o -name "SEC-005*" -print
(no results)

$ find . -path ./.git -prune -o -name "INC-006*" -print
(no results)
```

The directory does not exist. Cross-referenced from at least:

- `creek-tools/docs/security/threat-model.md:144`
- `creek-tools/docs/cleaning-and-purge.md` (the OPS-002 migration note
  at line 151)
- `creek-tools/CLAUDE.md` (references `plans/git-issues/` for
  ARCH-style decisions)
- Inline code comments naming SEC-006, INC-006 (e.g.
  `creek/classify/privacy_filter.py:78`,
  `creek/link/embeddings.py:7,51`,
  `creek/templates/skills/privacy-tier.SKILL.md:77`)
- `creek-tools/scripts/coverage-waivers.txt` references
  `plans/git-issues/TEST-002-coverage-aggregate-hides-low-modules.md`

GitHub side (verified via MCP): the `geoffe-ga/creek-vault` repository
has 4 open issues — all labeled `enhancement`, none with `SEC-*` or
`INC-*` identifiers. So a reader cannot resolve SEC-005 / SEC-006 / INC-006
via either route.

## Why it matters

The documentation makes a specific, falsifiable promise: *"search the
issue tracker or the in-repo `plans/git-issues/` directory for the full
text."* Neither route works. A v1 user who reads the threat model and
follows "see SEC-005 for the audit tamper-evidence story" finds nothing.

The damage is twofold:

1. **Operational:** a user encountering a real failure has no way to
   look up the known caveat that the docs gestured at.
2. **Trust:** if the cross-references are stale, the *content* the
   cross-references gesture at is reasonably suspected of being stale
   too. The threat model is precisely the document a privacy-conscious
   user reads with the most scrutiny.

## Reproduction

Static:

```bash
cd /home/user/Creek-Vault
# Verify the cross-referenced directory is missing:
test -d plans/git-issues && echo "exists" || echo "missing"
# Verify the SEC/INC IDs are not findable as files:
find . -path ./.git -prune -o \( -name "SEC-*" -o -name "INC-*" \) -print | wc -l
# 0
# Verify they are not open issues in GitHub either:
gh issue list --search "label:security OR SEC- OR INC-" --repo geoffe-ga/creek-vault
# (cannot run from this environment; use the GitHub MCP equivalent)
```

## Acceptance criteria

Closed when **one of** the following holds:

A. **Restore the directory.** `plans/git-issues/SEC-002.md`,
   `SEC-005.md`, `SEC-006.md`, `INC-006.md`, `OPS-002.md`,
   `TEST-002-coverage-aggregate-hides-low-modules.md`, plus every other
   file the docs / coverage-waivers / code comments reference, exists
   under `plans/git-issues/` with at least a one-paragraph summary, a
   status (open / closed / superseded), and a link to the GitHub issue
   if any.

B. **Rewrite the cross-references.** Every reference in docs,
   `coverage-waivers.txt`, and code comments to `plans/git-issues/`
   (or to a bare `SEC-N` / `INC-N` ID) is rewritten to a live URL
   (GitHub issue, ADR file, or inline documentation). The threat
   model's "Cross-references" footer is either deleted or replaced
   with live links.

C. **Hybrid.** A `plans/INDEX.md` (or `docs/decisions/INDEX.md`)
   enumerates every SEC-/INC-/OPS-/TEST- ID with its current status and
   a link. Docs cross-references point at that file instead of at a
   bare ID.

The acceptance test: a fresh-clone reader who follows every
cross-reference in `threat-model.md`, `cleaning-and-purge.md`, and the
code comments cited above lands somewhere they can read. No
404s, no dead grep results.

## Files affected

- `plans/git-issues/` (creation) **or**
- `creek-tools/docs/security/threat-model.md` (lines 144-156, plus any
  inline "(see SEC-…)" references)
- `creek-tools/docs/cleaning-and-purge.md` (OPS-002 reference at line
  151, and any other)
- `creek-tools/scripts/coverage-waivers.txt`
- `creek-tools/CLAUDE.md` (any reference to `plans/git-issues/`)
- Code comments naming bare SEC-/INC- IDs (see Evidence list)

## Dependencies / blockers

None for option B (smallest diff). Option A is the right answer if
those issue files actually exist somewhere and were lost in a
restructure; check `git log -- plans/` before deciding.
