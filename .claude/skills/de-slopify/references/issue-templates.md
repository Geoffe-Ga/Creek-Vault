# Issue & Epic Templates — Filing De-Slop Work the Backlog Can Consume

Findings become GitHub issues that **Ralph** picks up autonomously
(`scripts/ralph/pick-next.sh`). To be picked up correctly, issues must follow
the label and shape conventions below. Get this wrong and Ralph either ignores
real work or tries to implement an epic as a single PR.

---

## How the Ralph picker treats labels (must-know)

From `scripts/ralph/pick-next.sh`:

- **Picks** the lowest-numbered open issue with **none** of these labels:
  `epic wontfix duplicate invalid question blocked needs-spec future-work
  do-not-auto-merge in-progress`.
- An issue already referenced by an open PR's `Closes/Fixes/Resolves #N` is
  treated as in-flight and skipped.

**Consequences for de-slop filing:**

- A **standalone finding** = a normal issue with **no** excluded label → Ralph
  works it. ✅
- An **epic** must carry the `epic` label → Ralph skips the umbrella and works
  its decomposed sub-issues instead. ✅
- A sub-issue that depends on an unbuilt predecessor should carry `blocked` (or
  `needs-spec`) until its predecessor merges, so Ralph doesn't start it out of
  order. The 10-tick `backlog-grooming` pass (or you, next run) removes the
  label once the predecessor is done. Note the dependency as `Depends on #N` in
  the body regardless.

---

## Standard labels this skill uses

Create any that don't exist (`gh label create <name> --color <hex> --description ...`).

| Label | Meaning | Color |
|-------|---------|-------|
| `de-slop` | Filed by the de-slopify detector | `#5319e7` |
| `epic` | Umbrella issue; Ralph skips, works sub-issues | `#3e4b9e` |
| `priority-critical` / `priority-high` / `priority-medium` / `priority-low` | Severity from the rubric | red→grey |
| `creek-pipeline` / `creek-mcp` / `crawdad-bot` | Scope (matches `.github/deslop-areas.json` area ids) | tool-matched |
| `bug` | Family 0 / 12 correctness finding (has a reproducing test) | `#d73a4a` |
| `dead-code` | Family 3 dispensable | `#cfd3d7` |
| `wire-in` | Family 3 finding whose remedy is connect-it + e2e test (not delete) | `#0052cc` |
| `refactor` | Bloaters / couplers / verbosity | `#fbca04` |
| `tech-debt` | Architecture / change-preventers | `#e99695` |
| `security` | Family 10 (work itself defers to the `security` skill) | `#b60205` |
| `tests` | Family 9 testing slop | `#0e8a16` |
| `blocked` / `needs-spec` | Not ready for autonomous pickup | `#000000` |

Keep labels minimal per issue: one severity + one scope + one category + `de-slop`.
`wire-in` is **not** in `pick-next.sh`'s exclude list, so Ralph still picks
these issues up like any other standalone finding.

---

## Template A — Standalone finding (single, atomic, Ralph-ready)

Mirror the established `prompts/github-issues/*.md` shape so it's actionable
without a single clarifying question. Title is imperative and specific. For a
wire-in finding, title it as a connect-it task, e.g. "Wire in orphaned
`export_data()` from the creek CLI and cover it with an e2e test" — not as a
removal.

```markdown
# <imperative title> (e.g. "Remove dead `legacy_resonance()` and its orphaned test")

**Labels:** `de-slop`, `<scope>`, `<category>`, `priority-<level>`
**Detected by:** de-slopify scan (<area id or "full audit">) <YYYY-MM-DD>
**Severity:** <Critical|High|Medium|Low>
**Remediation:** <delete | wire-in (+ e2e test) | decision-needed>

## Problem
<2-3 sentences. What's wrong, where. Cite file:line. State the taxonomy family.>
**Current state:** <concrete observation>

## Evidence (corroboration)
- Signal 1: <reproducing artifact — test output / mypy error / query count / tool JSON>
- Signal 2: <independent signal — grep result / second tool / reading>
<Paste the minimal proof. This is what makes the finding trustworthy.>

## Scope
<1-2 sentences bounding what this covers and explicitly what it does NOT.>

## Tasks
1. <specific, actionable step with file paths>
2. <...>

## Acceptance Criteria
- [ ] <testable, binary criterion tied to the evidence above>
- [ ] No existing tests break; coverage stays ≥ 90%.
- [ ] All pre-commit hooks pass (`pre-commit run --all-files`).

## Files to Create/Modify
| File | Action |
|------|--------|
| `path/to/file.py` | Modify / Delete / Create |
```

