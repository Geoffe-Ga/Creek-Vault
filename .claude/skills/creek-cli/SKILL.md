---
name: creek-cli
description: >-
  Drive the `creek` CLI natively — run the pipeline and, above all, generate
  essays/drafts in the user's own voice. Use when the user asks to "write an
  essay in my voice", "draft something", "use the creek CLI", "ingest/classify/
  link my vault", "mine blog ideas", "build my voice fingerprint", or run any
  `creek <subcommand>`. Encodes the exact invocation (uv, working dir, env vars,
  cloud consent), the real vault locations, and the non-obvious draft gotchas so
  no exploration is needed. Do NOT use for editing creek-tools source/tests
  (use stay-green/work-issue) or a guided hand-held onboarding (use
  creek-walkthrough).
---

# creek-cli

The `creek` binary is **not on `PATH`**. It runs from the Python subproject via
`uv`. Get this wrapper right and every command works first try.

## Invocation contract (always do this)

The Bash tool resets cwd to the repo root each call, so `cd` every time and
export the env in the **same** command:

```bash
cd "$(git rev-parse --show-toplevel)"/creek-tools && \
export CREEK_CONFIG=<VAULT>/00-Creek-Meta/creek_config.yaml CREEK_ANTHROPIC_CONSENT=1 && \
uv run creek <subcommand> --vault <VAULT> [opts]
```

- `CREEK_CONFIG` → the vault's own config. Without it you get a "config not
  found; running with built-in defaults" warning and wrong privacy/redaction
  behaviour.
- `CREEK_ANTHROPIC_CONSENT=1` → **required** for any step that calls the LLM
  (`draft`, `classify --method llm`, `mine`). Without it: "requires explicit
  consent … Set CREEK_ANTHROPIC_CONSENT=1". `ANTHROPIC_API_KEY` is already in
  the environment. Setting consent egresses vault content to Anthropic — fine
  for the user's own vault on their own key; that is the whole point of the tool.

### Finding the vault

No vault path is recorded here: this repo is public, and a vault location is
the operator's own business (FEAT-019 — no user vault content or locations
live in this repo). Discover them instead:

```bash
find ~ -name creek_config.yaml 2>/dev/null   # ignore /tmp pytest hits
```

Confirm which vault the user means if more than one answers, and keep any
personal default in `.claude/settings.local.json`, which is git-ignored.

## Command surface

`init` · `process` (redact→ingest→classify→link→index) · `ingest --type <t>`
· `redact` · `classify` · `link` · `compile` · `mine` (blog/essay ideas) ·
`draft` (essay in voice) · `save` · `report` · `state` · `lint` · `review` ·
`clean` · `purge` · `skills` · `gdrive` · `compost`.

Ingest types: `chatgpt claude code discord document generic image markdown
presentation spreadsheet substack`.

Run `uv run creek <cmd> --help` for any subcommand's flags.

## ⭐⭐ The single biggest lever: how much of the user reaches the model

A draft only sounds like the user when the prompt is *fueled* with the user's
own material and stance. Measured on two real drafts of the same Casa Lupe essay,
the difference between "sounds exactly like me" and "generic essay voice" was
**entirely the invocation**, not any pipeline setting. The good one had:

1. **A hand-written `--voice-core` file** carrying the scene + the precise
   argument/angles + the instruction *"write in my first-person voice, lean on
   the source fragments, not generic essay tropes."* This is the heaviest lever.
   Without it the model has no brief and no order to imitate the user.
2. **`--seed-topic "<loaded keyword>"`** (e.g. `unworthy`) → pulled a **21-fragment
   resonance chain of the user's real journals** AND activated a full skill stack
   incl. the **`confessional` register** skill. The model then weaves the user's
   *verbatim* lines in. Registers live in `<VAULT>/creek-skills/registers/`
   (`confessional`, `raw`, `conversational`, `prophetic`, …).
3. The weak path to AVOID for voice: `--seed-fragment` on a **fresh, unclassified**
   note → 1 source fragment, `activated_skills: []`, no register, no resonance.
   It composes from almost nothing of the user, so it defaults to generic register.

