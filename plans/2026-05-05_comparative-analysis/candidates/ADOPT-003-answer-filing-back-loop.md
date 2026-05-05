# ADOPT-003: Answer-Filing-Back Loop

**Verdict:** ADOPT
**Source system:** Karpathy LLM Wiki
**Affects:** both — Creek Vault data layer + CrawDad agent layer
**Roadmap target:** v1 (foundational); v1.1 for the CrawDad → vault auto-filing path
**Estimated complexity:** M
**Conflicts with non-negotiables?** none

## What it is

Karpathy's gist: *"Good answers can be filed back into the wiki as new pages. A comparison you asked for, an analysis, a connection you discovered — these are valuable and shouldn't disappear into chat history."* The mechanic: a good Q&A response gets written to `wiki/syntheses/<topic>.md`. The next query sees the synthesis page in `index.md`. The wiki compounds.

Community implementations like [AgriciDaniel/claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) surface this as `/save [name]` to file the current conversation as a wiki note.

## Why it's interesting

This is the single mechanic that differentiates "wiki" from "stored chat history." Without it, every conversation re-derives the same insights from scratch. With it, the corpus grows along the axis of what the human has actually asked and used — a more useful selection than what's been ingested.

For CrawDad specifically, this is the difference between a Discord bot that reflects-and-forgets and one that compounds with the user. A reflection that lands well in conversation should become a fragment, possibly a synthesis page, possibly a Praxis note — not vanish.

## Fit with Creek Vault and/or CrawDad

Creek already has the destination locations:

- A reflection that names a new pattern → `10-Liminal/Unnamed/` (let it cluster for the next Unnamed Digest).
- A reflection that names a known pattern in a fresh way → `02-Threads/` or `03-Eddies/` (synthesis page).
- A reflection that produces a practice → `04-Praxis/`.
- A reflection that names a contradiction → `10-Liminal/Paradoxes/` (NOT resolved).
- A reflection that produces a draft fragment of writing → eventual `07-Voice/Drafts/`.

What's missing is the routing. Creek-tools has `creek-skills/`, `creek mine`, `creek draft` — none of these have a "save this answer back to the vault" path. CrawDad needs this primitive from day one.

The save operation needs:
1. A target classification — fragment, synthesis page, praxis, paradox, draft, or unnamed.
2. A provenance trail — which conversation, which fragments contributed, which skills were activated.
3. A title — auto-generated, human-overridable.
4. Frontmatter — wavelength phase + mode + dosage + frequency must be carried through (CrawDad knows the user's current phase from recent fragments; the saved page inherits it).

## Translation if adapted

The destination decision is a classification problem. The simplest viable v1 is to ask the user — `/crawdad save` with a prompt for "thread / eddy / praxis / paradox / unnamed / draft." The smartest v1.1 is to have CrawDad propose the classification and let the user confirm, using the same rules-then-LLM pipeline as `creek classify`.

Two important rules:
- **Personal-tier and intimate-tier conversations don't auto-file.** The privacy-tier system in `creek-tools` already gates body content; the save loop must respect it. Default behavior: intimate → never auto-file; personal → save title + summary, not full body; open → full save.
- **Auto-filing is opt-in per command.** A `/crawdad reflect` that auto-files everything is the wrong default. The save action is explicit, both because of privacy and because the user owns what enters the vault.

## Dependencies

- Depends on: ADOPT-001 (three-layer architecture defines where to file).
- Pairs with: ADOPT-008 (Haiku-router on the CrawDad side dispatches "save this" intents).

## Acceptance criteria

- A `creek save` CLI command exists that takes a body, a target type, optional fragment provenance, and writes a properly-classified note with full frontmatter.
- The save command honors privacy tiers — intimate content cannot be auto-saved; personal content saves title-only by default.
- Saved notes carry provenance frontmatter that links back to the conversation/source they came from (Discord message ID for CrawDad, conversation ID for Claude Code, etc.).
- A `/crawdad save` (or equivalent) interaction is implemented in the CrawDad agent layer (v1.1 milestone) that wraps `creek save` via the MCP server (see ADAPT-004).
- A regression test verifies that saving a paradox routes to `10-Liminal/Paradoxes/`, not to a synthesis page.
