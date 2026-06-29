# The Slop Taxonomy — An Exhaustive Field Guide to Sloppy and Amateurish Code

This is the reference catalog the `de-slopify` skill scans against. It is the
distilled answer to the question *"what does sloppy, amateurish, AI-generated,
or otherwise low-quality code actually look like?"* — organized so a reviewer
(human or agent) can systematically hunt for each species.

It synthesizes several established sources and adapts them to Adepthood's
FastAPI + React Native stack:

- **Fowler & Beck**, *Refactoring* (2nd ed., 2018) — the canonical 22 smells,
  grouped into Bloaters, OO-Abusers, Change-Preventers, Dispensables, Couplers.
- **Mäntylä & Lassenius**, *Bad Code Smells Taxonomy* — the academic expansion.
- **Robert C. Martin**, *Clean Code* — the heuristics (G1–G36, N1–N7, etc.).
- **SonarSource / SonarQube** rule families — reliability, maintainability,
  security-hotspot, cognitive-complexity.
- **AI-slop literature (2024–2026)** — the failure modes specific to
  LLM/agent-generated code (architectural drift, duplicated blocks, confident
  wrongness, ceremonial comments, hallucinated APIs).

> **How to use it.** Each entry has: a *name*, a *tell* (how it presents), and a
> *corroboration hint* (what second signal makes it real vs. a false alarm).
> Nothing here is filed as work on its own — see `detection-playbook.md` for the
> evidence-and-corroboration gate every finding must pass.

---

## Family 0 — Correctness & Latent Bugs (the highest-value class)

Slop that is not merely ugly but *wrong*. These get the highest severity because
they cause incidents, not just friction.

| Smell | The tell | Corroboration hint |
|-------|----------|--------------------|
| **Off-by-one / boundary error** | `<=` vs `<`, `range(n)` vs `range(n+1)`, slice fence-posts | Write the failing test at the boundary value; if it fails, real. |
| **Swallowed exception** | `except Exception: pass`, `catch {}`, `.catch(() => {})` with no rethrow/log | grep for bare excepts; confirm the error path is reachable and silently lost. |
| **Broad except clause** | `except Exception:` / `except:` masking specific failures | ruff `BLE001`; confirm a narrower type exists. |
| **Mutable default argument** | `def f(x=[])`, `def f(x={})` | ruff `B006`; trivially real. |
| **Resource leak** | file/socket/session opened without `with`/`finally`/`close()` | trace the handle; confirm no context manager. |
| **Async footgun** | un-awaited coroutine, blocking call in async path, `asyncio.run` in a running loop | ruff `RUF006`, `ASYNC*`; confirm with a runtime trace or test. |
| **Race / shared-state mutation** | module-level mutable state mutated per-request; non-atomic check-then-act | concurrency reasoning + a stress test; corroborate, don't guess. |
| **Floating-point / money in float** | currency or energy points stored as `float` | confirm rounding drift is observable. |
| **Timezone-naive datetime** | `datetime.now()` without tz, naïve/aware mixing | ruff `DTZ*`; confirm comparison across tz. |
| **TOCTOU / idempotency hole** | check-then-act without a lock or unique constraint; missing idempotency key TTL | reproduce the double-submit. |
| **Unvalidated boundary input** | request body trusted without schema/length/range checks | confirm no Pydantic validator / no zod-equivalent. |
| **Incorrect null/undefined handling** | `if (x)` truthiness bugs (`0`, `""`, `false`), `Optional` deref without guard | type-narrowing analysis + test. |
| **SQL/ORM N+1 or missing filter** | query in a loop; `.all()` then filter in Python; missing tenant/user filter | log query count; the missing-filter case is also a security bug. |
| **Wrong comparison** | `==` on objects/NaN, `is` on small ints/strings by luck, `assert` for control flow | confirm semantics differ from intent. |
| **Silent type coercion** | implicit `str`↔`int`, JS `==`, truthy coercion | eslint `eqeqeq`; confirm a coercion path. |

