# `creek lint` — unified vault hygiene

`creek lint` is the named entry point for the operations that
[`docs/emergence.md`](emergence.md) calls Tag Garden, Unnamed Digest,
Synchronicity, Compost, and Paradox Preservation, plus three new
deterministic checks (broken wiki-links, orphan compiled pages, and
schema-skill size budgets). FEAT-008 implements it as one verb so the
rest of the system (CrawDad, agents, the audit report) can call into a
single surface instead of five.

## Non-negotiable rules

Lint is *not* a generic-wiki lint. The following rules are pinned in
the module docstring at `creek/lint/__init__.py` and verified by the
regression tests in `tests/test_lint.py`:

1. **Lint never resolves paradoxes.** Detected contradictions route to
   `10-Liminal/Paradoxes/`. The existing `ParadoxDetector` writes the
   note; lint only counts and summarises.
2. **Lint never auto-creates compiled pages.** Missing-page candidates
   are surfaced as suggestions only. The human (or `creek compile`)
   decides when an Unnamed pattern earns a Thread or Eddy.
3. **Lint never deletes orphan fragments.** Isolated insights belong
   somewhere even when not yet linked. Only orphan *compiled* pages
   are surfaced — and even those are suggestions, not deletions.

## CLI surface

```bash
creek lint                                  # all deterministic checks
creek lint --check paradox                  # one specific check
creek lint --check broken-links --check tags  # multiple checks
creek lint --since 7d                       # include semantic checks
```

| Flag | Meaning |
|---|---|
| `--check NAME` | Run only the named check(s). Repeatable. |
| `--since DURATION` | Incremental window. Accepts `7d`, `1w`, `1mo`, `30d`. Also triggers semantic checks. |
| `--vault PATH` | Vault root. Falls back to config. |

### Check names

Deterministic (always run by default):

- `broken-links` — wiki-links / relative links that do not resolve.
- `orphan-compiled` — Threads / Eddies / Praxis / Frequency-indexes
  that no fragment links to.
- `skill-size` — `.SKILL.md` files over their declared word budget
  (proxy for the ≤1500-token limit pinned by `lint.SKILL.md`); also
  enforces ≤3000 words on root `AGENTS.md`.
- `tags` — single-use tags surfaced as orphans (never auto-deleted).
- `compost` — counts of recorded compost notes under `10-Liminal/Compost/`.

Semantic (run when `--since` or an explicit `--check` is passed):

- `paradox` — contradictions routed to `10-Liminal/Paradoxes/`.
- `synchronicity` — surprising cross-source resonances already on disk.
- `unnamed` — fragments whose primary frequency is `unclassified`,
  wherever they live, plus anything physically under `10-Liminal/Unnamed/`.

## Output

Each run writes a per-run report at
`00-Creek-Meta/Processing-Log/lint-<iso-date>.md`. The processing log
is append-only — successive runs in the same day overwrite that day's
file but do not touch prior days.

The next `creek state` run reads the most recent lint report and
appends it to the audit report under the `## Lint summary` section
(FEAT-008 acceptance criterion).

## Composition with other verbs

- `creek compile` — compile reads what lint flags, but lint never
  compiles. The decision to materialise a compiled page stays with the
  human.
- `creek state` — appends the latest lint report. Section ordering is
  pinned by FEAT-006 / -008 tests.
- `creek report --type {tags,unnamed,…}` — preserved as thin wrappers
  over the original generators for backwards compatibility.

## See also

- [`emergence.md`](emergence.md) — the §10 emergence subsystems lint
  unifies.
- [`00-Creek-Meta/Skills/lint.SKILL.md`](../../00-Creek-Meta/Skills/lint.SKILL.md)
  — the schema skill that load-bears these rules.
- `FEAT-008` — the implementation plan (use `git log --grep='FEAT-008'`
  for the originating commits; `plans/git-issues/` was retired in #243).