For a wire-in issue, the Files table's Action column is Create/Modify — the
wired call site plus the new e2e test file — not Delete.

**Rule:** a standalone issue should be ~50–300 net LoC and touch ≤ ~5 files. If
it's bigger, it's an epic.

---

## Template B — Epic (umbrella for coordinated, multi-issue work)

Use when a corroborated problem needs more than one PR's worth of change
(e.g. "decompose the 600-line crawdad `router.py`", "unify three divergent
error contracts", "destub the aspirational encryption feature"). Mirrors the
existing `prompts/github-issues/phase-*-epic.md` and `audit-destub` shape.

```markdown
# <Epic title> (e.g. "De-Slop: Decompose the crawdad router god-module")

**Labels:** `de-slop`, `epic`, `<scope>`, `priority-<level>`
**Detected by:** de-slopify scan (<area id or "full audit">) <YYYY-MM-DD>

## Why this is an epic
<The corroborated problem and why it can't be one atomic PR. Cite evidence.>

## Evidence (corroboration)
<The tool output / metrics / reading that proves the problem is real.>

## Target end state
<What "done" looks like across all sub-issues — the architecture after.>

## Decomposition (sub-issues, in dependency order)
1. #<n> — <sub-issue title> — ~<LoC> — depends on: none
2. #<n> — <sub-issue title> — ~<LoC> — depends on: #<prev>
...

## Dependency graph
```
sub-1 ──▶ sub-2 ──▶ sub-4
   └────▶ sub-3 ────┘
```

## Non-goals
<What this epic explicitly does not attempt.>
```

Then create each sub-issue with **Template A**, link them to the epic body, and
(if GitHub sub-issues are available) attach them via `gh sub-issue` / the REST
sub-issues API. Mark dependents `blocked` per the picker rule above.

> The `triage-and-plan` skill is the heavy machinery for decomposing a large
> area into a well-ordered epic of ~300-LoC issues. **Invoke it** when an epic
> needs more than ~3 sub-issues — don't hand-roll a sprawling decomposition.

---

## `gh` filing recipes (file-based bodies, never inline strings)

Write the body to a temp file in the scratchpad, then file it. This avoids shell
quoting bugs and matches the repo's `git-workflow` convention.

```bash
SCRATCH="${SCRATCHPAD:-/tmp}"; BODY="$SCRATCH/deslop-issue.md"

# Standalone finding
cat > "$BODY" <<'EOF'
## Problem
...
EOF
gh issue create \
  --title "Remove dead legacy_resonance() and its orphaned test" \
  --label de-slop --label creek-pipeline --label dead-code --label priority-medium \
  --body-file "$BODY"

# Epic (capture its number to link sub-issues)
EPIC_URL=$(gh issue create --title "De-Slop: Decompose the crawdad router god-module" \
  --label de-slop --label epic --label crawdad-bot --label priority-high \
  --body-file "$SCRATCH/deslop-epic.md")
EPIC_NUM="${EPIC_URL##*/}"

# Sub-issue referencing the epic
gh issue create --title "Extract message parsing from crawdad router.py" \
  --label de-slop --label crawdad-bot --label refactor --label priority-high \
  --body-file "$SCRATCH/deslop-sub-1.md"   # body contains "Refs #$EPIC_NUM"
```

Dedup before every create:

```bash
gh issue list --state open --search "in:title <keywords>" --json number,title
gh search issues --repo geoffe-ga/creek-vault "<keywords>" --state open
```

---

## Filing checklist (every issue, before `gh issue create`)

- [ ] Corroborated by ≥2 signals; evidence pasted into the body.
- [ ] Not a duplicate of an open or recently-closed issue.
- [ ] Not in the "NOT slop" guard list.
- [ ] Correct labels: severity + scope + category + `de-slop` (+ `epic` if umbrella).
- [ ] Title is imperative and specific; body is actionable without questions.
- [ ] Sized right (standalone ≤ ~300 LoC / ≤ 5 files; else epic + sub-issues).
- [ ] Dependencies noted (`Depends on #N`) and dependents labeled `blocked`.
- [ ] Acceptance criteria are testable and tie back to the evidence.