---

## Family 1 — Bloaters (code that grew too big to reason about)

| Smell | The tell | Corroboration hint |
|-------|----------|--------------------|
| **Long Method / Function** | 50+ lines, scrolls past a screen, many responsibilities | radon CC grade ≥ C **and** a reviewer reading confirms multiple jobs. |
| **Large Class / God Object** | a class/module that knows or does everything | LoC + fan-in/fan-out; confirm distinct responsibilities. |
| **Long Parameter List** | 4+ positional params, especially booleans | ruff `PLR0913`; confirm a param object is natural. |
| **Data Clumps** | the same 3+ fields travel together everywhere | grep the recurring tuple; confirm ≥3 call sites. |
| **Primitive Obsession** | strings/ints where a value object belongs (ids, money, enums-as-strings) | confirm invariants are re-checked everywhere. |
| **Long message chain / Train wreck** | `a.b().c().d().e()` | confirm Law-of-Demeter violation, not a fluent builder. |
| **Combinatorial flag explosion** | functions whose behavior forks on many boolean params | count branches; confirm callers pass varied combos. |
| **Deeply nested code** | 4+ levels of indentation, arrow-shaped code | cognitive-complexity / radon; confirm guard clauses would flatten it. |

---

## Family 2 — OO / Structure Abusers

| Smell | The tell | Corroboration hint |
|-------|----------|--------------------|
| **Switch/if-elif on type** | repeated `isinstance`/`type ==` ladders | confirm polymorphism or a dispatch table fits. |
| **Refused Bequest** | subclass ignores/overrides most of parent | confirm the inheritance is wrong, not partial. |
| **Temporary Field** | object field only set in some flows, null otherwise | trace lifecycle; confirm it's conditional state. |
| **Alternative Classes, Different Interfaces** | two classes do the same job with different method names | confirm interchangeability. |
| **Inappropriate Intimacy** | two modules reach into each other's privates | confirm tight coupling across a boundary. |
| **Feature Envy** | a method uses another object's data more than its own | confirm the method belongs elsewhere. |
| **Middle Man** | a class that only delegates | confirm it adds no value. |
| **Hub-and-spoke god module** | everything imports one util grab-bag | import graph fan-in; confirm it should be split. |

---

## Family 3 — Dispensables (code that should not exist)

This family is the heart of "de-slopping." It is where dead, stubbed, orphaned,
and duplicated code lives.

| Smell | The tell | Corroboration hint |
|-------|----------|--------------------|
| **Dead code** | unreachable branches, unused functions/classes/vars, unused imports | **vulture** (Python) / **eslint+ts** (frontend) **and** a grep proving zero references (incl. dynamic/string refs, routers, DI). |
| **Commented-out code** | blocks of `# old impl ...` / `// ...` left in | grep; trivially real — delete, git remembers. |
| **Stubbed / hollow implementation** | `raise NotImplementedError`, `pass`, `return None  # TODO`, `return []` placeholder, `throw new Error('not implemented')`, a function whose body is a single `...` | grep + confirm it's referenced as if real (a stub nobody calls is dead code; a stub callers depend on is a **lie** — higher severity). |
| **Fake/aspirational feature flag** | a flag/docstring/comment claiming a guarantee the code doesn't deliver (e.g. "encrypted at rest" with plaintext writes) | trace the claim to the implementation; confirm the gap. This is the `audit-destub` pattern — see that epic. |
| **Orphaned code** | files/modules nothing imports; routes never mounted; assets never referenced; tests for deleted features | dependency graph + grep; confirm zero inbound edges. |
| **Duplicated code** | copy-pasted blocks, parallel near-identical functions | structural duplication scan + diff the blocks; confirm ≥ N tokens identical across ≥2 sites. |
| **Speculative generality** | abstractions/params/hooks "for the future" with one caller | confirm a single implementation/caller; YAGNI. |
| **Lazy class** | a class/module that doesn't pull its weight | confirm it can be inlined. |
| **Data class (anemic)** | a bag of fields with no behavior where behavior belongs | confirm logic is scattered across users. |
| **Dead config / deps** | declared dependencies never imported; env vars never read; settings never used | grep usage of each; confirm zero references. |
| **Redundant test** | tests asserting framework behavior, tautologies, `assert True`, snapshot-only with no logic | confirm it would survive a logic mutation (see mutation-testing). |

