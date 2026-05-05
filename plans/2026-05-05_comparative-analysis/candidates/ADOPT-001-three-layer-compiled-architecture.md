# ADOPT-001: Three-Layer Architecture (Raw / Compiled / Schema)

**Verdict:** ADOPT
**Source system:** Karpathy LLM Wiki
**Affects:** Creek Vault data layer (with downstream effects on CrawDad)
**Roadmap target:** v1 — load-bearing for nearly everything else
**Estimated complexity:** L
**Conflicts with non-negotiables?** none — but adaptation is required to honor liminal preservation (see "Translation if adapted")

## What it is

Karpathy's pattern commits to three layers with explicit ownership: `raw/` (immutable, human-writable, LLM-readable), `wiki/` (LLM-curated synthesis, human-reviewed), and `CLAUDE.md` (the schema document, co-evolved). Queries route through the wiki; raw is consulted only to verify or to fix wiki fidelity. Cited from the [gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) and [DAIR.AI Academy reproductions](https://academy.dair.ai/blog/llm-knowledge-bases-karpathy).

## Why it's interesting

The asymmetric write authority is the move that makes a personal wiki sustainable. Humans abandon wikis because the bookkeeping cost outgrows the reading value; assigning the LLM the bookkeeping cost while humans stay in review-and-curate mode inverts that economics. The compile/query separation also makes the artifact compounding rather than session-bound — synthesized pages are produced once and read many times.

## Fit with Creek Vault and/or CrawDad

Creek Vault's existing structure already approximates this:

| Karpathy | Creek Vault |
|---|---|
| `raw/` | `01-Fragments/` |
| `wiki/sources/` | (nothing today — fragments serve as source notes) |
| `wiki/entities/` | (nothing today — would be person/place/concept notes) |
| `wiki/concepts/` | `06-Frequencies/` (per-frequency index notes) |
| `wiki/syntheses/` | `02-Threads/`, `03-Eddies/` |
| `wiki/index.md` | (missing — see ADOPT-007) |
| `wiki/log.md` | `00-Creek-Meta/Processing-Log/` (already append-only) |
| `CLAUDE.md` | `creek_ontology_agent_prompt.md` (in `00-Creek-Meta/Ontology/`) + scattered ADRs |

The structural fit is good. What's missing is the *contract*: queries today don't route through the compiled layer. `creek mine` reads fragments directly; `creek draft` pulls fragments as source material; CrawDad (when built) would naively reach for fragments too. Adopting this candidate means committing to the compile-then-query discipline, not just renaming folders.

The ontology spec at §10 already names "the most important folder" (`10-Liminal/`) as the place uncategorizable content goes. This is a Creek-specific addition to the three-layer model: there is a *fourth* layer, the liminal layer, that is neither raw nor compiled — it's where compilation explicitly fails and that's the point. The spec doesn't need to change; it accommodates this naturally.

## Translation if adapted

Even though the verdict is ADOPT, the pattern needs Creek-flavored translation:

1. **Wavelength as a first-class compile dimension.** Karpathy's `entities/` and `concepts/` are flat; Creek's compiled pages must carry phase, mode, dosage, and frequency through. The compile prompt must be wavelength-aware.
2. **Provenance preservation back to fragment IDs.** Every claim on a compiled page must trace back to the fragment(s) that produced it. The lossy-compression risk is amplified when downstream consumers (drafts, Substack posts) read from compiled pages. This is non-negotiable for voice fidelity.
3. **`10-Liminal/` is *not* a wiki sub-folder.** It's a fourth layer with different rules — content goes there when compilation explicitly fails or refuses. The lint pass (ADOPT-002) must route paradoxes and unnameable content there rather than treating them as defects.
4. **`creek_ontology_agent_prompt.md` is the schema.** Don't introduce a competing `CLAUDE.md`. The existing canonical spec *is* the schema document; if a vault-root `CLAUDE.md` or `AGENTS.md` is added, it should be a thin pointer to the ontology spec plus a short, executable contract for compile/query/lint.

## Dependencies

- Blocks: ADOPT-002 (lint), ADOPT-003 (file-back loop), ADOPT-007 (`index.md`), ADAPT-001 (topology clustering), most CrawDad candidates.
- Depends on: nothing in this candidate set, but operationally requires reconciliation of the spec/implementation drift on phase and mode names (see `LANDSCAPE.md` closing paragraph and `DELTA-MATRIX.md` synthesis).

## Acceptance criteria

- A short root-level `AGENTS.md` (or vault-root `CLAUDE.md`) exists, points to `creek_ontology_agent_prompt.md` as canonical, and defines the compile / query / lint / file-back contract in under ~3000 tokens.
- `creek mine`, `creek draft`, and any other downstream consumer have a documented "compiled-layer-first" query path: read the compiled page, fall back to fragments only when the compiled page is missing or insufficient.
- Compiled pages (Threads, Eddies, Frequency-index notes) carry per-claim provenance to fragment IDs in their frontmatter.
- The four-layer convention (raw / compiled / schema / liminal) is documented in the ontology spec or a sibling doc, with explicit rules for what does and doesn't move between layers.
- A regression test verifies that `creek draft` and `creek mine` route through the compiled layer in the absence of `--bypass-compiled` (or equivalent escape hatch).
