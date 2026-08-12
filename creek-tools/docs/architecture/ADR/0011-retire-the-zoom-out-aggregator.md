# ADR-0011: Retire the FEAT-022 zoom-out aggregator

- **Status**: Accepted
- **Date**: 2026-08-12
- **Driving issue**: #1342. Successor work tracked in #1457.

## Context

FEAT-022 (#253) was meant to be the zoom-out twin of the FEAT-021
zoom-in splitter: where `split()` carves a large document into finer
children, the aggregator (`creek/atomize/aggregate.py`) was meant to
stitch short chat turns into coarser parents — message → exchange →
burst → session — so a Discord or Claude conversation would classify
at a grain wider than a single line. FEAT-023 (#254) wired it into the
re-atomization orchestrator (`_route_stream` / `_zoom_out_stream` /
`_tree_for_parent` in `creek/classify/reatomize.py`), and FEAT-027
(#264) added a cross-source mode on top of it. All three issues are
**closed** — the capability was marked shipped.

It never had a caller. `classify_reatomize_stream` and
`_zoom_out_stream` were the only functions that invoked `aggregate()`,
and a repo-wide search for callers of `classify_reatomize_stream`
turned up nothing outside `creek/classify/reatomize.py` itself and
`tests/test_reatomize.py`. The only production entry point is
`classify_reatomize`, and it never reached the stream path: whenever
the resolved direction was `"aggregate"` it returned a childless
`aggregate_no_siblings` leaf. So `creek classify --reatomize
--reatomize-direction aggregate` was a complete no-op, and — because
`choose_direction`'s default `auto` heuristic already resolves to
`aggregate` for any Discord, Claude, or ChatGPT fragment at
`document`/`exchange` level — so was every re-atomization attempt on
the bulk of a chat-heavy vault, with no flag required to hit it.
Nowhere in production does anything mint a fragment at level
`exchange`, `burst`, or `session`; those values survived only as
`FragmentLevel` vocabulary nothing produced.

The rest of this section states what the aggregator **would have**
done had a caller ever reached it — never what it *did* do, because it
never ran outside its own test suite.

### The privacy argument: a downgrade, not a fail-open

This is not a re-opening of the hole issue #876 closed. #876 fixed a
*ranking* — `creek/classify/privacy_filter.py`'s `_TIER_RANK` puts
`PrivacyTier.UNCLASSIFIED` at `1`, equal to `PERSONAL` and above
`OPEN` — and that fix is still fully in force. An untiered fragment is
refused outright at the default `open` ceiling; nothing in this
retirement touches that. The accurate claim is narrower: the
aggregator would have introduced a **tier downgrade** on its own
output, reachable only through the path above.

- The deleted `_build_parent` constructed `Fragment(...)` with
  `id`/`title`/`source`/`created`/`child_ids`/`level`/`tags`/`weighted`
  and no `privacy_tier`, so a freshly-minted parent defaulted to
  `PrivacyTier.UNCLASSIFIED`, the field default on `Fragment`
  (`creek/models.py`).
- A parent's title and body are both concatenations of every child's:
  the body via the joiner in `_tree_for_parent`, the title via
  `title = config.joiner.join(c.title for c in children)` in
  `_build_parent`. So even the personal-tier *summary* path — which
  emits `f"[Personal-tier summary: {title}]"`
  (`_summarize_personal` in `creek/classify/privacy_filter.py`) — would
  have surfaced every
  child's title text, intimate children included.
- Persistence stamps every re-atomized descendant from a single
  `root_tier`, taken as `tier_of(root_fragment)` for the one fragment
  whose file the engine is rewriting
  (`creek/classify/classify_engine.py`), and
  `_child_with_concrete_tier` only *fills a missing* tier — it never
  overwrites one already present:
  `if tier_of(child) is not PrivacyTier.UNCLASSIFIED: return child`.
  An aggregated parent has no single root among its N siblings, so it
  would have been stamped with one arbitrary member's tier, never the
  maximum of the set.
- The consequence, had this ever run, at `--include-tier personal`: a
  parent whose text includes an `intimate` child's content would be
  admitted with its full body at a ceiling where that same child is
  refused outright. `Fragment.voice_proxy_eligible` (`creek/models.py`)
  compounds this — it is `True` for any self-authored,
  non-`INTIMATE` fragment, so an `unclassified`-defaulted, self-authored
  parent built over `intimate` children would have been voice-proxy
  eligible while those children were not.

The problem was not unsolvable — it was simply never solved, because
nothing forced anyone to. `creek/classify/privacy_filter.py` already
provides exactly the primitive this needed: `max_source_tier`,
`max(tiers, key=tier_sensitivity, default=PrivacyTier.INTIMATE)`.
ADR-0010 elevates that same
max-of-members derivation to policy for threads and eddies, another
member-composed surface. ADR-0010 is dated 2026-08-10 — after the
aggregator was written — which is a fair account of why the aggregator
never had this: the pattern it needed didn't exist yet when it was
built. Wiring `max_source_tier` into `_build_parent` would have closed
the gap, but the persistence signature in `classify_engine.py` would
also have had to carry a derived tier down through
`_persist_reatomized_children`, rather than the single `root_tier` it
carries today. That is real design and implementation work, and it
buys nothing while the feature has zero consumers.

(The tier ranking above is deliberately context-dependent:
`creek/classify/privacy_pass.py`'s `_ESCALATION_RANK` puts
`UNCLASSIFIED` *below* `OPEN` for merge-escalation, the opposite of
`_TIER_RANK`'s admission ordering. Every claim in this ADR about
`unclassified` names the specific gate it means.)

### The level-ladder ratchet

Independent of privacy, `_zoom_out_stream` picked its target level by
reading `classified_leaves[0]` alone and stepping one rung up the
ladder `document → exchange → burst → session` from whatever level
that single leaf happened to be at. Production only ever mints
`document`-level fragments, so a first `creek classify --reatomize`
run would persist `exchange` parents; a second run would find those
`exchange` fragments already on disk and roll them up again to
`burst` (which additionally needs an embedder — see below); a third
run would roll to `session`. Each invocation of `creek classify
--reatomize` would climb another rung and mint a fresh tier of files,
and which rung a given run lands on depends on corpus iteration
order — which fragment happens to be `classified_leaves[0]` this time.
That directly contradicts the idempotency promise
`classify_reatomize`'s own docstring makes: "re-running on
byte-identical input yields a tree of identical shape."

### An engine inversion, not a wiring job

`run_classify` processes one markdown file at a time through a thread
pool, and `_maybe_reatomize_and_persist` runs per fragment inside a
lock. Zoom-out needs the opposite shape: the whole corpus grouped by
`FragmentSource` *before* any per-fragment decision can be made — you
cannot decide whether a message joins an exchange without having seen
the other messages in that exchange first. Restoring this is not a
matter of calling the existing function from a new call site; it
requires re-architecting the classify engine's iteration order for
one direction while leaving the other (splitting) exactly as it is.

### Secondary factors, kept in proportion

- **The embedder.** `burst` grouping needs dense embeddings, and
  `AggregationConfig.embedder` defaulted to `None` — nothing wired
  one in. Per ADR-0004, embeddings are local-only sentence-transformers,
  so supplying one is a dependency-shape and startup-cost question for
  the classify stage, not a privacy question. It is also moot on a
  first run: the ladder above means `burst` is never reached until a
  vault has already been rolled up twice.
- **Corpus growth.** Each aggregated parent duplicates the text of
  every child it stitches together, growing the fragment count `n`
  fed into the already-O(n²) pairwise link pass (a scale limit hit
  before, at 35k fragments). Aggregation on a chat-heavy vault would
  add on the order of 20-30% more `n`, so roughly 1.5x the pairwise
  comparisons. `LinkingConfig.hierarchy_sibling_skip_window` (FEAT-024)
  already exists to damp parent/child link noise, which might suggest
  downstream code was built expecting these parents to exist — but
  FEAT-024's damping is fully justified by the FEAT-021 splitter path
  alone, which does produce parent/child pairs today.

### What this ADR concedes

Chat is the dominant corpus in most vaults, and `choose_direction`
correctly identifies zoom-out as the answer for it — that heuristic is
untouched by this change. After this retirement, `creek classify
--reatomize` is structurally inert for most of a chat-heavy vault: a
chat fragment at `document`/`exchange` level now stops at an honest
`no_operator` leaf instead of silently no-op'ing through
`aggregate_no_siblings`. That was already true before this change —
there was no caller to lose — but it should be said plainly rather
than let a deletion imply the capability itself was worthless. It was
not attempted; it was unreachable. The honest fix is a designed
feature — parent tier derivation, idempotent leveling, corpus-level
batching — not a wire-in of the code this ADR removes. It is tracked
in #1457.

## Decision

**Delete the FEAT-022 zoom-out aggregator in its entirety, rather than
wire it in or leave it dormant.**

- `creek/atomize/aggregate.py` is deleted.
- `classify_reatomize_stream`, `_route_stream`, `_zoom_out_stream`, and
  `_tree_for_parent` are deleted from `creek/classify/reatomize.py`.
- The three `StopReason` tokens whose only emitters were the deleted
  functions are deleted; `no_operator` is the new leaf reason for a
  fragment `choose_direction` would have routed to the retired
  operator.
- The four `linking:` config keys that existed solely to tune the
  aggregator (`exchange_max_gap_minutes`, `burst_similarity_threshold`,
  `session_max_gap_minutes`, `cross_source_aggregation`) are deleted
  from `LinkingConfig`.
- `--reatomize-direction aggregate` and
  `classification.reatomize_direction: aggregate` are rejected outright
  rather than coerced to `auto`.
- `exchange`, `burst`, and `session` remain valid `FragmentLevel`
  values — a legacy or hand-authored fragment may still carry one —
  but nothing in `creek/` currently mints one.

Wiring the aggregator in instead was rejected: doing so honestly would
have required solving the tier-derivation, idempotency, and
engine-inversion problems above first, which is exactly the design
work #1457 exists to do — there is no version of "just connect it"
that does not also do that work. Leaving it in place, dormant, was
rejected because dead code is not idle; it is a standing claim, and
this one had already survived a year of every quality gate in this
repo (`ruff`, `mypy --strict`, `radon`, `interrogate` are all
perfectly content with a well-typed, well-documented module nothing
calls) while looking, from the CLI and the YAML schema, exactly like a
feature that worked.

## Consequences

- **Rejection, not coercion, at the boundary.** `creek classify
  --reatomize --reatomize-direction aggregate` now exits with code 2
  and an explanation naming the retirement. A vault YAML carrying
  `classification.reatomize_direction: aggregate` now raises at config
  load, via a `mode="before"` validator that fires before pydantic's
  generic "not a permitted literal" error would. Coercing the value to
  `auto` instead was considered and rejected: it would be a *behaviour
  change*, not a migration — a non-chat fragment that the retired
  `aggregate` value left untouched would, under `auto`, be split into
  new child fragments the vault never had before. Rejecting loudly is
  both the honest answer and the strictly behaviour-preserving one.
- **The four `linking:` keys are gone, not deprecated.**
  `LinkingConfig` is a plain pydantic `BaseModel` with the default
  `extra='ignore'` behaviour (it declares no `model_config` override),
  so an existing `creek_config.yaml` that still carries
  `exchange_max_gap_minutes`, `burst_similarity_threshold`,
  `session_max_gap_minutes`, or `cross_source_aggregation` continues to
  load without error — the keys are silently dropped rather than
  rejected. A regression test pins that a vault config carrying all
  four still loads cleanly.
- **The decision is enforced, not merely recorded.**
  `tests/test_config_contract.py::test_zoom_out_aggregator_may_not_return_without_a_production_consumer`
  scans every source file under `creek/` and `creek_mcp/` (minus
  `creek/config.py`, excluded by the `_production_files()` helper it
  reuses, and which names none of these tokens) for the bare tokens
  `creek.atomize.aggregate`, `AggregationConfig`, and `AggregateLevel`
  — including inside comments and docstrings — and fails the build if
  any reappears without this same test having also been updated to
  point at a real caller. This is modelled directly on the #1339
  precedent (`test_timezone_field_may_not_return_without_a_production_reader`):
  a generic dormancy allowlist is exactly how the four `linking:` keys
  survived from #1041 all the way to #1342, each declaration citing an
  issue and each dutifully re-approved by the ratchet for a year. This
  guard does not consult that allowlist at all, so the same drift
  cannot recur by the same mechanism.
- **Test surface.** 462 production lines and 1138 test lines were
  deleted (`tests/test_atomize_aggregate.py`, 841 lines / 57 tests, and
  `tests/test_aggregate_bubble_up.py`, 297 lines / 11 tests) — every one
  of those tests drove `aggregate()` directly and had no other subject.
  Baseline before this change: `check-all.sh` exit 0, 9715 tests, 94.66%
  coverage.
- **Recovery.** The full implementation — `aggregate()`, the stream
  orchestration, the config keys, the CLI value, and their tests — is
  intact in git history at the commit immediately preceding this
  change. Reintroducing it is a `git revert` plus the design work below,
  not a rewrite from nothing.

## Reopening criteria

Supersede this ADR once the coarsening work tracked in **#1457** is
designed. Any successor design must satisfy all three of the following
before it may call an aggregation operator from production code:

- **Parent tier is the maximum of its members**, computed via
  `max_source_tier` (or its equivalent at design time) and threaded
  through persistence as a real per-node value — not inherited from a
  single `root_tier`, and never defaulted to `UNCLASSIFIED`.
- **Idempotency across runs.** Re-running `creek classify --reatomize`
  on an unchanged corpus must reproduce the same tree shape every time,
  with no dependency on iteration order and no ladder-climbing that
  regrades already-aggregated fragments to the next coarser level on a
  subsequent run.
- **Corpus-level batching in the classify engine.** The direction that
  needs to see a whole `FragmentSource`'s fragments before deciding how
  to group them must be given that view — either by re-architecting
  `run_classify`'s per-file iteration for this one path, or by a
  pre-pass that groups before the existing per-fragment loop begins.

Until then, do not re-add an aggregation operator behind a flag with no
caller — that is precisely the shape this ADR retired.