---

## Family 4 — Change-Preventers (code that makes change expensive)

| Smell | The tell | Corroboration hint |
|-------|----------|--------------------|
| **Divergent Change** | one module changed for many unrelated reasons | git churn + confirm distinct change-reasons in history. |
| **Shotgun Surgery** | one logical change forces edits in many files | trace a representative change; confirm scatter. |
| **Parallel inheritance hierarchies** | adding a subclass here forces one there | confirm the lock-step. |
| **Implicit cross-layer coupling** | frontend type silently depends on backend shape with no shared contract | confirm a schema mismatch is possible. |

---

## Family 5 — Couplers & Architecture

| Smell | The tell | Corroboration hint |
|-------|----------|--------------------|
| **Circular dependency** | module A imports B imports A | import-graph cycle detection; trivially real once found. |
| **Layering violation** | router does DB access directly; domain imports web framework; model imports a router | confirm the dependency crosses a layer the wrong way. |
| **Business logic in the wrong tier** | validation/energy/streak math inside a route handler or a React component | confirm it belongs in `domain/`. |
| **Leaky abstraction** | callers must know internal details to use a thing safely | confirm the contract is insufficient. |
| **Global mutable singleton** | module-level caches/state mutated across requests | confirm request-to-request bleed. |
| **Missing seam / untestable design** | logic that can't be tested without network/clock/db because it hard-codes them | confirm no injection point exists. |
| **Inconsistent error contract** | endpoints return errors in different shapes/status codes | diff error responses across routers. |
| **Architectural drift (AI-era)** | the same concept implemented 3 ways because each was generated fresh | find the divergent implementations; confirm they should be one. |

---

## Family 6 — Naming, Readability & Comment Slop

| Smell | The tell | Corroboration hint |
|-------|----------|--------------------|
| **Meaningless names** | `data`, `data2`, `tmp`, `obj`, `foo`, `handleStuff`, `x1` | confirm the name doesn't reveal intent. |
| **Misleading names** | name says one thing, code does another; `get_*` that mutates | confirm semantic mismatch (a real bug risk). |
| **Inconsistent vocabulary** | `fetch`/`get`/`load`/`retrieve` for the same concept | confirm same operation, different words. |
| **Ceremonial / restating comments** | `# increment i by 1` above `i += 1`; `// constructor`; docstrings that restate the signature | grep comment-to-code ratio; confirm zero added information. |
| **Stale / lying comments** | comment describes behavior the code no longer has | confirm divergence from code. |
| **Banner / ASCII-art noise** | `#########` dividers, decorative boxes | cosmetic; bundle, don't file alone. |
| **Over-explaining the obvious** | paragraph docstring on a one-line getter | confirm verbosity without value. |
| **AI-tell comments** | "Note: this is a placeholder", "In a real implementation...", "This should probably...", "As an AI..." | grep; these flag unfinished/uncertain code — pair with the code they describe. |
| **Commented apologies / TODO/FIXME/HACK/XXX** | debt markers left in tree | grep; triage each — actionable now ⇒ file it. |

---

## Family 7 — Verbosity & Needless Complexity

