# GAP-005 — Top-level README "Key capabilities" list contradicts the package README, the registry, and the threat model

- **Severity:** High
- **Prod-readiness criterion threatened:** doc honesty

## Evidence

`README.md:15-22`:

```
- **Twelve source platforms** wired into a single registry — Claude,
  ChatGPT, Discord, Google Drive, code, documents (DOCX/PDF), markdown,
  spreadsheets (XLSX/CSV), presentations (PPTX), images (OCR), generic
  text, plus a fallback `other`.
- **Local-first by default.** Classification runs on Ollama; embeddings
  on `sentence-transformers`. The Anthropic API path is opt-in.
- **Privacy-tiered.** `Open` / `Personal` / `Intimate` privacy tiers
  with consent gating and a full audit trail.
- **Right-to-be-forgotten.** `creek purge` removes a fragment, source,
  date range, or the entire vault, scrubbing every reference along the
  way.
- **Deterministic.** Fragment IDs are hashed from `(source, timestamp,
  content)` so re-processing is idempotent.
- **Voice-aware generation.** The `creek skills` / `creek mine` /
  `creek draft` flow turns vault contents into a per-frequency Voice
  Skill Tree and uses it to draft new essays in your style.
```

Cross-checks against the package README, the registry, and the threat
model:

| README bullet | Reality | File:line |
|---|---|---|
| "Twelve source platforms" | 11 ingestors + 1 gdrive *downloader* (not an ingestor) per `creek-tools/README.md:172-176` | `creek/ingest/__init__.py:55-67` |
| List names `code, documents, markdown, spreadsheets, presentations, images, generic text` | Registry also has `substack` (added per CHANGELOG); README omits it | `creek/ingest/__init__.py:55-67` |
| "plus a fallback `other`" | `other` is **not** a fallback ingestor — the package README explicitly says: *"The `other` enum value on `SourcePlatform` is reserved for downstream consumers (e.g. fragments synthesised from praxes) and has no parser."* | `creek-tools/README.md:175-176` |
| "consent gating" | Consent is **per source ingestion event**, recorded one-shot to `Processing-Log/consent-log.json` (`creek/consent.py:1-15`). Tier-based **filtering** in generation/draft is a separate mechanism. The README phrasing implies tier-coupled consent that does not exist. | `creek/consent.py:37-78`, `creek/classify/privacy_filter.py` |
| "scrubbing every reference along the way" | Embeddings cache not scrubbed (see GAP-001), most vault folders not scrubbed (see GAP-004), threat model openly contradicts this (`docs/security/threat-model.md:123-125`) | GAP-001, GAP-004 |
| "full audit trail" | True for the *purge* and *redact* audits (hash-chained). The README does not make clear what *isn't* audited (general pipeline runs land in `Processing-Log/`, explicitly *"not compliance-grade, allowed to be lossy"* per `docs/cleaning-and-purge.md:198`). | `creek/audit/log.py`, `docs/cleaning-and-purge.md:198` |

The "Status" paragraph (README.md:91) compounds the issue with a
present-tense list of capabilities that includes the same overstated
RTBF claim.

## Why it matters

A new user evaluating creek-tools reads the top-level README first.
Doc-honesty is a top-line v1 criterion: every present-tense capability
the README advertises has to be true of the code as it ships, today. The
bullet list currently fails that test on at least three of six bullets
in non-trivial ways:

1. The platform count and `other`-as-fallback claims are factually
   wrong against the registry.
2. The "scrubbing every reference" claim contradicts the threat model
   in the same docs tree.
3. The "consent gating" phrasing conflates per-source consent (an
   ingestion gate) with per-fragment tier filtering (a downstream
   filter), implying a coupling that does not exist.

These aren't tone issues — a reviewer or a privacy-conscious user who
spot-checks any one of them will lose trust in the rest.

## Reproduction

Static — read `README.md:15-22` and the cross-referenced files in the
evidence table.

```bash
# Platform count + substack omission:
grep -n "register_ingestor\|INGESTOR_REGISTRY" creek-tools/creek/ingest/__init__.py
# 'substack' appears in the registry.

# `other` fallback claim vs. reality:
grep -n "other" creek-tools/creek/ingest/__init__.py
# `other` is not registered as an ingestor.

# RTBF claim vs. threat model:
grep -n "embedding cache" creek-tools/docs/security/threat-model.md
# "Wipe the embedding cache when you wipe vault content. It is *not*
#  automatically purged when you `creek purge vault`."
```

## Acceptance criteria

Closed when README.md:15-22 (and the Status paragraph at line 91) are
rewritten so that **every** sentence is true of the code as it ships
today. Concretely:

1. Source-platform bullet says either "11 source ingestors and a
   read-only Google Drive downloader" (mirror `creek-tools/README.md`)
   or lists each by name including `substack`. Drop the
   "fallback `other`" phrase or rewrite it as "plus a reserved `other`
   enum value for downstream consumers (no parser)."
2. Privacy bullet separates the two concepts: per-source consent
   logging at ingestion *and* tier-aware filtering in downstream
   stages. The word "consent gating" is either dropped or defined.
3. RTBF bullet either (a) reflects the post-GAP-001 /
   GAP-004 fix (and only after those land), or (b) lists what is *not*
   scrubbed today (embeddings cache, derived content under 04 / 05 /
   06 / 07 / 08 / 09 / 10) with a pointer to the threat model.
4. Status paragraph either drops the present-tense capability list or
   tightens it to match the rewritten Key Capabilities bullets.
5. Any reviewer reading the new bullets and grepping the code finds no
   contradictions of the kind catalogued above.

## Files affected

- `README.md` (lines 15-22, 91-92)
- `creek-tools/docs/security/threat-model.md` (consistency check against
  the rewritten RTBF bullet)

## Dependencies / blockers

The RTBF bullet text depends on whether GAP-001 and GAP-004 are fixed
or accepted before v1. If fixed, the new text can keep the strong
promise. If accepted, the new text must enumerate the carve-outs. This
finding cannot land cleanly until that product decision is made.
