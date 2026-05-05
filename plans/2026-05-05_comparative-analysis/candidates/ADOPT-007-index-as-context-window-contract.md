# ADOPT-007: `index.md` as Context-Window Contract

**Verdict:** ADOPT
**Source system:** Karpathy LLM Wiki
**Affects:** Creek Vault data layer (CrawDad reads it at session start)
**Roadmap target:** v1
**Estimated complexity:** S
**Conflicts with non-negotiables?** none

## What it is

Karpathy's pattern keeps an `index.md` at the wiki root that lists every page with a short summary. The architectural commitment: **`index.md` must fit in a single LLM context window.** Navigation happens by in-context reasoning over the index, not by retrieval. The LLM reads `index.md` first, then pulls specific pages by name as needed. At ~100 articles / ~400K words this works without any retrieval pipeline.

Cited from [the gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) and elaborated in `cablate/llm-atomic-wiki` (which discusses the empirical scaling threshold).

## Why it's interesting

This is the architectural claim that justifies "no vector DB at personal-knowledge-base scale." Not "vector DBs are bad" — *"at this scale, the index fits."* It's a real, bounded engineering claim, and the breakthrough is treating the size discipline as load-bearing rather than as a UX preference.

For Creek, the analog isn't quite "list every fragment with a summary" — that doesn't fit even at modest scale. The analog is "list every *compiled-layer page* with a one-line description." Creek has, generously, ~10 frequency-index notes, several dozen Threads, several dozen Eddies, ~10 voice registers, plus a handful of Praxis notes — well under 200 pages of compiled material. That fits.

## Fit with Creek Vault and/or CrawDad

The Creek index document — call it `00-Creek-Meta/Vault-Index.md` or fold it into the audit report (ADOPT-005) — would contain:

- One line per Thread (status, fragment count, first-seen, last-seen).
- One line per Eddy (fragment count, threads-flowing-through, last-active).
- One line per Frequency-index note (fragment count, recent dosage trend).
- One line per Wavelength Mode profile (sample descriptors).
- One line per Voice register (exemplar count, last refreshed).
- One line per Praxis (status, review interval).
- One line per Decision (status, last activity).
- The current wavelength snapshot at the top — phase, mode, dosage trend.

Total: a few hundred lines, well within any current Claude context window.

CrawDad reads this at session start, in the same way Graphify-installed agents read `GRAPH_REPORT.md` via the `PreToolUse` hook. The bot's "what's going on?" answer is grounded in the index without any tool calls.

## Translation if adapted

Two adaptations:

1. **Don't make this a separate document from the audit report (ADOPT-005).** They're really the same artifact. The audit report is "the index plus what's interesting this week." Have one file, `00-Creek-Meta/State/latest.md`, that serves both roles.
2. **Wavelength snapshot at the very top.** Karpathy's `index.md` is structural; Creek's is *interpretive* — what page you reach for depends on where the human is on the cycle. Putting the wavelength snapshot at the top primes both the LLM and the human to read the rest of the index in context.

The size discipline must be enforced. If the compiled layer grows past what the index can summarize in a single context window, the response is *not* to add retrieval — it's to consolidate the compiled layer (which is what `creek lint`'s synchronicity and tag-cluster checks should be doing anyway). The index size is a quality signal: if it's drifting toward unwieldy, the compiled layer is fragmenting.

## Dependencies

- Depends on: ADOPT-001 (index describes the compiled layer, which must exist as a layer).
- Same artifact as: ADOPT-005 (audit report). Implementing one implements the other.

## Acceptance criteria

- `creek state` (or whatever the audit-report command is named) generates an index section that lists every compiled-layer page with a one-line summary.
- The current wavelength snapshot appears at the top of the document.
- A test pins the maximum size of the index section (e.g., 50K tokens) and fails the build if it exceeds the budget — forcing the human to reckon with compiled-layer fragmentation rather than silently growing a retrieval pipeline.
- CrawDad's session-start reads this artifact before any tool call.