| Smell | The tell | Corroboration hint |
|-------|----------|--------------------|
| **Reinventing the stdlib** | hand-rolled `groupby`, manual `dict.get` chains, custom date math | confirm a standard/idiomatic one-liner exists. |
| **Yoda / redundant conditionals** | `if x == True`, `if len(xs) > 0`, `return True if c else False`, `x if x else y` | ruff `SIM*`; trivially real. |
| **Unnecessary intermediate variables / indirection** | single-use vars, wrapper functions that only call one thing | confirm inlining clarifies. |
| **Defensive over-checking** | re-validating invariants already guaranteed; double null-checks | confirm the guard is unreachable. |
| **Premature / cargo-cult optimization** | micro-opts that obscure intent with no measured need | confirm no profiling justified it. |
| **Verbose boilerplate that a helper would kill** | the same 6-line try/except/log repeated everywhere | confirm a decorator/util fits. |
| **Wall-of-code config** | giant literal dicts/objects that should be data files | confirm it belongs in config/fixtures. |
| **Over-abstraction** | factories-of-factories, 5 layers to do one thing | confirm fewer layers suffice. |

---

## Family 8 — Type-Safety & Contract Erosion

| Smell | The tell | Corroboration hint |
|-------|----------|--------------------|
| **Escape hatches** | `# type: ignore`, `// @ts-ignore`, `// eslint-disable`, `# noqa`, `cast(Any, ...)` without justification | grep; confirm no linked issue justifies it (CLAUDE.md bans unjustified ones). |
| **`Any` / `unknown` creep** | `Any` params/returns, `as any`, untyped dicts crossing boundaries | mypy/tsc; confirm a real type is knowable. |
| **Loose assertions** | `as Foo` casts that bypass checks, non-null `!` operator overuse | confirm the cast can be wrong at runtime. |
| **Frontend/backend shape drift** | TS type and Pydantic schema for the same payload disagree | diff the two; confirm a field/optionality mismatch. |
| **Missing annotations** | public functions without signatures (where strict mode allows) | mypy/tsc strict gaps. |

---

## Family 9 — Testing Slop

| Smell | The tell | Corroboration hint |
|-------|----------|--------------------|
| **Coverage theater** | high % but assertions are weak/absent | mutation testing — does a logic flip survive? |
| **Tests that never fail** | `assert True`, asserting mocks were called but not outcomes, snapshot-only | confirm no behavioral assertion. |
| **Over-mocking** | mocking the unit under test; testing the mock | confirm the real behavior isn't exercised. |
| **Missing edge/error-path tests** | only happy path covered | coverage of error branches; confirm gaps. |
| **Flaky tests** | time/order/network dependent | confirm nondeterminism source. |
| **Commented-out / skipped tests** | `@pytest.mark.skip`, `xit`, `it.skip` without an issue ref | grep; CLAUDE.md requires a justification. |

---

## Family 10 — Security & Safety Slop

(Defer deep work to the `security` skill; this catalog only flags candidates.)

| Smell | The tell | Corroboration hint |
|-------|----------|--------------------|
| **Hardcoded secret/credential** | API keys, tokens, passwords in source | detect-secrets / gitleaks; confirm it's a live secret shape. |
| **Injection vector** | string-built SQL, `eval`, `exec`, shell with user input, `dangerouslySetInnerHTML` | bandit / eslint; confirm user input reaches the sink. |
| **Missing authz check** | endpoint mutates another user's data without ownership check | trace the handler; confirm no guard. |
| **Permissive CORS / wildcard** | `allow_origins=["*"]` with credentials | confirm config. |
| **Weak crypto / homemade auth** | md5/sha1 for passwords, custom token signing | confirm the primitive. |
| **Sensitive data in logs** | logging tokens, PII, journal contents | grep log calls near sensitive fields. |
| **Vulnerable dependency** | pinned dep with a known CVE | pip-audit / npm audit (defer fix to `cve-remediation`). |

---

## Family 11 — Dependency, Build & Config Hygiene

