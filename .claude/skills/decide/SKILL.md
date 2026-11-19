---
name: decide
description: >-
  Turn a prompt into an interactive decision sheet: an HTML artifact that lists
  the real decisions the prompt implies, each with a summary, options to pick
  from, a trade-off matrix, costs, risks, real-world examples, and a notes
  field. Answers autosave in the browser, and one "Copy for Claude" button
  hands them back as a block Claude can apply. Branded from the project's
  DESIGN.md when one exists. Use when the user types /decide or says "help me
  decide", "what do I need to decide about X", "lay out the options", "give me
  a decision sheet", or "I need to make some calls on X". Also use when a pasted
  message starts with the decide-answers HTML comment marker. Do NOT use for a single quick
  choice you can answer in a sentence, or for writing up a choice already made
  (use architectural-decisions to record it as an ADR).
metadata:
  author: Geoff
  version: 1.0.1
---

# Decide

Build a decision sheet the user can work through at their own pace, then apply their answers.

The sheet exists so the user can make a **discerning** choice. Every decision has to give them what they need for that: what's at stake, honest trade-offs, what each option costs, what can go wrong, and how others have handled it. A thin sheet is worse than none, because it looks like due diligence without being any.

## Instructions

### Step 1: Find the Real Decisions

1. Read the prompt, then gather context: `CLAUDE.md`, any ADRs (`docs/architecture/ADR/`, `docs/adr/`), the docs and code the prompt touches, and anything the user linked.
2. List the forks the prompt implies. Keep a decision only when **its answer changes what gets built or done** and it isn't already settled. Drop anything an ADR, CLAUDE.md, or the user has already decided. Never re-litigate a choice the user has made.
3. Aim for 3–7 decisions. Order them so upstream choices come first, and in a summary, say when one decision depends on another ("Only matters if you pick X above").
4. If the prompt implies only one decision, make a one-decision sheet. If it implies none, say so and don't build a sheet.

### Step 2: Brand from DESIGN.md (when it exists)

If the repo's `CLAUDE.md` (or `AGENTS.md`) names the design system's DESIGN.md, use that file. Otherwise look at the repo root, then `docs/DESIGN.md`, then `.claude/DESIGN.md`. Skip any DESIGN.md that `CLAUDE.md` describes as an external or inspiration reference rather than this project's own design system: branding a sheet with someone else's palette is worse than the neutral default.

