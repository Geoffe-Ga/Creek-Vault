# Karpathy LLM Wiki

## Source URLs

### Karpathy primary
- Original X post (Apr 2 2026): <https://x.com/karpathy/status/2039805659525644595>
- Gist (`llm-wiki.md`, Apr 3 2026): <https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f>
- "Idea file" follow-up: <https://x.com/karpathy/status/2040470801506541998>
- Farzapedia signal-boost (Apr 5 2026): <https://x.com/karpathy/status/2040572272944324650>

### Community elaborations
- DAIR.AI Academy: <https://academy.dair.ai/blog/llm-knowledge-bases-karpathy>, <https://academy.dair.ai/blog/how-to-build-an-llm-knowledge-base>
- VentureBeat: <https://venturebeat.com/data/karpathy-shares-llm-knowledge-base-architecture-that-bypasses-rag-with-an>
- MindStudio walkthroughs: <https://www.mindstudio.ai/blog/andrej-karpathy-llm-wiki-knowledge-base-claude-code>, <https://www.mindstudio.ai/blog/karpathy-llm-knowledge-base-compiler-analogy>, <https://www.mindstudio.ai/blog/llm-wiki-vs-rag-markdown-knowledge-base-comparison>
- aimaker.substack: <https://aimaker.substack.com/p/llm-wiki-obsidian-knowledge-base-andrej-karphaty>
- "The Schema Is the Product": <https://cozypet.github.io/llm-wiki-schema/>
- LLM Wiki v2 (extension gist): <https://gist.github.com/rohitg00/2067ab416f7bbe447c1977edaaa681e2>
- Critical: "LLM Wiki is a Bad Idea" (Mehul Gupta): <https://medium.com/data-science-in-your-pocket/andrej-karpathys-llm-wiki-is-a-bad-idea-8c7e8953c618>
- Reference implementations: [ScrapingArt stack](https://github.com/ScrapingArt/Karpathy-LLM-Wiki-Stack), [AgriciDaniel/claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian), [Ar9av/obsidian-wiki](https://github.com/Ar9av/obsidian-wiki), [shannhk/llm-wikid](https://github.com/shannhk/llm-wikid), [cablate/llm-atomic-wiki](https://github.com/cablate/llm-atomic-wiki)

## What it is

Not a system — an architectural pattern with a folder structure, a discipline, and a schema document. The pattern: raw sources are immutable; an LLM compiles them per-source into a markdown wiki; queries read the wiki, not the raw sources; good answers file back into the wiki; periodic lint passes find contradictions, orphans, and gaps. Three layers, asymmetric write authority. Karpathy describes it as an "idea file" rather than a product; the value is in the idea, not the code.

## Architecture

Three layers with explicit ownership:

| Layer | Owner (write) | Reader | Mutability |
|---|---|---|---|
| `raw/` | Human | LLM | Append-only |
| `wiki/` | LLM | Both | LLM-curated, human-reviewed |
| `CLAUDE.md` (or `AGENTS.md`) | Both, co-evolved | LLM at session start | Slow-changing |

`wiki/` typically organizes as `sources/`, `entities/`, `concepts/`, `syntheses/`, plus an `index.md` (table of contents — must fit one context window) and an append-only `log.md`.

**Compile** is per-source, manual, interactive: drop a file in `raw/`, tell the LLM "ingest this," it reads, discusses takeaways with you, writes a summary in `sources/`, updates `index.md`, and touches "10–15 wiki pages" of entities/concepts that the source affects, then logs the operation. Compile is closer to "summarize-and-cross-link" than to anything LLVM does; the compiler analogy is a community gloss.

**Query** routes through the wiki: LLM reads `index.md`, pulls specific pages by name into context, answers. Raw sources are referenced only to verify or to fix wiki fidelity. No vector search, no embedding pipeline at this scale (~100 articles, ~400K words is Karpathy's stated working size).

**Answer-filing-back** is the most underrated mechanic: a good Q&A response gets written to `wiki/syntheses/<topic>.md`. The next query sees the synthesis page in `index.md`. The wiki compounds.

**Lint pass** is manual, on-demand, LLM-driven. Looks for contradictions across pages, stale claims newer sources superseded, orphan pages with zero inbound links, important concepts mentioned but lacking their own page (3+ mentions threshold in some implementations), missing cross-references, data gaps fillable by web search. Output is a structured report; humans approve fixes.

## Wins

- **The schema as a first-class artifact.** `CLAUDE.md` is the load-bearing innovation. Pre-LLM personal wikis assumed a human reader who could infer conventions; this pattern requires conventions be *executable* by an agent. Frontmatter contracts, link conventions, lint rules — all explicit.
- **Asymmetric write authority.** Humans abandon wikis because the maintenance burden grows faster than the value. Letting the LLM absorb the bookkeeping cost while humans stay in review-and-curate mode is the actual move.
- **Compile/query separation.** Synthesized pages are produced once and read many times. The artifact compounds rather than being session-bound.
- **`index.md`-as-context-window contract.** Navigation by in-context reasoning over a hand-sized table of contents, not by retrieval. At ~100 articles this works; the architectural commitment to keeping `index.md` sized for the context window is a real claim, not just a UX preference.
- **Lint as named operation.** Naming the maintenance loop is half the battle. Karpathy doesn't specify the lint pass in detail, but explicitly calling it out as a separable cadence is what most personal-wiki tooling has missed.
- **File-back-good-answers loop.** This single mechanic differentiates "wiki" from "stored chat history" in a way no other personal-knowledge tool gets right.

## Costs

- **Lossy compression with quiet hallucination propagation.** Every compile step introduces small errors; over many ingests they compound. Once a claim is on a wiki page, it acts as a fact for the next compile cycle. Lint mitigates but doesn't eliminate. **This is the load-bearing risk for any voice-generation system built on top of a Karpathy-style wiki — the drafted essay's "facts" trace back to the wiki, not to the raw sources, and exact-quote fidelity is gone.**
- **Manual cadence.** Compile, lint, file-back-answers — none are automated. They run when the human remembers, which is the historical failure mode of every personal wiki ever.
- **Voice generation is out of scope.** Karpathy doesn't address it. The closest community piece (extendedbrain.substack) explicitly says "the last mile (voice, narrative, audience empathy, editorial judgement) is still a human job."
- **Audit/precision domains are a poor fit.** Multiple critiques converge: research/exploration is fine; anything requiring exact citations or audit trails is risky.
- **Monolithic CLAUDE.md** grows past ~5000 tokens and the agent ignores sections — community implementations recommend modular skill files.
- **Orphan/contradiction/staleness only surface if lint runs.**
- **Naming collisions in parallel/multi-agent compilation.** cablate observed `mcp-plus-skills.md` vs. `mcp-plus-skills-architecture.md` collisions.
- **Flat structure breaks past a few hundred items.**

## Relevance to Creek Vault, CrawDad, or both

**Creek Vault, primarily.** Karpathy's pattern is the closest neighbor to Creek's compiled-layer ambition — the Threads/Eddies/Frequency-index notes are aspirationally compiled but the system never quite commits, and queries currently route through fragments rather than through the compiled layer. Adopting Karpathy's discipline is the most consequential single change in the integration plan.

Specifically:
1. **Three-layer architecture** maps almost cleanly onto Creek's existing structure: `01-Fragments/` ↔ `raw/`, the derived layers (02-Threads, 03-Eddies, 06-Frequencies, parts of 05-Wavelength) ↔ `wiki/`, and there should be one canonical `CLAUDE.md`/`AGENTS.md` at the vault root that defines the schema for both Creek-Vault-the-vault and `creek-tools`-the-toolchain.
2. **Compile-then-query** becomes the new contract: `creek mine`, `creek draft`, `creek wavelength`, and CrawDad's reflective queries should route through the compiled layer first. The fragment layer remains primary as source of truth, but it's referenced (not retrieved-from) at query time.
3. **`creek lint` as a unified named operation** — Creek currently has Tag Garden, Unnamed Digest, Synchronicity Detection, Compost Tracking, and Paradox Preservation as five separate report types. Karpathy's "lint" is the unifying frame; they're really facets of the same vault-hygiene operation.
4. **Answer-filing-back loop** — when CrawDad answers a question well, the answer should land in `02-Threads/`, `03-Eddies/`, or a new synthesis location, not vanish into Discord history.
5. **`index.md`-as-context-window contract** — there should be one document at the vault root that summarizes the compiled layer in a context-window-sized way. The voice-skill tree is a candidate for what feeds this index, but the index itself is a missing artifact.

**Critical adaptations Creek must make** (these are *not* clean adopts):
- **The lint pass must NOT resolve paradoxes.** Karpathy's pattern treats contradictions as defects; Creek treats them as data. The Creek lint must explicitly route contradictions to `10-Liminal/Paradoxes/` rather than flagging them for resolution.
- **Synthesis pages must preserve provenance back to fragments.** The lossy-compression risk is amplified when downstream consumers (drafts, Substack posts) read from synthesis pages. Every claim on a compiled page must trace back to fragment IDs.
- **Wavelength phase must be a first-class compile dimension.** Karpathy's `entities/` and `concepts/` are flat; Creek's compiled layer must carry phase, mode, dosage, and frequency through to the synthesis.

**CrawDad, secondarily.** Karpathy's "ask Claude Code questions" is the conversational mode CrawDad needs to embody on Discord. The discipline of "queries read the wiki, not the raw sources" maps to CrawDad never directly searching `01-Fragments/` — it should always go through the compiled layer first.