**The strong recipe** = write a `--voice-core` brief (scene + angle + "imitate my
source") **and** seed by a loaded topic keyword so the resonance chain + register
+ frequency/phase skills all fire. The FEAT-040.8 "Voice targets" preamble and the
voice-fidelity guard (rewrite to low `voice_distance`) are secondary — they can
even sand off idiosyncrasy, so don't rely on them to create voice.

```bash
# 1. Write the brief to a file — scene, the exact angle/argument, and the order
#    to imitate the user's own writing. Keep it specific; this is what steers tone.
cat > /tmp/voice-core.txt <<'EOF'
You are writing a personal essay IN MY OWN FIRST-PERSON VOICE (I am Geoff). Draw
voice, phrasing, and texture from the Source fragments below — my journals/notes.
SCENE: <one specific scene, no invented characters or sub-plots>.
THE ARGUMENT: <the precise distinction/claim the essay must hold>.
CLOSE on <the honest, unresolved question>.
EOF

# 2. Seed by a loaded keyword present in the corpus (check: grep -rli "<kw>" 01-Fragments)
uv run creek draft --vault <VAULT> \
  --seed-topic "unworthy" \
  --voice-core /tmp/voice-core.txt \
  --max-tokens 3500
```

### Decomposing a raw epiphany into a brief (the digestible shape)

When the user hands you a raw, run-on insight, don't paste it in — re-shape it
into this skeleton (proven on `unworthy` and `equanimity`). Keep the user's own
rhetorical moves (e.g. an "either way / both at once" parallelism) intact:

```
You are writing a personal essay IN MY OWN FIRST-PERSON VOICE (I am Geoff). Draw
voice/phrasing/texture from the Source fragments — my journals — and use my own
ontology language (Peaking, Withdrawal, contraction, …) the way I actually do.
Not generic essay or generic "mindfulness" tropes.

SCENE: <one concrete moment; name at most one real person; invent no others>.
THE LESSON / SPINE: <the single claim I arrived at, stated plainly as mine>.
THE TEACHING or KEY IMAGE: <any quote/parable/metaphor, in my own words>.
THE ANGLES (hold them in parallel, don't collapse): <2 framings of the same thing>.
CLOSE: <the conviction or the honest unresolved question — no tidy bow>.
```

### Skill-stack mechanics (how the register/phase/frequency skills fire)

Confirmed in `drafts.py:_skill_stack`:
- **Phase skill** ← the `--phase <name>` flag (global "current phase"), NOT source
  filtering. So pass `--phase withdrawal` to activate `phases/withdrawal.SKILL.md`
  without narrowing retrieval. (`--seed-phase` *does* filter and usually empties
  the slice — avoid it.) Valid: rising peaking withdrawal diminishing
  bottoming_out restoration.
- **Frequency / mode / register skills** ← inferred automatically from the matched
  fragments (union of primary frequencies; dominant mode-orientation; dominant
  `voice_register`). You can't pass register directly — you steer it by which
  fragments the topic keyword pulls. A confessional corpus → `confessional` register.
- **No source cap on the topic path**: every substring match is included. Pick a
  keyword with a *moderate, on-theme* hit count (~20-50). `unworthy`→21 and
  `equanimity`→43 worked; `Withdrawal`→168 or `receptivity`→225 would bloat the
  prompt and dilute focus. Check first: `grep -rli "<kw>" <VAULT>/01-Fragments | wc -l`.

Full worked example (Withdrawal-phase contemplative essay):
```bash
uv run creek draft --vault <VAULT> \
  --seed-topic "equanimity" --phase withdrawal \
  --voice-core /tmp/voice-core.txt --max-tokens 3500
# → 43-fragment chain, all frequencies + withdrawal phase + confessional register,
#   voice_distance 0.0. Title == the keyword; the LLM writes its own H1 in the body.
```

## Writing an essay in the user's voice

This is the headline use case. Two facts drive the whole recipe:

1. **Build the voice fingerprint once per vault** (idempotent):
   ```bash
   uv run creek report --type fingerprint --vault <VAULT>
   ```
   Writes `00-Creek-Meta/voice-fingerprint.json` from self-authored fragments.
   `draft` loads it to inject voice targets and run the post-compose
   voice-fidelity guard (aim for low `voice_distance`).

2. **`--seed-topic` is a case-insensitive SUBSTRING gate on retrieval, and it
   is also the essay title.** It must appear *verbatim and contiguous* in some
   fragment's title or body or you get "No source material matches topic".
   Adding `--seed-frequency/--seed-phase/--seed-mode` does **not** relax this —
   the topic substring is still required *within* each dimension slice. And most
   fragments have an unclassified `phase`, so `--seed-phase` usually yields an
   empty slice. Net: a natural-language sentence as `--seed-topic` essentially
   never matches.

### The native pattern: essay about a NEW subject/anecdote

When the user wants an essay about something **not already in the vault** (a
dinner, a conversation, a fresh idea), do **not** fight `--seed-topic`. Capture
it as a note, ingest it, then draft a follow-up keyed by fragment ID
(`--seed-fragment` resolves by ID with no topic gate):

```bash
# 1. Write the raw moment as a markdown note in the user's own first-person voice
#    (this is the seed material). Save to e.g. /tmp/creek-inbox/<slug>.md

# 2. Ingest it (idempotent; deterministic fragment ID)
uv run creek ingest --type markdown --input /tmp/creek-inbox/<slug>.md --vault <VAULT> -y

# 3. Find the new fragment ID
grep -rl "<distinctive phrase>" <VAULT>/01-Fragments | head -1   # then read its `id:`

# 4. Draft the essay in voice, grounded in that fragment (+ any resonance)
uv run creek draft --vault <VAULT> --seed-fragment <frag-id> --max-tokens 3500
```

Output lands in `<VAULT>/07-Voice/Drafts/<date>-<slug>.md` with full provenance
(prompt, source fragments, `voice_distance`, voice findings) in frontmatter.
Classifying the new fragment first (`creek classify --method rules`) adds
frequency resonance but is optional — `draft` works on an unclassified seed and
the captured note alone is enough source material.

### Essay from material already in the vault

Use a **single salient keyword** that really occurs in fragments as the topic
(check first: `grep -rli "worth" <VAULT>/01-Fragments | wc -l`). Optionally
narrow by a *populated* frequency (F-codes or APTITUDE labels like `self-love`):

```bash
uv run creek draft --vault <VAULT> --seed-topic "worth" --seed-frequency self-love --max-tokens 3500
```

Other `draft` options worth knowing: `--seed-outline-text "## A\n## B"`
(multi-section, each header composed independently — headers are topic
substrings too), `--ontology-twist` (≥2 sources; forbids paraphrase),
`--include-tier open|personal|intimate|all` (privacy override, audited),
`--no-llm` (sanitize+measure only, skip rewrite), `--voice-core <file>`
(prepend a prose voice description), `--bypass-compiled` (escape hatch).

## Other recipes

```bash
# Full pipeline on new sources
uv run creek process --vault <VAULT>

# Classify leftover fragments locally (offline, no consent needed)
uv run creek classify --method rules --vault <VAULT>

# Mine blog/essay ideas, then draft the Nth one
uv run creek mine --vault <VAULT>
uv run creek draft --vault <VAULT> --index 0 --max-tokens 3500

# Weekly state report / vault hygiene
uv run creek state --vault <VAULT>
uv run creek lint --vault <VAULT>
```

## Failure → cause cheat-sheet

| Message | Cause | Fix |
|---|---|---|
| `Failed to spawn: creek … No such file` | wrong cwd | `cd …/creek-tools` first |
| `requires explicit consent … CREEK_ANTHROPIC_CONSENT=1` | LLM step, no consent | export the var |
| `config … not found; running with built-in defaults` | `CREEK_CONFIG` unset | point it at the vault config |
| `No source material matches topic '…'` | topic isn't a verbatim substring | use a single real keyword, or the `--seed-fragment` pattern |
| `No source material in any attempted dimension` | dimension/topic slice empty (often unclassified phase) | drop `--seed-phase`; use a populated frequency or `--seed-fragment` |
| draft errors on missing fingerprint behaviour | fingerprint never built | `creek report --type fingerprint` |