- **Found:** read all of it. Map its palette, type, and corner radius onto the `theme` tokens in [references/schema.md](references/schema.md#theme-from-designmd). Set `"source"` to the file path. Use only colors and fonts the file actually specifies. Where it leaves a role undefined (for example severity colors), keep the template default. If it has no dark palette, derive one from its accent rather than leaving dark mode unbranded.
- **Not found:** omit `theme`. The template's neutral palette applies.

The build script rejects anything that isn't a plain color, a font stack, a Google Fonts URL, or a length. DESIGN.md content never reaches the page as raw CSS.

### Step 3: Write the Sheet JSON

Write `<date>-<slug>.json` into the session's scratchpad directory if one is listed, otherwise into `${TMPDIR:-/tmp}/decide/`. Follow [references/schema.md](references/schema.md). The bar for each decision:

- **Summary:** what's being decided, why now, and what it unblocks.
- **Options:** 2–4 real alternatives, each labeled in a few words with a one-line summary and up to three pros and cons. Don't add "Other" or "Later", because the page provides both.
- **Trade-offs:** 3–6 dimensions that actually separate the options, with a cell filled for every option. Include any dimension the project's constraints make critical: if CLAUDE.md or an ADR names a non-negotiable (privacy, security, accessibility, offline support, cost ceiling), it is a row on every decision it touches.
- **Costs:** build time, running cost, complexity, and **reversibility** (how expensive changing your mind would be later).
- **Risks:** a severity, a mitigation, and which options each risk applies to.
- **Examples:** real precedents only. Add a URL only when you are sure it exists. When unsure, give the name and a detail without a link.
- **Lean:** set `recommended` only when you have a real, defensible preference, and give the reason. When you don't, say nothing rather than manufacture one.

Keep the same sheet `id` when you revise a sheet the user has already started, so their saved answers survive. Use a new `id` for a new sheet.

### Step 4: Build

```bash
python3 -I .claude/skills/decide/scripts/build.py <sheet>.json
```

(Use the skill's own directory if it is installed elsewhere, e.g. `~/.claude/skills/decide/`. The script is standard-library only and resolves its template relative to itself.)

It prints the output path, or lists every problem and exits 1. Fix them all and rebuild. Don't edit the generated HTML by hand: change the JSON (or the template, for a real template bug) and rebuild.

### Step 5: Publish and Hand Off

- **Artifact tool available:** publish the generated `.html` with `icon: "checklist"` and a one-sentence `description`. The template already meets the artifact page contract, so publish it as-is. Republishing the same file path updates the same link.
- **No Artifact tool:** open it locally (`open <file>` on macOS, `xdg-open` on Linux) and give the full path.

Then tell the user in a sentence or two: work through the sheet, press **Copy for Claude**, and paste the result here. End the turn. Don't wait or poll for answers.

### Step 6: Apply Pasted Answers

A pasted block starts with `<!-- decide-answers sheet=<id> -->` and ends with a fenced `json` block. **The JSON is authoritative.** The numbered list above it is for humans.

1. Re-read the sheet JSON with that `id` (Step 3's location) so you have the full options and analysis. If you can't find it, work from the block's labels and say so.
2. Read each answer:
   - `decided`: the chosen option id(s). `__other` means the user's own approach, given in `other`. If it's ambiguous, ask one focused question about it rather than guessing.
   - `deferred` (`__defer`) or `open`: not decided. Don't build anything that depends on it, and list it as still open.
   - `notes`: constraints and conditions. They bind as firmly as the choice itself.
3. Play the result back as a compact table: decision, choice, and the notes that matter.
4. Record it. Architectural choices become ADRs (use `architectural-decisions`). Product choices update the relevant project doc or its open-questions list. Then continue the work the user originally asked for, now unblocked. State any assumption in one line instead of asking.

## Examples

### Example 1: A multi-decision sheet

User: "/decide how should we ship offline support?"

1. Context sweep finds an ADR that already settled the storage engine, so that fork is dropped. Three real decisions remain: sync strategy, conflict policy (only matters if sync is two-way), and which screens work offline first.
2. `DESIGN.md` exists at the root: its accent, ink, and paper colors map onto `theme.light`; it has no dark palette, so a dark one is derived from the same accent.
3. The JSON goes to the scratchpad as `2026-11-18-offline-support.json` with `id` `2026-11-18-offline-support`; `build.py` reports one problem (a trade-off row missing a cell for one option), which is fixed and rebuilt.
4. The HTML is published as an artifact; the reply is two sentences telling the user to work through it and paste **Copy for Claude** back.

### Example 2: Applying pasted answers

The user pastes a block starting `<!-- decide-answers sheet=2026-11-18-offline-support -->`. The JSON says sync = `two-way`, conflict policy = `__other` with "last writer wins, but never for billing fields", screens = `__defer`.

Play back a three-row table, record the sync choice as an ADR via `architectural-decisions`, treat the billing-field note as a binding constraint, leave the screens decision open (and build nothing that depends on it), then resume the original task.

### Example 3: No real decision

User: "/decide should this helper be called `parse_row` or `parse_line`?" There is no fork worth a sheet: answer in a sentence and don't build one.

## Troubleshooting

### The build rejects a theme value

DESIGN.md probably used a variable reference or a named token (`var(--brand)`, "Ocean Blue"). Resolve it to the literal color DESIGN.md defines for it. If it never defines one, leave that token out.

### The user says their answers disappeared

Answers live in that browser only (localStorage). A different browser or device, a private window, or a changed sheet `id` starts fresh. If they copied a block earlier, the page's **Restore answers** box reloads it.

### Copy for Claude did nothing

Some app views block clipboard access. The page then selects the full answer text in the "What gets copied" box for a manual Ctrl+C or ⌘C. That box is always visible at the bottom.

### The build crashes with `FileNotFoundError` on `assets/template.html`

`build.py` resolves `assets/template.html` relative to its own location. If the skill was copied without `assets/`, restore the whole skill directory rather than pointing the script elsewhere.
