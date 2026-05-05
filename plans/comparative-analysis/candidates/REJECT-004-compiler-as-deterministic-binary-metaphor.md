# REJECT-004: The "Compile = Deterministic Binary" Metaphor

**Verdict:** REJECT
**Source system:** Karpathy LLM Wiki community elaborations (NOT Karpathy himself)
**Affects:** Creek Vault data layer (mostly: the integration plan's framing)
**Roadmap target:** N/A
**Estimated complexity:** N/A
**Conflicts with non-negotiables?** voice (indirectly)

## What it is

Several community write-ups of Karpathy's pattern have extended the "compile" framing into a literal compiler analogy: "PDFs and notes are the source code; the wiki is the binary" ([MindStudio compiler-analogy post](https://www.mindstudio.ai/blog/karpathy-llm-knowledge-base-compiler-analogy)). Some implementations (e.g., [`ussumant/llm-wiki-compiler`](https://github.com/ussumant/llm-wiki-compiler)) treat compile as a discrete batch operation `/wiki-compile` analogous to `make`.

Karpathy himself uses "compile" in the casual software-engineering-borrowed sense, not as a formal claim about determinism. He does not describe one-way deterministic transformation; the gist's compile step is interactive ("discusses key takeaways with you") and explicitly co-evolves with the human.

## Why it's interesting

The compiler metaphor is seductive because it suggests that the wiki is a clean, deterministic, reproducible artifact. It implies that running compile twice gives the same wiki. It implies that a wiki page is *exactly* what its source produces, the way an object file is exactly what its `.c` produces.

None of that is true for LLM-generated synthesis. The wiki page is one *interpretation* of the source produced by an LLM at a moment in time. Re-running compile produces a different page. The interpretation is lossy. The source-to-wiki relationship is many-to-one (multiple sources contribute to one page) and one-to-many (one source touches 10–15 pages), with no recovery path from page back to source content.

For a project where downstream consumers (drafts, Substack posts) read from compiled pages, treating compile as deterministic is a category error that masks the lossy-compression risk.

## Fit with Creek Vault and/or CrawDad

It doesn't, and the integration plan should explicitly avoid the framing.

When the integration plan describes `creek compile` (or whatever the verb becomes — possibly `creek synthesize`, possibly just `creek lint --rebuild`), it should:

- Not describe it as deterministic.
- Not describe synthesis pages as "binaries" or "outputs" of a compile step.
- Always carry per-claim provenance (ADOPT-006's confidence tiers) so the lossy-compression risk is visible in the data, not hidden by metaphor.
- Treat compile re-runs as expected to differ; flag the differences as potentially interesting (synchronicity-flavored) rather than as build failures.

## Reasoning if rejected or deferred

The verdict won't flip. The compiler analogy is rhetorically convenient and substantively wrong. If the user wants to keep the *word* "compile" (as in `creek compile`, mirroring Karpathy's vocabulary), that's fine — language is shorthand. What's rejected is the *implication* that synthesis is deterministic.

The right vocabulary for the operation is closer to "summarize and cross-link" or "synthesize," but "compile" is shorter and Karpathy made it standard in the community. Use the word; reject the metaphor.

## Dependencies

- Adjacent to: ADOPT-001 (three-layer architecture), ADOPT-006 (confidence tiers — these make the lossy-compression risk visible).

## Acceptance criteria

N/A — this is a metaphor rejection. The acceptance criterion is documentary: any future doc that describes compile as deterministic, or synthesis pages as "outputs" or "binaries," gets corrected against this file.
