# ADOPT-002: `creek lint` — Unified Vault Hygiene Operation

**Verdict:** ADOPT
**Source system:** Karpathy LLM Wiki
**Affects:** Creek Vault data layer (CrawDad consumes outputs)
**Roadmap target:** v1
**Estimated complexity:** M
**Conflicts with non-negotiables?** liminal — must be adapted carefully (see "Translation if adapted")

## What it is

Karpathy's gist names a discrete, on-demand lint operation that finds: contradictions across pages, stale claims newer sources have superseded, orphan pages with zero inbound links, important concepts mentioned but lacking their own page, missing cross-references, and data gaps fillable by web search. Output is a structured report; humans approve fixes. Cited from [the gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) and elaborated in `cablate/llm-atomic-wiki` (which usefully splits lint into deterministic checks and semantic checks).

## Why it's interesting

Creek Vault already implements every piece of what Karpathy calls "lint" — but as five separate report types running on five different cadences with no unified entry point:

- `creek report --type tags` — Tag Garden (orphan tags, growth, consolidation)
- `creek report --type unnamed` — Unnamed Digest (cluster surfacing for uncategorizable content)
- `creek report --type synchronicity` — Surprising cross-source resonances
- `creek report --type compost` — Resolved/abandoned threads
- `creek report --type paradox` — Contradiction detection

These are facets of one operation: "look at the vault, name what's growing, what's stale, what's contradicting itself, what's orphaned, what's surfacing." Karpathy gave it a name. Creek should adopt the name.

## Fit with Creek Vault and/or CrawDad

The implementation path is mostly a CLI ergonomics change:

```bash
creek lint --vault ~/Obsidian/Creek-Vault                 # default: all checks
creek lint --vault ~/Obsidian/Creek-Vault --check tags    # single check
creek lint --vault ~/Obsidian/Creek-Vault --report-only   # don't propose fixes
creek lint --vault ~/Obsidian/Creek-Vault --since 7d      # incremental
```

Each existing report-type generator becomes a *check* under one entry point. Output is one consolidated report (see ADOPT-005 for the audit-report pattern) plus a per-check section.

CrawDad consumes the report: when the user says "what's surfacing this week?" or "what should I be writing about?", CrawDad reads the latest `creek lint` output rather than re-querying the vault from scratch.

## Translation if adapted

This is the load-bearing adaptation: **the Creek lint must NOT resolve paradoxes.** Karpathy's lint treats contradictions as defects; Creek treats them as data. The translation:

1. **Contradictions** → routed to `10-Liminal/Paradoxes/` (not flagged for resolution). The existing `ParadoxDetector` already does this; lint just orchestrates it.
2. **Orphans** → not flagged as defects unless they're orphan *compiled* pages. Orphan fragments are normal; isolated insights belong somewhere even when they're not yet linked.
3. **"Important concepts mentioned but lacking their own page"** → routed to `10-Liminal/Unnamed/` for the next Unnamed Digest, not auto-created. Force-classification violates liminal preservation.
4. **Stale claims** → flagged for review only. Claims aren't deleted; they're versioned.
5. **Data gaps** → presented as questions, not as web-search prompts. CrawDad's conversational mode can pick these up later.

The split between deterministic and semantic checks (cablate's contribution) is worth keeping: broken wiki-links, orphan detection, tag co-occurrence, and frontmatter validation are deterministic and cheap; contradiction detection, synchronicity surfacing, and unnamed clustering use embeddings/LLM and are expensive. `creek lint` should run deterministic checks always and semantic checks behind a flag (or on a longer cadence).

## Dependencies

- Depends on: ADOPT-001 (three-layer architecture — lint operates over the compiled layer).
- Pairs with: ADOPT-005 (audit report — lint output goes there), ADAPT-002 (four-worker decomposition — lint becomes the unified frame for what Curator/Janitor/Distiller/Surveyor do continuously).

## Acceptance criteria

- A `creek lint` CLI command exists, with `--check` for individual checks and a default that runs all of them.
- The five existing emergence report types are reachable as `--check` values (`tags`, `unnamed`, `synchronicity`, `compost`, `paradox`) without losing any current behavior.
- Lint output is a single consolidated markdown document (see ADOPT-005), per-section by check.
- Lint never auto-creates compiled pages, never resolves contradictions, never deletes orphan fragments — these are non-negotiable behaviors documented in the lint module's docstring.
- A regression test verifies that paradoxes detected during lint land in `10-Liminal/Paradoxes/`, not in a "to-fix" queue.
- Lint runs incrementally given `--since <duration>` (e.g., `--since 7d`); deterministic checks default to incremental, semantic checks default to full unless `--since` is passed.