| Smell | The tell | Corroboration hint |
|-------|----------|--------------------|
| **Unused dependency** | declared in requirements/package.json, never imported | grep imports; confirm zero use. |
| **Phantom dependency** | imported but not declared | confirm import without a manifest entry. |
| **Duplicate / conflicting config** | two tools configured for the same job, contradicting | confirm overlap. |
| **Magic numbers / strings** | unexplained literals (`* 86400`, `0.7`, status codes) | confirm a named constant adds meaning. |
| **Environment-coupled code** | hard-coded localhost/paths/ports | confirm it breaks outside dev. |
| **Copy-pasted config drift** | the same setting set differently in N places | diff the values. |

---

## Family 12 — AI-Slop-Specific Patterns (2024–2026)

LLM/agent-generated code has characteristic failure modes worth hunting
explicitly. (Sources: AI-slop research catalogs; SpecDetect4AI; empirical
studies of LLM-generated code.)

| Smell | The tell | Corroboration hint |
|-------|----------|--------------------|
| **Confident wrongness** | plausible code that compiles and reads well but is subtly incorrect | only filed with a reproducing test — never on vibes. |
| **Hallucinated API / import** | calls to functions/params/endpoints that don't exist | tsc/mypy/import error; trivially real if it doesn't resolve. |
| **Reinvented existing helper** | a fresh utility duplicating one already in the repo | grep for the existing helper; confirm overlap. |
| **Ceremonial scaffolding** | elaborate structure (interfaces, abstract bases) around trivial logic | confirm the ceremony exceeds the need. |
| **Explanatory-comment spam** | every line narrated; docstrings restating types | comment density metric. |
| **"In a real implementation" stubs** | placeholder bodies with apologetic comments | grep AI-tell phrases. |
| **Inconsistent-with-house-style** | code that ignores the repo's established patterns (e.g. not using `Depends(get_session)`) | compare to the canonical pattern in CLAUDE.md. |
| **Defensive sludge** | redundant try/except, re-checking guaranteed invariants, belt-and-suspenders nobody asked for | confirm unreachable defenses. |
| **Over-broad error handling that hides bugs** | catch-all that turns a crash into a wrong-but-silent result | confirm a real failure is masked. |

---

## Severity rubric

Assign each corroborated finding a severity. This maps to issue labels and to
how aggressively Ralph should pick it up.

| Severity | Definition | Examples |
|----------|------------|----------|
| **Critical** | Causes data loss, security breach, or user-facing breakage now | live secret, missing authz, injection, swallowed error on a write path, a stub callers depend on |
| **High** | Latent correctness bug, architectural debt that compounds, lying feature flag | off-by-one behind a flag, N+1 on a hot path, circular dep, frontend/backend shape drift |
| **Medium** | Maintainability tax, real dead/duplicated code, weak tests | god function, duplicated block, coverage theater, dead module |
| **Low** | Readability, naming, comment slop, cosmetic verbosity | ceremonial comments, meaningless names, redundant conditionals |

**Bundling rule:** many Low findings in one file/area = one tidy-up issue, not
twenty. Don't death-by-a-thousand-issues the backlog.

---

## What is explicitly NOT slop (false-positive guards)

The detector must *not* file these. Treating them as findings is itself slop.

- **Intentional, documented simplicity** — a small function is not a "lazy class."
- **Justified suppressions** — a `# type: ignore[...]` or `// eslint-disable`
  with a linked issue and a real reason is allowed by CLAUDE.md. Leave it.
- **Test fixtures and factories** — duplication and "magic" values in tests are
  often deliberate and readable.
- **Generated code / vendored files / migrations** — Alembic migrations,
  `package-lock.json`, build output. Out of scope.
- **Framework-required boilerplate** — Pydantic models, SQLModel tables, React
  Navigation config have irreducible shape.
- **Domain constants that are self-explanatory** — `status_code=404`, `HTTP_200`.
- **Style the repo has deliberately chosen** — match CLAUDE.md/AGENTS.md, not a
  generic ideal.
- **Speculative "could be faster"** without a measurement — premature.
- **Anything you can't corroborate** — if the second signal isn't there, it's a
  hypothesis, not a finding. Drop it.
