# Detection Playbook — Evidence, Corroboration, and the No-False-Positive Gate

The taxonomy (`slop-taxonomy.md`) says *what* to look for. This playbook says
*how to find it with evidence* and — critically — *how to prove a finding is
real before it becomes work*. The entire value of this skill collapses if it
files plausible-but-wrong findings, because Ralph will autonomously implement
them. **A false positive is worse than a missed finding.**

---

## The Two-Signal Rule (the core discipline)

> **No finding is filed unless it is corroborated by at least two independent
> signals, at least one of which is concrete and reproducible.**

The signal classes, strongest first:

1. **Reproducing artifact** — a failing test, a stack trace, a query-count log,
   a `mypy` error, an import that doesn't resolve. *Strongest.* For any
   **correctness/bug** claim (Family 0, Family 12 "confident wrongness"), a
   reproducing artifact is **mandatory** — never file a bug on reasoning alone.
2. **Static-analysis hit** — a tool flags the exact line(s): ruff, mypy,
   vulture, radon/xenon, bandit, interrogate, detect-secrets, pip-audit, an
   import-cycle scan, a duplication scan.
3. **Structural proof** — a grep/graph result showing the claim: zero inbound
   references (dead code), N identical token-runs across files (duplication),
   a recurring field triple (data clump), a flag whose claim has no
   implementing code path.
4. **Reviewer reading** — an agent reads the code and explains the defect in
   terms of intent. *Never sufficient alone* — it must confirm a 1–3 signal,
   and for bugs it must be backed by signal #1.

**Examples of valid corroboration:**

- *Dead function:* vulture flags it (signal 2) **and** grep finds zero callers
  including dynamic/string/route references (signal 3). ✅
- *God function:* radon grades it `D` (signal 2) **and** a reading shows ≥3
  distinct responsibilities (signal 4). ✅
- *Off-by-one:* a written test fails at the boundary (signal 1) **and** the
  reading explains why (signal 4). ✅
- *Duplicated block:* duplication scan finds it (signal 2/3) **and** the diff
  shows the blocks are semantically identical (signal 4). ✅

**Examples that DO NOT clear the gate (drop them):**

- "This function looks too long." — one soft signal, no tool, no responsibility
  analysis. ❌
- "This is probably an N+1." — no query-count artifact. ❌
- "This might be a race condition." — no reproducing test or rigorous argument
  plus a concrete interleaving. ❌
- vulture flags a symbol that is actually a Typer CLI command / FastMCP tool
  handler / discord.py listener / pytest fixture / dynamically dispatched —
  the grep refutes it. ❌ (This is why vulture alone is never enough.)

When in doubt, **don't file it.** Null findings are a valid, healthy outcome.

---

## The toolbox (already in this repo)

Everything below is provisioned by `cd creek-tools && uv sync --all-extras`
(see `creek-tools/pyproject.toml` and `creek-tools/.pre-commit-config.yaml`).
`collect-evidence.sh` runs the read-only subset over all three Python source
roots (`creek-tools/creek`, `creek-tools/creek_mcp`, `crawdad/crawdad`) and
writes a report to the scratchpad. There is no frontend in this monorepo.

### Python (creek-tools + crawdad)

Invocations run from the repo root; `$ROOTS` below stands for
`creek-tools/creek creek-tools/creek_mcp crawdad/crawdad`, and the shared tool
config is `creek-tools/pyproject.toml` (use `uv run <tool>` from `creek-tools/`
when running outside the collector script).

