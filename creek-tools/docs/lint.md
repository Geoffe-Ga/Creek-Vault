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
  Surveys every `*.md` in the vault except three directories holding
  Creek's own machine-written documents *about* the vault:
  `00-Creek-Meta/Processing-Log/` and `00-Creek-Meta/State/` both
  render findings back as vault content — three successive `creek
  lint` runs over a vault holding ONE genuine broken link reported 1,
  then 2, then 3 — and `00-Creek-Meta/Ontology/` (the deployed spec,
  whose line 746 *documents* wiki-link syntax with a backticked
  `[[note-name]]` example: the one finding a whole-vault scan produces
  on a fresh 32-file `creek init` vault; #1460 tracks the code-span-
  aware fix that would let this prefix be dropped). Nothing else is
  withheld — `00-Creek-Meta/Tag-Garden.md` stays surveyed, since it
  emits real `[[fragment-id]]` links. `creek clean broken-links`
  shares this same scanner and scope.
- `orphan-compiled` — Threads / Eddies / Praxis pages that nothing in
  that same surveyed set links to (a page's link to itself does not
  count). A page is never a candidate when its frontmatter `type` is
  one Creek's own `IndexGenerator` writes (`frequency-index`,
  `thread-index`, `eddy-map`, `temporal-index`, `source-index` — the
  set `creek.generate.indexes.GENERATED_INDEX_TYPES`): these are
  Dataview query notes nothing is ever expected to link. Before this
  exclusion existed, a vault that had run `creek index` reported `13
  orphan compiled page(s)`, all thirteen false — twelve of them
  generated-index pages, the thirteenth cited only from an
  `08-Decisions` brief the old fragments-only survey never read.
  The exclusion needs an *explicit* declaration: a page with no `type`
  key, an unrecognised `type`, no frontmatter at all, or a header YAML
  cannot parse stays a candidate and is still reported when nothing
  links it. The default has to fall that way — were it the other way,
  any page could exempt itself from the check by writing a broken
  header, and a check that falls silent is the same end state as the
  false-positive storm it replaces.
- `skill-size` — `.SKILL.md` files over their declared word budget
  (proxy for the ≤1500-token limit pinned by `lint.SKILL.md`); also
  enforces ≤3000 words on root `AGENTS.md`.
- `tags` — single-use tags surfaced as orphans (never auto-deleted).
- `compost` — counts of recorded compost notes under `10-Liminal/Compost/`.
- `draft-grounding` — drafts whose stamped grounding scores
  (`derivative_score` / `grounding_score`) fall outside the configured
  `draft.derivative_upper` / `draft.grounding_fraction_lower` bounds.
- `voice-fidelity` — drafts whose distance from your *measured voice*
  exceeds `ai_style.voice_distance_upper` (see below).

Semantic (run when `--since` or an explicit `--check` is passed):

- `paradox` — contradictions routed to `10-Liminal/Paradoxes/`.
- `synchronicity` — surprising cross-source resonances already on disk.
- `unnamed` — fragments whose primary frequency is `unclassified`,
  wherever they live, plus anything physically under `10-Liminal/Unnamed/`.

### Voice fidelity (FEAT-040)

The `voice-fidelity` check walks `07-Voice/Drafts/`, reads the
`voice_distance` / `voice_findings` frontmatter stamped by the
generation-time guard (and re-scans any draft that lacks the stamp against
your voice fingerprint), and surfaces one finding per draft whose
`voice_distance` exceeds `ai_style.voice_distance_upper` (default `0.35`).
Each finding names the top over- and under-used features and a remediation
hint — re-run `creek draft` or revise toward your voice. It runs by default,
or on demand with `creek lint --check voice-fidelity`. If no fingerprint
exists yet it emits a single informational finding (run the profiler first)
rather than failing. Like every lint check it only **reads** — it never
rewrites or deletes a draft.

> **This is not an AI detector.** Voice distance is a *probabilistic,
> vault-relative* signal: it measures how far a draft sits from **your own
> measured writing**, not whether text "is AI". A high distance means "this
> reads less like you than your baseline" — a prompt to revise toward your
> voice, never proof of authorship and never an accusation. Humans and tools
> are unreliable at AI detection; this check exists to surface drafts for
> *your* review, not to convict. Do not use it to accuse anyone of using AI.

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
