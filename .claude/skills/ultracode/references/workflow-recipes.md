# Workflow recipes

The `Workflow` tool is the "think" half of ultracode. This skill's instructions
are themselves the explicit opt-in that authorises calling it.

Load the `workflow-authoring` skill before writing a script. What follows is the
shape these three phases take, plus the one gate that most often kills a run.

---

## THE GATE: `${VAR}` interpolation kills a run at 0 agents

Workflow scripts are **JavaScript**. Any `${VAR}` inside a backtick template
literal is JS interpolation, not text. Pasting a shell line, a CI snippet, or a
verbatim defect quote into an agent prompt therefore crashes the entire run:

```
echo "::warning::comment on PR #${PR} was not admitted"
```
→ `ReferenceError: PR is not defined` — **0 agents, 25 ms, nothing cached to
resume from.** The stack trace points at the generated `workflow.js`, so it
reads like a harness bug.

This bites precisely because quoting a defect *verbatim* is the right way to
brief an agent — the more faithful the quote, the likelier it carries a `${...}`.

Escape every literal one as `\${PR}`. `$(cmd)`, bare `$VAR` and `$?` are safe;
only `${...}` bites. **Sweep before launching** — `node --check` does NOT catch
this, because `${PR}` is valid syntax and fails only at runtime:

```bash
python3 - "$SCRIPT" <<'PY'
import sys, re
s = open(sys.argv[1]).read()
for m in re.finditer(r'(?<!\\)\$\{(\w+)\}', s):
    print("interpolates:", m.group(0))
PY
```

Confirm every survivor is one you *meant* (a real prior-phase result). Recovery
is cheap: edit the persisted script the launch result names and re-invoke with
`{scriptPath, resumeFromRunId}` — with 0 agents done there is nothing to replay.

---

## Phase A — Premise + design audit (one run per wave)

Fan out across the whole tranche. Structure per issue:

```
premise-verify(issue)  →  design-panel(issue, premise)  →  judge(designs)  →  dossier
```

Use `pipeline()` so each issue's design phase starts the moment its premise
phase lands, instead of waiting for the slowest premise check.

**Premise phase schema** — make falsity a first-class output:

```js
const PREMISE_SCHEMA = {
  type: 'object',
  properties: {
    head_sha: { type: 'string' },
    verdict: { enum: ['confirmed', 'partly', 'refuted', 'already-fixed'] },
    premises: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          claim:    { type: 'string' },
          holds:    { type: 'boolean' },
          location: { type: 'string' },   // file:line RE-DERIVED at HEAD
          evidence: { type: 'string' },
        },
        required: ['claim', 'holds', 'evidence'],
      },
    },
    corrected_scope:      { type: 'string' },
    acceptance_criteria:  { type: 'array', items: { type: 'string' } },
    blast_radius:         { type: 'array', items: { type: 'string' } },
    red_test:             { type: 'string' },
  },
  required: ['head_sha', 'verdict', 'premises', 'acceptance_criteria'],
};
```

Put this sentence in the prompt, verbatim:

> A premise of this issue being false is a **finding to report**, not a problem
> to work around. Line numbers in the issue body are routinely 100–300 lines
> stale — re-derive every `file:line` at HEAD and never quote the body's
> numbers onward. If the issue asks you to reverse a decision recorded in an ADR
> or a CLAUDE.md, say so instead of doing it.

**Design panel:** two or more agents under *opposing* lenses — `minimal-diff`
vs. `structural` — then a judge that must **refute both** before picking. A
judge that only ranks is a rubber stamp; require the refutation text.

**Read `verdict` and `holds:false` entries before trusting any downstream
phase.** `already-fixed` → close the issue with the evidence, do not open a
lane. `refuted` → correct the issue body and re-triage.

---

## Phase C — Adversarial review (per wave's PRs)

Two stages, never one:

```
review(dimension)  →  verify(each finding, by an independent skeptic)
```

Dimensions: correctness, security, test-quality, performance, docs — include
only those the change actually touches; padding is waste, not thoroughness.

**Every finding must survive an independent skeptic before it counts.** A
skeptic's job is to construct the concrete failing input, or to demonstrate the
finding is wrong. Findings that survive get a `CONFIRMED` verdict and a failure
scenario; the rest are dropped, not softened into nits.

Aim the test-quality dimension at what author self-review structurally cannot
catch:

- tests that pass against **unfixed** code (mutation-test them);
- a `parametrize` list emptied to nothing — the tests vanish behind a green gate;
- live assertions **deleted** from a test the change touched
  (`git show HEAD:<path>` and diff, do not trust the diff view alone);
- a reader/writer that self-heals bad input, making its own test vacuous;
- tests that only exercise the **non-default** path — every test passes an
  explicit flag while the bare invocation is uncovered;
- a hand-rolled fast path with only a hand-picked agreement table: demand a
  **differential fuzz** against the slow path instead.

---

## Reading the results

- **Never branch on substrings of agent prose.** Use enum fields. A rationale
  paragraph containing "wontfix" once dropped a `Closes` line.
- **Lane and review verdict fields describe the run's *start*.** A top-level
  `review: BLOCKING` or `final_gate2_green: false` often means "blocking findings
  were raised", not "they are still open". Read `findings[].fixed`.
- **Read `notes_for_operator` on every lane before merging.** That is the only
  place a lane discloses that its own Gate 2.5 degraded to self-review.

---

## Sizing

Respect the session's configured workflow size guideline, and note that it is
about **orchestration and token cost — it says nothing about memory.** An agent
that edits a file costs nothing; an agent that runs a test suite costs a gigabyte
and a core. Both budgets must fit: the agent count AND the resident set (SKILL.md
§4).