| Tool | Finds | Invocation |
|------|-------|------------|
| **ruff** | bugs (`B`,`BLE`,`RUF`), simplifications (`SIM`), complexity (`PLR`), datetimes (`DTZ`), dead imports (`F401`) | `ruff check $ROOTS --config=creek-tools/pyproject.toml --output-format=json` |
| **vulture** | dead code: unused funcs/classes/vars/imports | `vulture $ROOTS --min-confidence 60` (80 hides the whole unused-function tier, which vulture scores at 60 — issue #1395; the gated policy is `creek-tools/scripts/lint-vulture.sh`) |
| **radon** | cyclomatic complexity + maintainability index | `radon cc $ROOTS -s -n C` / `radon mi $ROOTS -s` |
| **xenon** | complexity grade gate | `xenon $ROOTS --max-absolute B --max-average A` |
| **mypy** (strict) | type holes, `Any` creep, unreachable code | `mypy $ROOTS --config-file=creek-tools/pyproject.toml` |
| **bandit** | security smells (injection, weak crypto, asserts) | `bandit -r $ROOTS -f json -c creek-tools/pyproject.toml` |
| **interrogate** | docstring coverage gaps | `interrogate $ROOTS -v` |
| **pip-audit** | vulnerable dependencies | `pip-audit -r creek-tools/requirements.txt` |
| **detect-secrets** | hardcoded secrets | `detect-secrets scan $ROOTS` |
| **git** | churn / hotspots / commented-out code | `git log --format= --name-only \| sort \| uniq -c \| sort -rn` |

### Cross-cutting grep recipes

Run these read-only; each hit is a *candidate* needing a second signal.
`$ROOTS` is `creek-tools/creek creek-tools/creek_mcp crawdad/crawdad`.

```bash
# Stubs / hollow implementations
rg -n "NotImplementedError|not implemented|return None\s*#\s*TODO" $ROOTS
# AI-tell comments
rg -n "In a real implementation|placeholder|for now|as an AI|should probably|FIXME|HACK|XXX" $ROOTS
# Debt markers
rg -n "TODO|FIXME|HACK" $ROOTS
# Escape hatches without an issue reference
rg -n "type: ignore|# noqa" $ROOTS
# Swallowed exceptions
rg -n "except (Exception|BaseException)?:\s*$" -U $ROOTS
# Commented-out code (heuristic: lines that are commented statements)
rg -n "^\s*#\s*(def |class |return |if |for |import )" $ROOTS
# Magic numbers (review, don't auto-file)
rg -n "[^_a-zA-Z0-9.]\d{3,}" $ROOTS
```

---

## Category → detection → corroboration map

For each taxonomy family, the primary tool and the required second signal.

| Family | Primary signal | Required second signal |
|--------|----------------|------------------------|
| 0. Correctness/bugs | ruff `B`/`BLE`/`RUF`, reading | **a failing test that reproduces it** (mandatory) |
| 1. Bloaters | radon CC | reading confirms multiple responsibilities |
| 2. OO abusers | reading / grep `isinstance` ladders | confirm a cleaner construct fits |
| 3. Dispensables — dead | vulture | grep proves zero inbound refs (incl. dynamic) |
| 3. Dispensables — dup | duplication scan (`jscpd`-style) | diff confirms semantic identity, ≥2 sites |
| 3. Dispensables — stub | grep stub markers | confirm callers depend on it (else it's dead) |
| 4. Change-preventers | git churn / shotgun trace | confirm scatter across files for one change |
| 5. Couplers/arch | import-cycle scan / layering grep | confirm the edge crosses a layer wrongly |
| 6. Naming/comments | comment-density / grep | reading confirms zero added information |
| 7. Verbosity | ruff `SIM`, reading | confirm an idiomatic shorter form exists |
| 8. Type safety | mypy / grep escape-hatches | confirm a real type is knowable / no issue ref |
| 9. Testing | pytest coverage + mutation reasoning | confirm a logic flip would survive |
| 10. Security | bandit / detect-secrets / pip-audit | confirm input reaches sink / secret is live |
| 11. Deps/config | grep imports vs manifest | confirm zero use / real conflict |
| 12. AI-slop | grep AI-tells / dup vs existing helper | reproducing artifact (bugs) or grep of the real helper |

---

## What is already enforced — never file it

The repo runs ruff, mypy (strict), radon/xenon, bandit, and interrogate in
pre-commit **and** CI. Anything those gate cannot
reach `main`, so it is **not a finding**:

- complexity grades (radon CC/MI, xenon) — already gated,
- ruff lint rules and formatting,
- mypy type errors,
- interrogate docstring-coverage gaps,
- bandit's high-confidence security rules.

If the only signal for a candidate is one of these tools agreeing with itself,
**drop it.** Those tools are the *map* (where to look), not the *findings*. The
findings come from reading the code for what the tools are blind to. A run whose
"findings" are all linter-shaped is a failed run — that was the failure mode of
the first audit.

## The weekly run procedure

1. **Snapshot.** Run `scripts/collect-evidence.sh` → a consolidated evidence
   report in the scratchpad (all tool JSON + grep hits + churn + reading
   targets). Treat the linter JSON as a map, per the rule above.
2. **Triage candidates.** Parse the report into candidate findings. Discard
   anything in the "NOT slop" guard list (`slop-taxonomy.md`) — generated code,
   migrations, justified suppressions, framework boilerplate, test fixtures —
   and anything already enforced by the gates above.
3. **Reading pass (fan-out) — the core of the audit. EXHAUSTIVE, WHOLE-CODEBASE,
   EVERY RUN.** The bundle's `area-inventory.txt` is the authoritative coverage
   set. Spawn one `Task` subagent for **every** area in it — every creek
   pipeline stage (`creek-tools/creek/{ingest,classify,link,author,...}/*`),
   the MCP server surface (`creek-tools/creek_mcp/*`), the crawdad Discord bot
   (`crawdad/crawdad/*`, incl. `llm/` and `builtin_workflows/`), the
   shared/util/config grab-bags, the top-level modules, and the `scripts/`
   shell tooling — never just the changed ones. Hand each the full taxonomy and have
   it **read the actual source** and return corroborated candidates for the
   linter-invisible families: dead/stubbed/orphaned code, duplication (local and
   repo-wide), architecture/layering violations, lying flags, verbosity, comment
   slop, AI-slop tells, weak tests. **`churn.txt` / `reading-targets.txt` set the
   ORDER only, never which areas to skip; a clean linter bundle or an unchanged
   file is no reason to skip an area; "delta-focused" / "since last run" /
   "building on last week's baseline" scoping is FORBIDDEN.** Do not skip this
   for a single-threaded skim or a delta scan — both produce the false "clean".
4. **Corroborate each survivor** against the Two-Signal Rule. For correctness
   candidates, *write and run the reproducing test* in a throwaway location
   (do not commit it — the implementing issue will own the real test). If it
   doesn't reproduce, drop it.
5. **Cluster.** Group corroborated findings by area/theme. A cluster that needs
   coordinated, multi-file change becomes an **epic**; standalone findings
   become single issues. Many Low findings in one file = one tidy-up issue.
6. **Dedup against the backlog.** For every cluster/finding, search existing
   open issues (`gh issue list`, `gh search issues`) and recent closed ones.
   If it already exists, skip (optionally add a corroborating comment). Never
   file a duplicate.
7. **Size & file.** Apply `issue-templates.md`. Respect the ~200–300 LoC
   sizing; split anything bigger into an epic + sub-issues in dependency order.
8. **Report with a coverage ledger.** Emit a run summary (counts by severity,
   what was filed, dropped and why, deduped) **plus a coverage ledger that proves
   WHOLE-CODEBASE coverage two ways: (a) a 13-row table — one per taxonomy family
   — naming the areas examined and the verdict, and (b) every area in
   `area-inventory.txt` marked READ this run (no area "unchanged → not read").**
   A clean verdict must read *"entire codebase read this run"*, never *"delta
   since #N"*. A clean verdict with no ledger, or a ledger that doesn't cover the
   full inventory, means the reading pass was skipped or narrowed to changed
   areas — that is a failed run, not a clean one.

---

## Calibration: precision over recall

This detector is tuned for **precision**. It is acceptable — expected, even —
to miss real slop in a given week. It is **not** acceptable to file a finding
that wastes an autonomous implementation cycle on a non-problem or, worse,
"fixes" correct code into broken code.

- If a finding's corroboration is shaky, **downgrade or drop it**.
- If a "fix" would be opinionated/stylistic against repo convention, **drop it**.
- If the evidence is a single tool hit with a known false-positive profile
  (vulture on dynamic dispatch, magic-number grep on HTTP codes), **drop it**
  unless a second signal confirms.
- Prefer **few, ironclad, high-value** issues over a long list of maybes.
